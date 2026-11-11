"""Event challenge management (Phase 4).

Native challenge content is created and edited through the registered native
challenge class (``CHALLENGE_CLASSES``) so custom challenge types keep working.
Arena then *forces* two invariants on every write:

* ``challenge.module_id = event.sealed_module_id`` (native endpoints → 404)
* ``challenge.state = "hidden"`` (defence in depth if a module were unsealed)

Fields that must never be set from organizer input (id, type changes,
module_id, state, next_id, requirements) are stripped before reaching native
code.
"""

import math
import re

from sqlalchemy.exc import IntegrityError

from CTFd.models import ChallengeFiles, Challenges, Flags, Hints, Tags, db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    DECAY_FUNCTIONS,
    DIFFICULTIES,
    SCORING_MODES,
    ArenaEventChallenge,
    ArenaSolve,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES, get_chal_class
from CTFd.plugins.flags import FLAG_CLASSES

PROTECTED_NATIVE = {
    "id",
    "type",
    "module_id",
    "state",
    "next_id",
    "requirements",
    "logic",
}
NATIVE_TEXT_FIELDS = {
    "name": 80,
    "category": 80,
    "description": 65535,
    "connection_info": 2000,
    "attribution": 2000,
}


class FakeRequest:
    """Minimal request object for native challenge-class methods.

    Native create/update/attempt only read ``request.form`` /
    ``request.get_json()`` (and get_ip() for writes, which Arena never calls).
    """

    def __init__(self, data):
        self.form = dict(data)

    def get_json(self, *a, **k):
        return self.form


# ---------------------------------------------------------------- dynamic value


def dynamic_value(ec, solve_count):
    """Same formulas as CTFd/plugins/challenges/decay.py, but with an
    event-scoped solve count (native decay counts global native Solves)."""
    if ec.scoring != "dynamic":
        return ec.points
    initial, minimum = ec.points, ec.minimum_points or 0
    decay = ec.decay or 1
    n = max(0, solve_count - 1)  # first solver gets full value
    if ec.decay_function == "linear":
        value = math.ceil(initial - decay * n)
    else:
        value = math.ceil(((minimum - initial) / (decay**2)) * (n**2) + initial)
    return max(minimum, int(value))


# ------------------------------------------------------------------- helpers


def _arena_fields(ec, data, creating):
    if "visible" in data:
        ec.visible = bool(v.boolean(data, "visible"))
    if "enabled" in data:
        ec.enabled = bool(v.boolean(data, "enabled"))
    if "difficulty" in data:
        ec.difficulty = (
            v.choice(data, "difficulty", DIFFICULTIES) if data["difficulty"] else None
        )
    if "position" in data:
        ec.position = v.integer(
            data, "position", minimum=0, maximum=100000, required=True
        )
    if "scoring" in data:
        ec.scoring = v.choice(data, "scoring", SCORING_MODES, required=True)
    if "points" in data or creating:
        ec.points = (
            v.integer(data, "points", minimum=0, maximum=100000, required=creating) or 0
        )
    if "minimum_points" in data:
        ec.minimum_points = v.integer(data, "minimum_points", minimum=0, maximum=100000)
    if "decay" in data:
        ec.decay = v.integer(data, "decay", minimum=1, maximum=100000)
    if "decay_function" in data:
        ec.decay_function = v.choice(
            data, "decay_function", DECAY_FUNCTIONS, required=True
        )
    for f in ("available_from", "available_until"):
        if f in data:
            setattr(ec, f, v.dt(data, f))
    if "max_attempts" in data:
        ec.max_attempts = v.integer(data, "max_attempts", minimum=0, maximum=10000)
    if "per_entity_flag" in data:
        ec.per_entity_flag = bool(v.boolean(data, "per_entity_flag"))
    if "image_id" in data:
        image_id = v.integer(data, "image_id", minimum=1)
        if image_id is not None:
            from CTFd.plugins.arena.models import ArenaImage

            img = db.session.get(ArenaImage, image_id)
            if img is None or not img.enabled:
                raise v.ValidationError("image_id", "unknown or disabled image")
        ec.image_id = image_id
    if "prerequisites" in data:
        prereqs = data["prerequisites"] or []
        if not isinstance(prereqs, list) or not all(
            isinstance(x, int) for x in prereqs
        ):
            raise v.ValidationError(
                "prerequisites", "must be a list of event challenge ids"
            )
        ec.prerequisites = sorted(set(prereqs)) or None
    if ec.scoring == "dynamic":
        if ec.minimum_points is None or ec.decay is None:
            raise v.ValidationError(
                "decay", "dynamic scoring needs minimum_points and decay"
            )
        if ec.minimum_points > ec.points:
            raise v.ValidationError("minimum_points", "must be <= points")
    if (
        ec.available_from
        and ec.available_until
        and ec.available_from >= ec.available_until
    ):
        raise v.ValidationError("available_until", "must be after available_from")


