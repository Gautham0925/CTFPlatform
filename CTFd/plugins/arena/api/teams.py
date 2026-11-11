"""Event team API: participant routes + organizer routes."""

from flask import request

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
from CTFd.plugins.arena.models import ArenaTeam, ArenaTeamInvite
from CTFd.plugins.arena.services import activity, scoreboard
from CTFd.plugins.arena.services import teams as team_service
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import get_current_user

teams_api = api_blueprint("arena_teams_api", __name__)
E = f"{API_PREFIX}/events/<int:event_id>"
A = f"{API_PREFIX}/admin/events/<int:event_id>"


# --------------------------------------------------------------- participant


@teams_api.route(f"{E}/teams")
@event_participant_required
def list_teams(event):
    q = scoped_query(ArenaTeam, event).filter(
        ArenaTeam.status == "active", ArenaTeam.visibility == "public"
    )
    search = request.args.get("q")
    if search:
        q = q.filter(ArenaTeam.name.contains(search[:64]))
    page, per_page = pagination()
    data, meta = paginate(
        q.order_by(ArenaTeam.name), page, per_page, team_service.serialize
    )
    return ok(data, meta=meta)


@teams_api.route(f"{E}/teams/<int:team_id>")
@event_participant_required
def get_team(event, team_id):
    team = scoped_get_or_404(ArenaTeam, team_id, event)
    me = get_current_user()
    is_member = team_service.membership(event, me.id)
    is_member = is_member is not None and is_member.team_id == team.id
    if (team.visibility == "private" or team.status != "active") and not is_member:
        raise ApiError(404)
    return ok(team_service.serialize(team, include_members=True))


@teams_api.route(f"{E}/teams", methods=["POST"])
@ratelimit(method="POST", limit=10, interval=60, key_prefix="arena-team-create")
@event_participant_required
def create_team(event):
    team, code = team_service.create_team(event, get_current_user(), json_body())
    activity.record(
        "team.create",
        user_id=get_current_user().id,
        event_id=event.id,
        target=("team", team.id),
    )
    data = team_service.serialize(team, include_members=True, private=True)
    data["join_code"] = code  # shown once; regenerate to obtain a new one
    return ok(data, status=201)


@teams_api.route(f"{E}/my-team")
@event_participant_required
def my_team(event):
    team = team_service.team_of(event, get_current_user().id)
    if team is None:
        return ok(None)
    is_captain = team.captain_user_id == get_current_user().id
    return ok(team_service.serialize(team, include_members=True, private=is_captain))


@teams_api.route(f"{E}/my-team", methods=["PATCH"])
@event_participant_required
def update_my_team(event):
    team = team_service.update_team(event, get_current_user(), json_body())
    return ok(team_service.serialize(team, include_members=True, private=True))


@teams_api.route(f"{E}/my-team/join-code", methods=["POST"])
@event_participant_required
def regenerate_code(event):
    team = team_service._require_captain(event, get_current_user())
    team_service._require_active(team)
    code = team_service.regenerate_join_code(team)
    activity.record(
        "team.join_code_regenerated",
        user_id=get_current_user().id,
        event_id=event.id,
        target=("team", team.id),
    )
    return ok({"join_code": code})


@teams_api.route(f"{E}/teams/join", methods=["POST"])
@ratelimit(method="POST", limit=10, interval=60, key_prefix="arena-team-join")
@event_participant_required
def join_team(event):
    team = team_service.join_with_code(
        event, get_current_user(), json_body().get("join_code")
    )
    activity.record(
        "team.join",
        user_id=get_current_user().id,
        event_id=event.id,
        target=("team", team.id),
    )
    return ok(team_service.serialize(team, include_members=True))


@teams_api.route(f"{E}/my-team/leave", methods=["POST"])
@event_participant_required
def leave(event):
    team = team_service.team_of(event, get_current_user().id)
    team_service.leave_team(event, get_current_user())
    activity.record(
        "team.leave",
        user_id=get_current_user().id,
        event_id=event.id,
        target=("team", team.id if team else None),
    )
    return ok({"left": True})


@teams_api.route(f"{E}/my-team/members/<int:user_id>/remove", methods=["POST"])
@event_participant_required
def kick(event, user_id):
    team = team_service.kick_member(event, get_current_user(), user_id)
    activity.record(
        "team.kick",
        user_id=get_current_user().id,
        event_id=event.id,
        target=("user", user_id),
        meta={"team": team.id},
    )
    return ok(team_service.serialize(team, include_members=True, private=True))


