"""Participant Jeopardy API. All lookups are scoped to the URL event."""

from flask import request
from sqlalchemy.exc import IntegrityError

from CTFd.models import ChallengeFiles, Challenges, Hints, Tags, db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import API_PREFIX, ApiError, api_blueprint, json_body, ok
from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.core.authz import event_participant_required
from CTFd.plugins.arena.core.context import scoped_get_or_404, scoped_query
from CTFd.plugins.arena.core.ratelimit import ratelimit
from CTFd.plugins.arena.models import (
    ArenaEventChallenge,
    ArenaHintUnlock,
    ArenaSubmission,
)
from CTFd.plugins.arena.services import activity
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import settings as event_settings
from CTFd.plugins.arena.services import submissions as sub_service
from CTFd.utils.user import get_current_user, get_ip

challenges_api = api_blueprint("arena_challenges_api", __name__)
E = f"{API_PREFIX}/events/<int:event_id>"


def _context(event):
    if not sub_service.challenges_viewable(event):
        raise ApiError(403, "Challenges are not available yet", code="not_started")
    user = get_current_user()
    entity = entity_service.resolve(event, user, require=(event.team_mode == "team"))
    solved = sub_service.solved_ids(event, entity.key) if entity else set()
    return user, entity, solved


def _open_challenge(event, ec_id, solved):
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    state = sub_service.challenge_state(event, ec, solved)
    if state == "hidden":
        raise ApiError(404)
    if state == "locked":
        raise ApiError(403, "Challenge is locked", code="locked")
    return ec, state


@challenges_api.route(f"{E}/challenges")
@event_participant_required
def list_challenges(event):
    _user, entity, solved = _context(event)
    show_counts = event_settings.get(event, "show_solve_counts")
    q = scoped_query(ArenaEventChallenge, event).filter(
        ArenaEventChallenge.visible.is_(True)
    )
    rows = q.order_by(ArenaEventChallenge.position, ArenaEventChallenge.id).all()
    natives = {
        c.id: c
        for c in Challenges.query.filter(
            Challenges.id.in_([r.challenge_id for r in rows] or [0])
        )
    }
    tags = {}
    for t in Tags.query.filter(Tags.challenge_id.in_(list(natives) or [0])):
        tags.setdefault(t.challenge_id, []).append(t.value)
    category = request.args.get("category")
    difficulty = request.args.get("difficulty")
    search = (request.args.get("q") or "").lower()[:80]
    status = request.args.get("status")
    out = []
    for ec in rows:
        c = natives.get(ec.challenge_id)
        state = sub_service.challenge_state(event, ec, solved)
        if state == "hidden" or c is None:
            continue
        if state == "locked":
            # Only reveal that a locked challenge exists, not its content.
            out.append(
                {
                    "id": ec.id,
                    "state": "locked",
                    "category": c.category,
                    "position": ec.position,
                }
            )
            continue
        item = {
            "id": ec.id,
            "name": c.name,
            "category": c.category,
            "type": c.type,
            "difficulty": ec.difficulty,
            "value": ec.current_value,
            "state": state,
            "solved": ec.id in solved,
            "tags": tags.get(c.id, []),
            "position": ec.position,
        }
        if show_counts:
            item["solves"] = ec.solve_count
        out.append(item)
    if category:
        out = [i for i in out if i.get("category") == category]
    if difficulty:
        out = [i for i in out if i.get("difficulty") == difficulty]
    if search:
        out = [
            i
            for i in out
            if search in (i.get("name") or "").lower()
            or any(search in t.lower() for t in i.get("tags", []))
        ]
    if status in ("solved", "unsolved"):
        out = [i for i in out if i.get("solved", False) == (status == "solved")]
    categories = sorted({i["category"] for i in out if i.get("category")})
    return ok(out, meta={"categories": categories})


@challenges_api.route(f"{E}/challenges/<int:ec_id>")
@event_participant_required
def get_challenge(event, ec_id):
    _user, entity, solved = _context(event)
    ec, state = _open_challenge(event, ec_id, solved)
    activity.record(
        "challenge.view",
        user_id=_user.id,
        event_id=event.id,
        target=("challenge", ec.id),
    )
    c = Challenges.query.filter_by(id=ec.challenge_id).first()
    unlocked = set()
    if entity:
        unlocked = {
            hid
            for (hid,) in db.session.query(ArenaHintUnlock.hint_id).filter_by(
                event_id=event.id, entity_key=entity.key, event_challenge_id=ec.id
            )
        }
    hints = []
    for h in Hints.query.filter_by(challenge_id=c.id).order_by(Hints.id):
        item = {
            "id": h.id,
            "title": h.title,
            "cost": h.cost,
            "unlocked": h.id in unlocked or h.cost == 0,
        }
        if item["unlocked"]:
            item["content_html"] = event_service.render_markdown(h.content)
        hints.append(item)
    attempts = None
    if entity:
        attempts = ArenaSubmission.query.filter_by(
            event_id=event.id,
            entity_key=entity.key,
            event_challenge_id=ec.id,
            status="incorrect",
        ).count()
    s = event_settings.effective(event)
    max_attempts = (
        ec.max_attempts if ec.max_attempts is not None else s["max_attempts_default"]
    )
    return ok(
        {
            "id": ec.id,
            "name": c.name,
            "category": c.category,
            "type": c.type,
            "difficulty": ec.difficulty,
            "description_html": event_service.render_markdown(c.description),
            "connection_info": c.connection_info,
            "attribution": c.attribution,
            "value": ec.current_value,
            "scoring": ec.scoring,
            "state": state,
            "solved": ec.id in solved,
            "solves": ec.solve_count if s["show_solve_counts"] else None,
            "max_attempts": max_attempts or None,
            "attempts": attempts,
            "available_until": v.iso(ec.available_until),
            "has_instance": ec.image_id is not None,
            "tags": [t.value for t in Tags.query.filter_by(challenge_id=c.id)],
            "hints": hints,
            "files": [
                {
                    "id": f.id,
                    "name": f.location.rsplit("/", 1)[-1],
                    "url": f"{E.replace('<int:event_id>', str(event.id))}/files/{f.id}",
                }
                for f in ChallengeFiles.query.filter_by(challenge_id=c.id)
            ],
        }
    )