def _validate_prerequisites(event, ec):
    if not ec.prerequisites:
        return
    if ec.id in ec.prerequisites:
        raise v.ValidationError("prerequisites", "a challenge cannot require itself")
    found = {
        i
        for (i,) in db.session.query(ArenaEventChallenge.id).filter(
            ArenaEventChallenge.event_id == event.id,
            ArenaEventChallenge.id.in_(ec.prerequisites),
        )
    }
    missing = sorted(set(ec.prerequisites) - found)
    if missing:
        raise v.ValidationError(
            "prerequisites", f"unknown challenges in this event: {missing}"
        )


def _native_data(data, creating):
    out = {}
    for f, n in NATIVE_TEXT_FIELDS.items():
        if f in data:
            out[f] = v.string(
                data,
                f,
                max_len=n,
                required=(creating and f == "name"),
                allow_empty=(f != "name"),
            )
    extra = data.get("type_data") or {}
    if not isinstance(extra, dict):
        raise v.ValidationError("type_data", "must be an object")
    for k, val in extra.items():
        if (
            k in PROTECTED_NATIVE
            or k in NATIVE_TEXT_FIELDS
            or not re.match(r"^[a-z_]{1,40}$", str(k))
        ):
            raise v.ValidationError("type_data", f"field {k!r} is not allowed")
        out[k] = val
    return out


def _seal(event, challenge):
    from CTFd.plugins.arena.services.events import ensure_sealed_module

    module_id = ensure_sealed_module(event)
    if not module_id:  # pragma: no cover - ensure_sealed_module always returns an id
        raise ApiError(500, "Event is not sealed")
    challenge.module_id = module_id
    challenge.state = "hidden"
    challenge.logic = "any"
    challenge.requirements = None
    challenge.next_id = None


def _validate_flag(data):
    ftype = v.choice(data, "type", tuple(FLAG_CLASSES), required=False) or "static"
    content = v.string(data, "content", required=True, max_len=4000, allow_empty=False)
    case_insensitive = bool(v.boolean(data, "case_insensitive"))
    if ftype == "regex":
        try:
            re.compile(content)
        except re.error as e:
            raise v.ValidationError("content", f"invalid regex: {e}")
    return Flags(
        type=ftype, content=content, data="case_insensitive" if case_insensitive else ""
    )


def _validate_hint(data, creating=True):
    content = v.string(
        data, "content", required=creating, max_len=20000, allow_empty=False
    )
    return {
        "content": content,
        "title": v.string(data, "title", max_len=80),
        "cost": v.integer(data, "cost", minimum=0, maximum=100000) or 0,
    }


# ------------------------------------------------------------------ create


