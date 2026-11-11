"""Scoreboard API (participant + organizer)."""

from flask import make_response, request

from CTFd.models import db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import (
    API_PREFIX,
    ApiError,
    api_blueprint,
    json_body,
    ok,
    paginate,
    pagination,
)
from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.core.authz import event_permission_required, event_view
from CTFd.plugins.arena.core.context import get_registration, scoped_query
from CTFd.plugins.arena.models import ArenaScoreEvent
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import scoreboard as sb
from CTFd.plugins.arena.services import scoring
from CTFd.plugins.arena.services import settings as event_settings
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import authed, get_current_user

scoreboard_api = api_blueprint("arena_scoreboard_api", __name__)
E = f"{API_PREFIX}/events/<int:event_id>"
A = f"{API_PREFIX}/admin/events/<int:event_id>"


def _viewer_can_see(event):
    """Scoreboard visibility setting (server-side)."""
    user = get_current_user() if authed() else None
    if user is not None and rbac.has_permission(
        user, rbac.PERM_EVENT_VIEW_ADMIN, event.id
    ):
        return True
    mode = event_settings.get(event, "scoreboard_visibility")
    if mode == "public":
        return True
    if mode == "participants":
        reg = get_registration(event, user)
        return reg is not None and reg.status == "approved"
    return False


def _respond(payload, tag, page, per_page):
    rows = payload["standings"]
    total = len(rows)
    sliced = rows[(page - 1) * per_page : page * per_page]
    body = dict(payload, standings=sliced)
    meta = {
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        }
    }
    response = ok(body, meta=meta)
    if tag:
        response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["X-Arena-Refresh"] = str(payload.get("refresh_seconds", 15))
    return response


def _serve(event, scope):
    page, per_page = pagination(default_per_page=50, max_per_page=200)
    ver = sb.version(event.id)
    tag = sb.etag(event.id, scope, ver)
    raw = tag[3:-1]  # W/"<raw>" -> <raw>
    if (
        request.if_none_match
        and request.if_none_match.contains_weak(raw)
        and sb.arena_cache.available()
    ):
        response = make_response("", 304)
        response.headers["ETag"] = tag
        return response
    payload, tag, _source = sb.get(event, scope)
    return _respond(payload, tag, page, per_page)


@scoreboard_api.route(f"{E}/scoreboard")
@event_view
def scoreboard(event):
    if not _viewer_can_see(event):
        raise ApiError(403, "The scoreboard is not public", code="scoreboard_hidden")
    view = request.args.get("view", "default")
    scope = "public"
    if view == "individuals":
        if event.team_mode == "team" and not event_settings.get(
            event, "individual_scoring"
        ):
            raise ApiError(404, "Individual scoring is disabled for this event")
        scope = "public:users"
    return _serve(event, scope)


@scoreboard_api.route(f"{E}/scoreboard/categories")
@event_view
def categories(event):
    if not _viewer_can_see(event):
        raise ApiError(403, code="scoreboard_hidden")
    return ok(scoring.category_standings(event, scope="public"))


@scoreboard_api.route(f"{E}/scoreboard/history")
@event_view
def history(event):
    if not _viewer_can_see(event):
        raise ApiError(403, code="scoreboard_hidden")
    top = v.integer(request.args, "top", minimum=1, maximum=25) or 10
    payload, _tag, _src = sb.get(event, "public")
    keys = [r["key"] for r in payload["standings"][:top]]
    names = {r["key"]: r["name"] for r in payload["standings"][:top]}
    series = scoring.history(event, keys, scope="public")
    return ok([{"key": k, "name": names[k], "series": series[k]} for k in keys])


