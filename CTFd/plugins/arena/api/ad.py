"""Attack & Defense API."""

from flask import request

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
from CTFd.plugins.arena.core.authz import (
    event_participant_required,
    event_permission_required,
)
from CTFd.plugins.arena.core.context import scoped_get_or_404, scoped_query
from CTFd.plugins.arena.core.ratelimit import ratelimit
from CTFd.plugins.arena.models import (
    ArenaADCapture,
    ArenaADCheck,
    ArenaADRound,
    ArenaADService,
    ArenaADTeamService,
    ArenaInstance,
    ArenaScoreEvent,
    ArenaTeam,
)
from CTFd.plugins.arena.services import activity
from CTFd.plugins.arena.services import ad as ad_service
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import get_current_user

ad_api = api_blueprint("arena_ad_api", __name__)
E = f"{API_PREFIX}/events/<int:event_id>/ad"
A = f"{API_PREFIX}/admin/events/<int:event_id>/ad"
OPERATE = rbac.PERM_AD_OPERATE


def _require_ad(event):
    if not event.ad_enabled:
        raise ApiError(404, "Attack & Defense is not enabled for this event")


def _team(event):
    ent = entity_service.resolve(event, get_current_user())
    return db.session.get(ArenaTeam, ent.team_id)


# --------------------------------------------------------------- participant


@ad_api.route(f"{E}/status")
@event_participant_required
def status(event):
    _require_ad(event)
    team = _team(event)
    rnd = ad_service.current_round(event)
    services = {
        s.id: s for s in ArenaADService.query.filter_by(event_id=event.id, enabled=True)
    }
    mine, targets = [], []
    for ts in ArenaADTeamService.query.filter_by(event_id=event.id).all():
        svc = services.get(ts.service_id)
        if svc is None:
            continue
        inst = db.session.get(ArenaInstance, ts.instance_id) if ts.instance_id else None
        endpoint = (
            {"host": inst.host, "ports": inst.ports}
            if inst and inst.status == "running"
            else None
        )
        if ts.team_id == team.id:
            mine.append(
                {
                    "service": svc.name,
                    "service_id": svc.id,
                    "status": ts.status,
                    "endpoint": endpoint,
                    "last_check_at": v.iso(ts.last_check_at),
                }
            )
        else:
            # Targets: other teams' public endpoints only (that is the game);
            # no flags, no credentials, no internal instance identifiers.
            targets.append(
                {
                    "team_id": ts.team_id,
                    "service": svc.name,
                    "service_id": svc.id,
                    "endpoint": endpoint,
                }
            )
    uptime = ad_service.uptime(event, team_id=team.id)
    return ok(
        {
            "round": ad_service.serialize_round(rnd) if rnd else None,
            "team_id": team.id,
            "my_services": mine,
            "targets": targets,
            "uptime": uptime,
            "services": [
                {"id": s.id, "name": s.name, "description": s.description}
                for s in services.values()
            ],
        }
    )


@ad_api.route(f"{E}/flags", methods=["POST"])
@ratelimit(method="POST", limit=120, interval=60, key_prefix="arena-ad-flags")
@event_participant_required
def submit(event):
    _require_ad(event)
    team = _team(event)
    user = get_current_user()
    results = ad_service.submit_flags(event, team, user, json_body().get("flags"))
    accepted = sum(1 for r in results if r["result"] == "accepted")
    activity.record(
        "ad.flag_submit",
        user_id=user.id,
        event_id=event.id,
        target=("team", team.id),
        meta={"submitted": len(results), "accepted": accepted},
    )
    return ok({"results": results, "accepted": accepted})


@ad_api.route(f"{E}/rounds")
@event_participant_required
def my_rounds(event):
    _require_ad(event)
    team = _team(event)
    key = entity_service.team_key(team.id)
    rows = (
        scoped_query(ArenaADRound, event)
        .order_by(ArenaADRound.number.desc())
        .limit(50)
        .all()
    )
    points = {}
    for se in scoped_query(ArenaScoreEvent, event).filter(
        ArenaScoreEvent.entity_key == key,
        ArenaScoreEvent.component.in_(("ad_attack", "ad_defense", "ad_sla")),
    ):
        rid = (
            se.source_ref.split(":", 1)[0]
            if se.source in ("ad_sla", "ad_defended")
            else None
        )
        if se.source in ("ad_capture", "ad_flag_lost"):
            cap = db.session.get(ArenaADCapture, int(se.source_ref))
            rid = str(cap.round_id) if cap else None
        if rid:
            points.setdefault(int(rid), {}).setdefault(se.component, 0)
            points[int(rid)][se.component] += se.points
    return ok(
        [dict(ad_service.serialize_round(r), points=points.get(r.id, {})) for r in rows]
    )


# ------------------------------------------------------------------- admin