@teams_api.route(f"{E}/my-team/captain", methods=["POST"])
@event_participant_required
def transfer(event):
    team = team_service._require_captain(event, get_current_user())
    new_id = v.integer(json_body(), "user_id", required=True, minimum=1)
    team_service.transfer_captain(event, team, new_id)
    activity.record(
        "team.captain_transfer",
        user_id=get_current_user().id,
        event_id=event.id,
        target=("team", team.id),
        meta={"to": new_id},
    )
    return ok(team_service.serialize(team, include_members=True))


@teams_api.route(f"{E}/my-team/invites", methods=["POST"])
@ratelimit(method="POST", limit=30, interval=60, key_prefix="arena-team-invite")
@event_participant_required
def send_invite(event):
    user_id = v.integer(json_body(), "user_id", required=True, minimum=1)
    inv = team_service.invite(event, get_current_user(), user_id)
    return ok(team_service.serialize_invite(inv), status=201)


@teams_api.route(f"{E}/my-team/invites/<int:invite_id>", methods=["DELETE"])
@event_participant_required
def cancel_invite(event, invite_id):
    team_service.cancel_invite(event, get_current_user(), invite_id)
    return ok({"cancelled": invite_id})


@teams_api.route(f"{E}/my-invites")
@event_participant_required
def my_invites(event):
    rows = scoped_query(ArenaTeamInvite, event).filter_by(
        user_id=get_current_user().id, status="pending"
    )
    return ok([team_service.serialize_invite(i) for i in rows])


@teams_api.route(f"{E}/my-invites/<int:invite_id>/<action>", methods=["POST"])
@event_participant_required
def respond(event, invite_id, action):
    if action not in ("accept", "reject"):
        raise ApiError(404)
    team = team_service.respond_invite(
        event, get_current_user(), invite_id, action == "accept"
    )
    if team is not None:
        activity.record(
            "team.join",
            user_id=get_current_user().id,
            event_id=event.id,
            target=("team", team.id),
            meta={"via": "invite"},
        )
        return ok(team_service.serialize(team, include_members=True))
    return ok(None)


# ----------------------------------------------------------------- organizer


@teams_api.route(f"{A}/teams")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def admin_list(event):
    q = scoped_query(ArenaTeam, event)
    status = request.args.get("status")
    if status:
        q = q.filter(ArenaTeam.status == status)
    page, per_page = pagination()
    data, meta = paginate(
        q.order_by(ArenaTeam.id),
        page,
        per_page,
        lambda t: team_service.serialize(t, include_members=True),
    )
    return ok(data, meta=meta)


@teams_api.route(f"{A}/teams/<int:team_id>")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def admin_get(event, team_id):
    team = scoped_get_or_404(ArenaTeam, team_id, event)
    return ok(team_service.serialize(team, include_members=True, private=True))


@teams_api.route(f"{A}/teams/<int:team_id>/<action>", methods=["POST"])
@event_permission_required(rbac.PERM_TEAMS_MANAGE)
def admin_action(event, team_id, action):
    team = scoped_get_or_404(ArenaTeam, team_id, event)
    if action == "ban":
        team_service.admin_set_status(event, team, "banned")
    elif action == "unban":
        team_service.admin_set_status(event, team, "active")
    elif action == "disband":
        team_service.disband(event, team)
        scoreboard.invalidate(event.id)
        audit("team.disband", event_id=event.id, target=("team", team_id))
        return ok({"disbanded": team_id})
    elif action == "captain":
        team_service.transfer_captain(
            event,
            team,
            v.integer(json_body(), "user_id", required=True, minimum=1),
            admin=True,
        )
    elif action == "members":
        data = json_body()
        team_service.admin_add_member(
            event,
            team,
            v.integer(data, "user_id", required=True, minimum=1),
            force=bool(v.boolean(data, "force")),
        )
    else:
        raise ApiError(404)
    audit(f"team.{action}", event_id=event.id, target=("team", team.id))
    scoreboard.invalidate(event.id)
    return ok(team_service.serialize(team, include_members=True))


@teams_api.route(
    f"{A}/teams/<int:team_id>/members/<int:user_id>/remove", methods=["POST"]
)
@event_permission_required(rbac.PERM_TEAMS_MANAGE)
def admin_remove(event, team_id, user_id):
    team = scoped_get_or_404(ArenaTeam, team_id, event)
    team_service._remove_member(event, team, user_id, allow_captain_succession=True)
    audit(
        "team.member_remove",
        event_id=event.id,
        target=("user", user_id),
        meta={"team": team_id},
    )
    return ok({"removed": user_id})