@scoreboard_api.route(f"{E}/scoreboard/challenges")
@event_view
def challenge_stats(event):
    if not _viewer_can_see(event) or not event_settings.get(event, "show_solve_counts"):
        raise ApiError(403, code="scoreboard_hidden")
    from CTFd.plugins.arena.services import submissions as sub_service

    stats = scoring.challenge_stats(event)
    user = get_current_user() if authed() else None
    is_admin = user is not None and rbac.has_permission(
        user, rbac.PERM_EVENT_VIEW_ADMIN, event.id
    )
    if not is_admin:
        # Don't reveal hidden/unreleased challenges through statistics.
        from CTFd.plugins.arena.models import ArenaEventChallenge

        open_ids = {
            ec.id
            for ec in scoped_query(ArenaEventChallenge, event)
            if sub_service.challenge_state(event, ec, set()) not in ("hidden", "locked")
        }
        stats = [s for s in stats if s["id"] in open_ids]
    return ok(stats)


@scoreboard_api.route(f"{E}/my-score")
@event_view
def my_score(event):
    if not authed():
        raise ApiError(401)
    ent = entity_service.resolve(event, get_current_user(), require=False)
    if ent is None:
        return ok(None)
    if not _viewer_can_see(event):
        raise ApiError(403, code="scoreboard_hidden")
    payload, _tag, _src = sb.get(event, "public")
    row = next((r for r in payload["standings"] if r["key"] == ent.key), None)
    return ok(row or {"key": ent.key, "score": 0, "rank": None})


# ------------------------------------------------------------------- admin


@scoreboard_api.route(f"{A}/scoreboard")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def admin_scoreboard(event):
    scope = "admin:users" if request.args.get("view") == "individuals" else "admin"
    return _serve(event, scope)


@scoreboard_api.route(f"{A}/score-events")
@event_permission_required(rbac.PERM_AUDIT_READ)
def score_events(event):
    q = scoped_query(ArenaScoreEvent, event)
    for f in ("component", "entity_key"):
        if request.args.get(f):
            q = q.filter(getattr(ArenaScoreEvent, f) == request.args[f][:32])
    page, per_page = pagination(default_per_page=50, max_per_page=200)
    data, meta = paginate(
        q.order_by(ArenaScoreEvent.id.desc()),
        page,
        per_page,
        scoring.serialize_event_row,
    )
    return ok(data, meta=meta)


@scoreboard_api.route(f"{A}/score-adjustments", methods=["POST"])
@event_permission_required(rbac.PERM_SCORING_MANAGE)
def adjust(event):
    data = json_body()
    row = scoring.adjust(
        event,
        v.string(data, "entity_key", required=True, max_len=32),
        data.get("points"),
        v.string(data, "reason", required=True, max_len=255),
        get_current_user(),
        idempotency_key=v.string(data, "idempotency_key", max_len=64),
    )
    sb.invalidate(event.id)
    audit(
        "score.adjust",
        event_id=event.id,
        target=("entity", row.entity_key),
        meta={"points": row.points, "reason": row.reason},
    )
    return ok(scoring.serialize_event_row(row), status=201)


@scoreboard_api.route(f"{A}/scoreboard/freeze", methods=["POST"])
@event_permission_required(rbac.PERM_SCORING_MANAGE)
def freeze(event):
    data = json_body()
    freeze_at = v.dt(data, "freeze_at")
    if freeze_at and (
        (event.starts_at and freeze_at < event.starts_at)
        or (event.ends_at and freeze_at > event.ends_at)
    ):
        raise v.ValidationError("freeze_at", "must be within the event window")
    event.freeze_at = freeze_at
    db.session.commit()
    sb.invalidate(event.id)
    audit(
        "score.freeze",
        event_id=event.id,
        target=("event", event.id),
        meta={"freeze_at": v.iso(freeze_at)},
    )
    return ok({"freeze_at": v.iso(event.freeze_at)})


@scoreboard_api.route(f"{A}/scoreboard/verify")
@event_permission_required(rbac.PERM_AUDIT_READ)
def verify(event):
    """Compare the served (cached) board with a fresh computation from the DB."""
    cached, _tag, source = sb.get(event, "admin")
    fresh = scoring.compute_standings(event, scope="admin")
    simplify = lambda rows: [(r["key"], r["score"], r["rank"]) for r in rows]  # noqa: E731
    problems = scoring.verify(event)
    matches = simplify(cached["standings"]) == simplify(fresh)
    if not matches:
        problems.append("cached scoreboard differs from database computation")
    return ok(
        {
            "consistent": not problems,
            "problems": problems,
            "cache_source": source,
            "entities": len(fresh),
        }
    )