@ad_api.route(f"{A}/services")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def list_services(event):
    return ok(
        [ad_service.serialize_service(s) for s in scoped_query(ArenaADService, event)]
    )


@ad_api.route(f"{A}/services", methods=["POST"])
@event_permission_required(OPERATE)
def create_service(event):
    svc = ad_service.create_service(event, json_body())
    audit("ad.service_create", event_id=event.id, target=("ad_service", svc.id))
    return ok(ad_service.serialize_service(svc), status=201)


@ad_api.route(f"{A}/services/<int:service_id>", methods=["PATCH"])
@event_permission_required(OPERATE)
def update_service(event, service_id):
    svc = scoped_get_or_404(ArenaADService, service_id, event)
    data = json_body()
    for f in ("attack_points", "defense_points", "loss_points", "sla_points"):
        if f in data:
            setattr(svc, f, v.integer(data, f, minimum=0, maximum=10000, required=True))
    if "enabled" in data:
        svc.enabled = bool(v.boolean(data, "enabled"))
    if "description" in data:
        svc.description = v.string(data, "description", max_len=5000)
    db.session.commit()
    audit(
        "ad.service_update",
        event_id=event.id,
        target=("ad_service", svc.id),
        meta={"fields": sorted(data)},
    )
    return ok(ad_service.serialize_service(svc))


@ad_api.route(f"{A}/provision", methods=["POST"])
@event_permission_required(OPERATE)
def provision(event):
    _require_ad(event)
    created = ad_service.provision(event)
    audit(
        "ad.provision",
        event_id=event.id,
        target=("event", event.id),
        meta={"created": created},
    )
    return ok({"created": created}, status=202)


@ad_api.route(f"{A}/rounds")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def rounds(event):
    page, per_page = pagination()
    data, meta = paginate(
        scoped_query(ArenaADRound, event).order_by(ArenaADRound.number.desc()),
        page,
        per_page,
        ad_service.serialize_round,
    )
    return ok(data, meta=meta)


@ad_api.route(f"{A}/rounds/<action>", methods=["POST"])
@event_permission_required(OPERATE)
def round_action(event, action):
    _require_ad(event)
    if action == "open":
        if ad_service.current_round(event) is not None:
            raise ApiError(409, "A round is already running")
        rnd = ad_service.open_round(event)
    elif action == "close":
        rnd = ad_service.current_round(event)
        if rnd is None:
            raise ApiError(409, "No round is running")
        ad_service.close_round(rnd)
    else:
        raise ApiError(404)
    audit(f"ad.round_{action}", event_id=event.id, target=("ad_round", rnd.id))
    return ok(ad_service.serialize_round(rnd))


@ad_api.route(f"{A}/checks")
@event_permission_required(rbac.PERM_AUDIT_READ)
def checks(event):
    q = scoped_query(ArenaADCheck, event)
    if request.args.get("round_id"):
        q = q.filter(
            ArenaADCheck.round_id == v.integer(request.args, "round_id", minimum=1)
        )
    page, per_page = pagination(default_per_page=100, max_per_page=500)
    data, meta = paginate(
        q.order_by(ArenaADCheck.id.desc()),
        page,
        per_page,
        lambda c: {
            "round_id": c.round_id,
            "team_id": c.team_id,
            "service_id": c.service_id,
            "status": c.status,
            "detail": c.detail,
            "created_at": v.iso(c.created_at),
        },
    )
    return ok(data, meta=meta)


@ad_api.route(f"{A}/captures")
@event_permission_required(rbac.PERM_AUDIT_READ)
def captures(event):
    page, per_page = pagination(default_per_page=100, max_per_page=500)
    data, meta = paginate(
        scoped_query(ArenaADCapture, event).order_by(ArenaADCapture.id.desc()),
        page,
        per_page,
        lambda c: {
            "id": c.id,
            "round_id": c.round_id,
            "attacker_team_id": c.attacker_team_id,
            "victim_team_id": c.victim_team_id,
            "service_id": c.service_id,
            "user_id": c.user_id,
            "created_at": v.iso(c.created_at),
        },
    )
    return ok(data, meta=meta)


@ad_api.route(f"{A}/uptime")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def uptime(event):
    return ok(ad_service.uptime(event))


@ad_api.route(f"{A}/team-services/<int:ts_id>/mock-status", methods=["POST"])
@event_permission_required(OPERATE)
def mock_status(event, ts_id):
    """Development only: set the result the MockRunner checker reports."""
    from flask import current_app

    if current_app.extensions["arena"]["settings"].runner_mode != "mock":
        raise ApiError(404)
    ts = scoped_get_or_404(ArenaADTeamService, ts_id, event)
    status_value = v.choice(
        json_body(), "status", ("up", "mumble", "corrupt", "down"), required=True
    )
    ts.mock_status = status_value
    db.session.commit()
    return ok({"id": ts.id, "mock_status": ts.mock_status})
