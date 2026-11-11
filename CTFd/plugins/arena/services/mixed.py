"""Mixed-mode scoring configuration and score history (Phases 5 & 10).

* Configuration is versioned and immutable (arena_scoring_configs): a change
  creates version N+1 with a mandatory reason; nothing is overwritten.
* Standings are always recomputed from stored source rows with the active
  version, so a change applies consistently to the whole event. To keep the
  history auditable, a snapshot of the standings is stored immediately before
  and after every change (kind config_before/config_after), plus after every
  A&D round, hourly while running, and a final snapshot.
"""

import datetime

from CTFd.models import db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError

DEFAULTS = {"jeopardy_weight": 1.0, "ad_weight": 1.0, "tie_break": "time"}
TIE_BREAKS = ("time", "solves", "attack")
DEFAULT_WEIGHTS = {"jeopardy": 1.0, "ad": 1.0}


def active_config(event):
    from CTFd.plugins.arena.models import ArenaScoringConfig

    cfg = None
    if event.id is not None:
        cfg = (
            ArenaScoringConfig.query.filter_by(event_id=event.id)
            .order_by(ArenaScoringConfig.version.desc())
            .first()
        )
    params = dict(DEFAULTS)
    if cfg is not None:
        params.update(cfg.params or {})
    params["version"] = cfg.version if cfg else 0
    if event.type != "mixed":  # weights only matter when both modes run
        params["jeopardy_weight"] = params["ad_weight"] = 1.0
    return params


def active_weights(event):
    cfg = active_config(event)
    return {"jeopardy": float(cfg["jeopardy_weight"]), "ad": float(cfg["ad_weight"])}


def validate_params(event, data):
    out = {}
    for key in ("jeopardy_weight", "ad_weight"):
        if key in data:
            val = data[key]
            if (
                isinstance(val, bool)
                or not isinstance(val, (int, float))
                or not 0 <= val <= 10
            ):
                raise v.ValidationError(key, "must be a number between 0 and 10")
            if event.type != "mixed" and float(val) != 1.0:
                raise v.ValidationError(
                    key, "weights can only be changed for mixed events"
                )
            out[key] = float(val)
    if "tie_break" in data:
        out["tie_break"] = v.choice(data, "tie_break", TIE_BREAKS, required=True)
    unknown = set(data) - {"jeopardy_weight", "ad_weight", "tie_break", "reason"}
    if unknown:
        raise v.ValidationError("params", f"unknown keys: {', '.join(sorted(unknown))}")
    if not out:
        raise v.ValidationError("params", "nothing to change")
    return out


def create_config(event, data, actor):
    from CTFd.plugins.arena.models import ArenaScoringConfig
    from CTFd.plugins.arena.services import scoreboard

    reason = v.string(
        data, "reason", required=True, max_len=255, min_len=3, allow_empty=False
    )
    changes = validate_params(event, data)
    current = active_config(event)
    params = {k: current[k] for k in DEFAULTS}
    params.update(changes)
    before = snapshot(
        event, "config_before", ref=f"v{current['version']}", commit=False
    )
    cfg = ArenaScoringConfig(
        event_id=event.id,
        version=current["version"] + 1,
        params=params,
        reason=reason,
        created_by=actor.id if actor else None,
    )
    db.session.add(cfg)
    try:
        db.session.flush()
    except Exception:  # UNIQUE(event, version): a concurrent change won
        db.session.rollback()
        raise ApiError(
            409, "Scoring configuration changed concurrently; retry", code="conflict"
        )
    after = snapshot(event, "config_after", ref=f"v{cfg.version}", commit=False)
    db.session.commit()
    scoreboard.invalidate(event.id)
    return cfg, _diff(before.payload, after.payload)


def _diff(before, after):
    b = {r["key"]: r for r in before}
    a = {r["key"]: r for r in after}
    changes = []
    for key in sorted(set(a) | set(b)):
        old, new = b.get(key, {}), a.get(key, {})
        if old.get("score") != new.get("score") or old.get("rank") != new.get("rank"):
            changes.append(
                {
                    "key": key,
                    "score": [old.get("score"), new.get("score")],
                    "rank": [old.get("rank"), new.get("rank")],
                }
            )
    return changes


def _condense(rows, limit=500):
    return [
        {
            "key": r["key"],
            "name": r["name"],
            "score": r["score"],
            "rank": r["rank"],
            "jeopardy": r["breakdown"]["jeopardy"],
            "attack_defense": r["breakdown"]["attack_defense"],
            "adjustments": r["breakdown"]["adjustments"],
        }
        for r in rows[:limit]
    ]


def snapshot(event, kind, ref=None, commit=True):
    from CTFd.plugins.arena.models import ArenaScoreSnapshot
    from CTFd.plugins.arena.services import scoring

    rows = scoring.compute_standings(event, scope="admin")
    snap = ArenaScoreSnapshot(
        event_id=event.id,
        kind=kind,
        ref=ref,
        config_version=active_config(event)["version"],
        payload=_condense(rows),
        created_at=datetime.datetime.utcnow(),
    )
    db.session.add(snap)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return snap


def serialize_config(cfg):
    return {
        "version": cfg.version,
        "params": cfg.params,
        "reason": cfg.reason,
        "created_by": cfg.created_by,
        "created_at": v.iso(cfg.created_at),
    }


def serialize_snapshot(s, include_payload=False):
    data = {
        "id": s.id,
        "kind": s.kind,
        "ref": s.ref,
        "config_version": s.config_version,
        "created_at": v.iso(s.created_at),
        "entities": len(s.payload or []),
    }
    if include_payload:
        data["standings"] = s.payload
    return data