def create(event, data):
    ctype = v.string(data, "type", max_len=80) or "standard"
    if ctype not in CHALLENGE_CLASSES:
        raise v.ValidationError(
            "type",
            f"unknown challenge type (available: {', '.join(sorted(CHALLENGE_CLASSES))})",
        )
    native = _native_data(data, creating=True)
    native.setdefault("category", "Miscellaneous")
    native["value"] = v.integer(
        data, "points", minimum=0, maximum=100000, required=True
    )
    if ctype == "dynamic":
        native.setdefault("initial", native["value"])
        native.setdefault(
            "minimum", v.integer(data, "minimum_points", minimum=0) or native["value"]
        )
        native.setdefault("decay", v.integer(data, "decay", minimum=1) or 1)
        native.setdefault("function", "logarithmic")
    from CTFd.plugins.arena.services.events import ensure_sealed_module

    native["type"] = ctype  # native admin API passes type too (polymorphic identity)
    native["state"] = "hidden"
    native["module_id"] = ensure_sealed_module(event)  # sealed from the first INSERT

    flags = [_validate_flag(f) for f in (data.get("flags") or [])]
    hints = [_validate_hint(h) for h in (data.get("hints") or [])]
    tags = v.string_list(data, "tags", max_items=20, max_len=80) or []

    ec = ArenaEventChallenge(
        event_id=event.id,
        visible=False,
        enabled=True,
        scoring="static",
        decay_function="logarithmic",
        position=0,
    )
    _arena_fields(ec, data, creating=True)

    chal_class = get_chal_class(ctype)
    try:
        challenge = chal_class.create(FakeRequest(native))
    except Exception as e:  # native classes raise various exceptions on bad input
        db.session.rollback()
        raise ApiError(400, f"Invalid challenge data: {type(e).__name__}")
    _seal(event, challenge)
    ec.challenge_id = challenge.id
    ec.current_value = ec.points
    db.session.add(ec)
    for f in flags:
        f.challenge_id = challenge.id
        db.session.add(f)
    for h in hints:
        db.session.add(Hints(challenge_id=challenge.id, **h))
    for t in tags:
        db.session.add(Tags(challenge_id=challenge.id, value=t))
    db.session.flush()
    _validate_prerequisites(event, ec)
    db.session.commit()
    return ec


# ------------------------------------------------------------------ update


def native_challenge(ec):
    return Challenges.query.filter_by(id=ec.challenge_id).first()


def update(event, ec, data):
    challenge = native_challenge(ec)
    if "type" in data and data["type"] != challenge.type:
        raise ApiError(409, "Challenge type cannot be changed; create a new challenge")
    native = _native_data(data, creating=False)
    if "points" in data:
        native["value"] = v.integer(
            data, "points", minimum=0, maximum=100000, required=True
        )
    if native:
        chal_class = get_chal_class(challenge.type)
        try:
            chal_class.update(challenge, FakeRequest(native))
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            raise ApiError(400, f"Invalid challenge data: {type(e).__name__}")
    _arena_fields(ec, data, creating=False)
    _validate_prerequisites(event, ec)
    challenge = native_challenge(ec)
    _seal(event, challenge)
    ec.current_value = dynamic_value(ec, ec.solve_count)
    if "tags" in data:
        Tags.query.filter_by(challenge_id=challenge.id).delete()
        for t in v.string_list(data, "tags", max_items=20, max_len=80) or []:
            db.session.add(Tags(challenge_id=challenge.id, value=t))
    db.session.commit()
    return ec


def delete(event, ec):
    if ArenaSolve.query.filter_by(event_id=event.id, event_challenge_id=ec.id).first():
        raise ApiError(409, "Challenge has solves; disable or hide it instead")
    challenge = native_challenge(ec)
    db.session.delete(ec)
    db.session.flush()
    if challenge is not None:
        get_chal_class(challenge.type).delete(
            challenge
        )  # native cleanup (files, flags, hints)
    db.session.commit()


def delete_all_for_event(event):
    """Event delete hook (draft events only, no solves)."""
    for ec in ArenaEventChallenge.query.filter_by(event_id=event.id).all():
        delete(event, ec)


# ----------------------------------------------------------- flags/hints/files


def add_flag(ec, data):
    flag = _validate_flag(data)
    flag.challenge_id = ec.challenge_id
    db.session.add(flag)
    db.session.commit()
    return flag


def delete_flag(ec, flag_id):
    n = Flags.query.filter_by(id=flag_id, challenge_id=ec.challenge_id).delete()
    db.session.commit()
    if not n:
        raise ApiError(404, "Flag not found")


def add_hint(ec, data):
    hint = Hints(challenge_id=ec.challenge_id, **_validate_hint(data))
    db.session.add(hint)
    db.session.commit()
    return hint


def update_hint(ec, hint_id, data):
    hint = Hints.query.filter_by(id=hint_id, challenge_id=ec.challenge_id).first()
    if hint is None:
        raise ApiError(404, "Hint not found")
    vals = _validate_hint(data, creating=False)
    for k in ("content", "title", "cost"):
        if k in data and vals[k] is not None:
            setattr(hint, k, vals[k])
    db.session.commit()
    return hint


def delete_hint(ec, hint_id):
    n = Hints.query.filter_by(id=hint_id, challenge_id=ec.challenge_id).delete()
    db.session.commit()
    if not n:
        raise ApiError(404, "Hint not found")


MAX_FILE_BYTES = 100 * 1024 * 1024


