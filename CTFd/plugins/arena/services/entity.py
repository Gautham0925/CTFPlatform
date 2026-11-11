"""Scoring identity ("entity") resolution.

Every score-bearing Arena row stores (event_id, entity_key):
  team events:       entity_key = "t:<arena_team_id>"
  individual events: entity_key = "u:<user_id>"
The entity is always derived server-side from the authenticated user and
the event; it is never accepted from the client.
"""

from dataclasses import dataclass
from typing import Optional

from CTFd.plugins.arena.api import ApiError


@dataclass(frozen=True)
class Entity:
    key: str
    team_id: Optional[int]
    user_id: int

    @property
    def is_team(self):
        return self.team_id is not None


def team_key(team_id):
    return f"t:{int(team_id)}"


def user_key(user_id):
    return f"u:{int(user_id)}"


def resolve(event, user, require=True):
    from CTFd.plugins.arena.services import teams as team_service

    if event.team_mode == "team":
        team = team_service.team_of(event, user.id)
        if team is None:
            if require:
                raise ApiError(403, "Join or create a team first", code="no_team")
            return None
        if team.status != "active":
            raise ApiError(
                403, f"Your team is {team.status}", code=f"team_{team.status}"
            )
        return Entity(team_key(team.id), team.id, user.id)
    return Entity(user_key(user.id), None, user.id)


def parse(key):
    kind, _, ident = (key or "").partition(":")
    if kind not in ("t", "u") or not ident.isdigit():
        raise ValueError(f"invalid entity key {key!r}")
    return kind, int(ident)