@scoreboard_api.route(f"{A}/scoreboard/invalidate", methods=["POST"])
@event_permission_required(rbac.PERM_SCORING_MANAGE)
def force_refresh(event):
    sb.invalidate(event.id)
    return ok({"version": sb.version(event.id)})


# ------------------------------------------------------- mixed scoring (P10)


@scoreboard_api.route(f"{E}/scoring")
@event_view
def scoring_rules(event):
    """Public scoring rules so participants can see how they are ranked."""
    from CTFd.plugins.arena.services import mixed

    cfg = mixed.active_config(event)
    return ok(
        {
            "type": event.type,
            "version": cfg["version"],
            "weights": {
                "jeopardy": cfg["jeopardy_weight"],
                "attack_defense": cfg["ad_weight"],
            },
            "tie_break": cfg["tie_break"],
            "first_blood_bonus": event_settings.get(event, "first_blood_bonus"),
            "freeze_at": v.iso(event.freeze_at),
        }
    )


@scoreboard_api.route(f"{A}/scoring/configs")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def list_configs(event):
    from CTFd.plugins.arena.models import ArenaScoringConfig
    from CTFd.plugins.arena.services import mixed

    rows = (
        scoped_query(ArenaScoringConfig, event)
        .order_by(ArenaScoringConfig.version.desc())
        .all()
    )
    return ok(
        {
            "active": mixed.active_config(event),
            "versions": [mixed.serialize_config(c) for c in rows],
        }
    )


@scoreboard_api.route(f"{A}/scoring/configs", methods=["POST"])
@event_permission_required(rbac.PERM_SCORING_MANAGE)
def create_config(event):
    from CTFd.plugins.arena.services import mixed

    cfg, diff = mixed.create_config(event, json_body(), get_current_user())
    audit(
        "score.config",
        event_id=event.id,
        target=("scoring_config", cfg.version),
        meta={
            "params": cfg.params,
            "reason": cfg.reason,
            "changed_entities": len(diff),
        },
    )
    return ok({"config": mixed.serialize_config(cfg), "changes": diff}, status=201)


@scoreboard_api.route(f"{A}/scoring/snapshots")
@event_permission_required(rbac.PERM_AUDIT_READ)
def list_snapshots(event):
    from CTFd.plugins.arena.models import ArenaScoreSnapshot
    from CTFd.plugins.arena.services import mixed

    q = scoped_query(ArenaScoreSnapshot, event)
    if request.args.get("kind"):
        q = q.filter(ArenaScoreSnapshot.kind == request.args["kind"][:16])
    page, per_page = pagination(default_per_page=50)
    data, meta = paginate(
        q.order_by(ArenaScoreSnapshot.id.desc()),
        page,
        per_page,
        mixed.serialize_snapshot,
    )
    return ok(data, meta=meta)


@scoreboard_api.route(f"{A}/scoring/snapshots/<int:snap_id>")
@event_permission_required(rbac.PERM_AUDIT_READ)
def get_snapshot(event, snap_id):
    from CTFd.plugins.arena.core.context import scoped_get_or_404
    from CTFd.plugins.arena.models import ArenaScoreSnapshot
    from CTFd.plugins.arena.services import mixed

    return ok(
        mixed.serialize_snapshot(
            scoped_get_or_404(ArenaScoreSnapshot, snap_id, event), True
        )
    )


@scoreboard_api.route(f"{A}/scoring/snapshots", methods=["POST"])
@event_permission_required(rbac.PERM_SCORING_MANAGE)
def take_snapshot(event):
    from CTFd.plugins.arena.services import mixed

    snap = mixed.snapshot(
        event, "manual", ref=(v.string(json_body(), "label", max_len=64) or None)
    )
    audit("score.snapshot", event_id=event.id, target=("snapshot", snap.id))
    return ok(mixed.serialize_snapshot(snap), status=201)