@challenges_api.route(f"{E}/challenges/<int:ec_id>/attempt", methods=["POST"])
@ratelimit(method="POST", limit=30, interval=60, key_prefix="arena-attempt")
@event_participant_required
def attempt(event, ec_id):
    user = get_current_user()
    entity = entity_service.resolve(event, user)
    ec = scoped_get_or_404(ArenaEventChallenge, ec_id, event)
    result = sub_service.attempt(
        event, ec, entity, user, json_body().get("submission"), ip=get_ip()
    )
    action = "challenge.solve" if result["status"] == "correct" else "challenge.attempt"
    activity.record(
        action,
        user_id=user.id,
        event_id=event.id,
        target=("challenge", ec.id),
        meta={"status": result["status"]},
    )
    result.pop("submission_id", None)
    result.pop("solve_id", None)
    return ok(result)


@challenges_api.route(f"{E}/hints/<int:hint_id>/unlock", methods=["POST"])
@event_participant_required
def unlock_hint(event, hint_id):
    user, entity, solved = _context(event)
    if entity is None:
        raise ApiError(403, "Join or create a team first", code="no_team")
    hint = Hints.query.filter_by(id=hint_id).first()
    if hint is None:
        raise ApiError(404)
    ec = (
        scoped_query(ArenaEventChallenge, event)
        .filter_by(challenge_id=hint.challenge_id)
        .first()
    )
    if ec is None:  # hint of another event's (or a native) challenge
        raise ApiError(404)
    _open_challenge(event, ec.id, solved)
    if not event_service.is_running(event):
        raise ApiError(403, "The event is not running", code="event_not_running")
    from CTFd.plugins.arena.services import scoring

    try:
        unlock = ArenaHintUnlock(
            event_id=event.id,
            event_challenge_id=ec.id,
            hint_id=hint.id,
            entity_key=entity.key,
            user_id=user.id,
            cost=hint.cost or 0,
        )
        db.session.add(unlock)
        db.session.flush()
        scoring.record_hint_unlock(event, unlock)  # same transaction as the unlock
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # already unlocked: idempotent, no double cost
    activity.record(
        "hint.unlock",
        user_id=user.id,
        event_id=event.id,
        target=("hint", hint.id),
        meta={"cost": hint.cost or 0},
    )
    for hook in sub_service.POST_COMMIT_HOOKS:
        try:
            hook(event, ec, entity, {"status": "hint_unlocked"})
        except Exception:  # noqa: BLE001 - never fail a committed unlock
            sub_service.log.exception("post-commit hook failed after hint unlock")
    return ok(
        {
            "id": hint.id,
            "cost": hint.cost,
            "content_html": event_service.render_markdown(hint.content),
        }
    )


@challenges_api.route(f"{E}/files/<int:file_id>")
@event_participant_required
def download_file(event, file_id):
    _user, _entity, solved = _context(event)
    f = ChallengeFiles.query.filter_by(id=file_id).first()
    if f is None:
        raise ApiError(404)
    ec = (
        scoped_query(ArenaEventChallenge, event)
        .filter_by(challenge_id=f.challenge_id)
        .first()
    )
    if ec is None:
        raise ApiError(404)
    _open_challenge(event, ec.id, solved)
    from CTFd.utils.uploads import get_uploader

    response = get_uploader().download(f.location)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@challenges_api.route(f"{E}/my-submissions")
@event_participant_required
def my_submissions(event):
    user = get_current_user()
    entity = entity_service.resolve(event, user, require=False)
    if entity is None:
        return ok([])
    rows = (
        scoped_query(ArenaSubmission, event)
        .filter_by(entity_key=entity.key)
        .order_by(ArenaSubmission.id.desc())
        .limit(50)
        .all()
    )
    names = dict(
        db.session.query(ArenaEventChallenge.id, Challenges.name)
        .join(Challenges, Challenges.id == ArenaEventChallenge.challenge_id)
        .filter(ArenaEventChallenge.event_id == event.id)
    )
    return ok(
        [
            {
                "challenge_id": r.event_challenge_id,
                "challenge": names.get(r.event_challenge_id),
                "status": r.status,
                "user_id": r.user_id,
                "date": v.iso(r.created_at),
            }
            for r in rows
        ]
    )


def can_preview(user, event):
    return rbac.has_permission(user, rbac.PERM_CHALLENGES_MANAGE, event.id)
