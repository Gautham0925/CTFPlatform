"""Analytics, activity and export API."""

from flask import Response, request, stream_with_context

from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import API_PREFIX, ApiError, api_blueprint, ok
from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.core.authz import (
    event_participant_required,
    event_permission_required,
)
from CTFd.plugins.arena.core.context import scoped_query
from CTFd.plugins.arena.core.ratelimit import ratelimit
from CTFd.plugins.arena.models import ArenaActivity, ArenaTeamMember
from CTFd.plugins.arena.services import activity as activity_service
from CTFd.plugins.arena.services import analytics
from CTFd.plugins.arena.services import teams as team_service
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import get_current_user

analytics_api = api_blueprint("arena_analytics_api", __name__)
E = f"{API_PREFIX}/events/<int:event_id>"
A = f"{API_PREFIX}/admin/events/<int:event_id>"
READ = rbac.PERM_ANALYTICS_READ


def _keyset(q, model):
    """Keyset pagination: ?before_id=<id>&limit=<n> (newest first)."""
    limit = v.integer(request.args, "limit", minimum=1, maximum=200) or 50
    before = v.integer(request.args, "before_id", minimum=1)
    if before:
        q = q.filter(model.id < before)
    rows = q.order_by(model.id.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, {
        "next_before_id": rows[-1].id if more and rows else None,
        "limit": limit,
    }


@analytics_api.route(f"{A}/analytics/overview")
@event_permission_required(READ)
def overview(event):
    return ok(analytics.overview(event))


@analytics_api.route(f"{A}/analytics/timeline")
@event_permission_required(READ)
def timeline(event):
    hours = v.integer(request.args, "hours", minimum=1, maximum=720) or 48
    return ok(analytics.timeline(event, hours=hours))


@analytics_api.route(f"{A}/analytics/challenges")
@event_permission_required(READ)
def challenges(event):
    return ok(analytics.challenge_report(event))


@analytics_api.route(f"{A}/analytics/refresh", methods=["POST"])
@ratelimit(method="POST", limit=6, interval=60, key_prefix="arena-analytics-refresh")
@event_permission_required(READ)
def refresh(event):
    from CTFd.plugins.arena.services import jobs

    job = jobs.enqueue(
        "analytics.rollup",
        {"event_id": event.id},
        dedupe_key=f"analytics:{event.id}",
        event_id=event.id,
        priority=60,
    )
    return ok({"job_id": job.id, "status": job.status}, status=202)


@analytics_api.route(f"{A}/activity")
@event_permission_required(rbac.PERM_AUDIT_READ)
def event_activity(event):
    q = scoped_query(ArenaActivity, event)
    if request.args.get("action"):
        q = q.filter(ArenaActivity.action == request.args["action"][:48])
    if request.args.get("user_id"):
        q = q.filter(
            ArenaActivity.actor_user_id == v.integer(request.args, "user_id", minimum=1)
        )
    if request.args.get("actor_type"):
        q = q.filter(ArenaActivity.actor_type == request.args["actor_type"][:16])
    rows, meta = _keyset(q, ArenaActivity)
    return ok([activity_service.serialize(r) for r in rows], meta=meta)


@analytics_api.route(f"{A}/export/<kind>.csv")
@ratelimit(method="GET", limit=20, interval=60, key_prefix="arena-export")
@event_permission_required(READ)
def export(event, kind):
    if kind not in analytics.EXPORT_KINDS:
        raise ApiError(404)
    if kind in ("submissions", "activity") and not rbac.has_permission(
        get_current_user(), rbac.PERM_AUDIT_READ, event.id
    ):
        raise ApiError(403)
    limit = v.integer(request.args, "limit", minimum=1, maximum=500000) or 100000
    header, rows = analytics.export_rows(event, kind, limit=limit)
    audit("export.csv", event_id=event.id, target=("export", kind))
    resp = Response(
        stream_with_context(analytics.stream_csv(header, rows)), mimetype="text/csv"
    )
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{event.slug}-{kind}.csv"'
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ------------------------------------------------------------ participants


@analytics_api.route(f"{E}/my-activity")
@event_participant_required
def my_activity(event):
    q = scoped_query(ArenaActivity, event).filter(
        ArenaActivity.actor_user_id == get_current_user().id
    )
    rows, meta = _keyset(q, ArenaActivity)
    return ok(
        [activity_service.serialize(r, include_actor=False) for r in rows], meta=meta
    )


@analytics_api.route(f"{E}/my-team/activity")
@event_participant_required
def team_activity(event):
    team = team_service.team_of(event, get_current_user().id)
    if team is None:
        return ok([])
    member_ids = [
        m.user_id
        for m in ArenaTeamMember.query.filter_by(team_id=team.id, event_id=event.id)
    ]
    visible = (
        "team.create",
        "team.join",
        "team.leave",
        "team.kick",
        "team.captain_transfer",
        "challenge.solve",
        "hint.unlock",
        "lab.allocate",
        "instance.allocate",
    )
    q = scoped_query(ArenaActivity, event).filter(
        ArenaActivity.actor_user_id.in_(member_ids or [0]),
        ArenaActivity.action.in_(visible),
    )
    rows, meta = _keyset(q, ArenaActivity)
    return ok([activity_service.serialize(r) for r in rows], meta=meta)