def add_file(ec, file_storage):
    from CTFd.utils.uploads import upload_file

    if file_storage is None or not file_storage.filename:
        raise ApiError(400, "file is required")
    name = file_storage.filename
    if "/" in name or "\\" in name or name.startswith(".") or len(name) > 128:
        raise ApiError(400, "invalid file name")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_FILE_BYTES:
        raise ApiError(413, "file too large")
    return upload_file(
        file=file_storage, challenge_id=ec.challenge_id, type="challenge"
    )


def delete_file(ec, file_id):
    from CTFd.utils.uploads import delete_file as native_delete

    f = ChallengeFiles.query.filter_by(id=file_id, challenge_id=ec.challenge_id).first()
    if f is None:
        raise ApiError(404, "File not found")
    native_delete(f.id)


# ------------------------------------------------------------------- clone


def clone(src_event, ec, target_event):
    """Copy a challenge (content + flags + hints + tags) into another event.
    Files are re-linked by copying their storage location record."""
    src = native_challenge(ec)
    data = {
        "type": src.type,
        "name": src.name,
        "category": src.category,
        "description": src.description,
        "connection_info": src.connection_info,
        "attribution": src.attribution,
        "points": ec.points,
        "scoring": ec.scoring,
        "minimum_points": ec.minimum_points,
        "decay": ec.decay,
        "decay_function": ec.decay_function,
        "difficulty": ec.difficulty,
        "max_attempts": ec.max_attempts,
        "per_entity_flag": ec.per_entity_flag,
        "visible": False,
        "flags": [
            {
                "type": f.type,
                "content": f.content,
                "case_insensitive": f.data == "case_insensitive",
            }
            for f in Flags.query.filter_by(challenge_id=src.id)
        ],
        "hints": [
            {"content": h.content, "title": h.title, "cost": h.cost}
            for h in Hints.query.filter_by(challenge_id=src.id)
        ],
        "tags": [t.value for t in Tags.query.filter_by(challenge_id=src.id)],
    }
    new = create(target_event, data)
    for f in ChallengeFiles.query.filter_by(challenge_id=src.id):
        db.session.add(
            ChallengeFiles(
                challenge_id=new.challenge_id, location=f.location, sha1sum=f.sha1sum
            )
        )
    try:
        db.session.commit()
    except IntegrityError:
        # files.location is unique: shared storage objects cannot be linked twice.
        db.session.rollback()
    return new


# ------------------------------------------------------------- serialization


def serialize_admin(ec, include_secrets=False):
    c = native_challenge(ec)
    data = {
        "id": ec.id,
        "event_id": ec.event_id,
        "challenge_id": ec.challenge_id,
        "type": c.type if c else None,
        "name": c.name if c else None,
        "category": c.category if c else None,
        "description": c.description if c else None,
        "connection_info": c.connection_info if c else None,
        "attribution": c.attribution if c else None,
        "visible": ec.visible,
        "enabled": ec.enabled,
        "difficulty": ec.difficulty,
        "position": ec.position,
        "scoring": ec.scoring,
        "points": ec.points,
        "minimum_points": ec.minimum_points,
        "decay": ec.decay,
        "decay_function": ec.decay_function,
        "current_value": ec.current_value,
        "solve_count": ec.solve_count,
        "available_from": v.iso(ec.available_from),
        "available_until": v.iso(ec.available_until),
        "max_attempts": ec.max_attempts,
        "prerequisites": ec.prerequisites or [],
        "per_entity_flag": ec.per_entity_flag,
        "image_id": ec.image_id,
        "tags": [t.value for t in Tags.query.filter_by(challenge_id=ec.challenge_id)],
        "hints": [
            {"id": h.id, "title": h.title, "cost": h.cost, "content": h.content}
            for h in Hints.query.filter_by(challenge_id=ec.challenge_id)
        ],
        "files": [
            {"id": f.id, "name": f.location.rsplit("/", 1)[-1]}
            for f in ChallengeFiles.query.filter_by(challenge_id=ec.challenge_id)
        ],
    }
    if include_secrets:
        data["flags"] = [
            {
                "id": f.id,
                "type": f.type,
                "content": f.content,
                "case_insensitive": f.data == "case_insensitive",
            }
            for f in Flags.query.filter_by(challenge_id=ec.challenge_id)
        ]
    return data
