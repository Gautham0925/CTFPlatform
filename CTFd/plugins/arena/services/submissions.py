"""Jeopardy attempt processing (Phase 4; scoring hooks filled in Phase 5).

Flow for POST …/challenges/<ec>/attempt:
  1. participant + scoring entity (team required in team events)
  2. event must be running; challenge visible, enabled, in window, unlocked
  3. already solved?  -> "already_solved" (no validation oracle, nothing stored)
  4. rate limit (DB-counted, per entity) and max attempts (per entity+challenge)
  5. per-(entity, challenge) cache lock (best effort; correctness is DB-backed)
  6. validate with the native challenge class (chal_class.attempt) or the
     per-entity HMAC flag
  7. ONE transaction: submission row (+ solve row + dynamic value + scoring
     ledger rows from SOLVE_TXN_HOOKS). UNIQUE(event, challenge, entity) on
     arena_solves makes duplicate awards impossible under concurrency.
  8. after commit: POST_COMMIT_HOOKS (scoreboard invalidation, activity) —
     failures there never affect the stored result.
"""

import datetime
import hashlib
import hmac
import logging
import time

from flask import current_app
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError, OperationalError

from CTFd.models import Challenges, db
from CTFd.plugins.arena import arena_cache
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    ArenaEventChallenge,
    ArenaSolve,
    ArenaSubmission,
)
from CTFd.plugins.arena.services import events as event_service
from CTFd.plugins.arena.services import settings as event_settings
from CTFd.plugins.arena.services.challenges import FakeRequest, dynamic_value
from CTFd.plugins.challenges import get_chal_class

log = logging.getLogger("arena.submissions")

# fn(event, ec, entity, solve, now) -> None; runs inside the solve transaction.
SOLVE_TXN_HOOKS = []
# fn(event, ec, entity, result) -> None; runs after commit, errors are logged.
POST_COMMIT_HOOKS = []

MAX_SUBMISSION_LEN = 2048


def _now():
    return datetime.datetime.utcnow()


# ------------------------------------------------------------- availability


def solved_ids(event, entity_key):
    return {
        ec_id
        for (ec_id,) in db.session.query(ArenaSolve.event_challenge_id).filter(
            ArenaSolve.event_id == event.id, ArenaSolve.entity_key == entity_key
        )
    }


def challenge_state(event, ec, solved, now=None):
    """hidden | locked | open | closed  (closed = visible but no submissions)."""
    now = now or _now()
    if not ec.visible:
        return "hidden"
    if ec.available_from and now < ec.available_from:
        return "hidden"
    if ec.prerequisites and not set(ec.prerequisites) <= solved:
        return "locked"
    if not ec.enabled or (ec.available_until and now >= ec.available_until):
        return "closed"
    if not event_service.is_running(event, now):
        return "closed"
    return "open"


def challenges_viewable(event, now=None):
    return event_service.effective_phase(event, now) in (
        "active",
        "paused",
        "completed",
        "archived",
    )


# ------------------------------------------------------------ flag helpers


def _flag_secret(event):
    base = (
        current_app.config.get("ARENA_FLAG_SECRET") or current_app.config["SECRET_KEY"]
    )
    if isinstance(base, str):
        base = base.encode()
    return hmac.new(
        base, f"arena-entity-flags:{event.id}".encode(), hashlib.sha256
    ).digest()


def per_entity_flag(event, ec, entity_key):
    digest = hmac.new(
        _flag_secret(event), f"{ec.id}:{entity_key}".encode(), hashlib.sha256
    )
    return "flag{" + digest.hexdigest()[:32] + "}"


def _validate(event, ec, challenge, submission, entity_key):
    if ec.per_entity_flag:
        expected = per_entity_flag(event, ec, entity_key)
        ok = hmac.compare_digest(expected.encode(), submission.encode())
        return ("correct" if ok else "incorrect"), ("Correct" if ok else "Incorrect")
    chal_class = get_chal_class(challenge.type)
    response = chal_class.attempt(challenge, FakeRequest({"submission": submission}))
    if isinstance(response, tuple):  # legacy (bool, message) plugins
        status, message = ("correct" if response[0] else "incorrect"), response[1]
    else:
        status, message = response.status, response.message
    if status is True:
        status = "correct"
    elif status is False:
        status = "incorrect"
    if status not in ("correct", "incorrect"):
        status = "incorrect"  # 'partial' requires native Partials; unsupported in Arena
    return status, message


# ------------------------------------------------------------------- attempt


def _lock(key):
    """True = acquired, False = held by another request, None = cache down
    (correctness then relies on the DB unique constraint alone)."""
    return arena_cache.add(key, 1, ttl=10)


def _unlock(key):
    arena_cache.delete(key)  # never raises; the lock also expires by TTL


def attempt(event, ec, entity, user, submission, ip=None):
    if not isinstance(submission, str):
        raise ApiError(400, "submission must be a string")
    submission = submission.strip()
    if not submission or len(submission) > MAX_SUBMISSION_LEN:
        raise ApiError(400, "submission must be 1-2048 characters")
    now = _now()
    if not event_service.is_running(event, now):
        raise ApiError(403, "The event is not running", code="event_not_running")
    solved = solved_ids(event, entity.key)
    state = challenge_state(event, ec, solved, now)
    if state == "hidden":
        raise ApiError(404)
    if state == "locked":
        raise ApiError(403, "Challenge is locked", code="locked")
    if state == "closed":
        raise ApiError(403, "Challenge is not accepting submissions", code="closed")
    if ec.id in solved:
        return {
            "status": "already_solved",
            "message": "You already solved this challenge",
        }

    s = event_settings.effective(event)
    window = now - datetime.timedelta(seconds=60)
    recent = (
        db.session.query(func.count(ArenaSubmission.id))
        .filter(
            ArenaSubmission.event_id == event.id,
            ArenaSubmission.entity_key == entity.key,
            ArenaSubmission.status == "incorrect",
            ArenaSubmission.created_at >= window,
        )
        .scalar()
    )
    if recent >= s["submissions_per_minute"]:
        raise ApiError(
            429,
            "You're submitting too fast. Try again in a minute.",
            code="ratelimited",
        )

    max_attempts = (
        ec.max_attempts if ec.max_attempts is not None else s["max_attempts_default"]
    )
    fails = 0
    if max_attempts:
        fails = (
            db.session.query(func.count(ArenaSubmission.id))
            .filter(
                ArenaSubmission.event_id == event.id,
                ArenaSubmission.entity_key == entity.key,
                ArenaSubmission.event_challenge_id == ec.id,
                ArenaSubmission.status == "incorrect",
            )
            .scalar()
        )
        if fails >= max_attempts:
            raise ApiError(403, "No attempts remaining", code="no_attempts")

    lock_key = f"arena:sublock:{event.id}:{entity.key}:{ec.id}"
    locked = _lock(lock_key)
    if locked is False:
        raise ApiError(429, "Another submission is being processed", code="busy")
    try:
        challenge = Challenges.query.filter_by(id=ec.challenge_id).first()
        status, message = _validate(event, ec, challenge, submission, entity.key)
        result = _record(event, ec, entity, user, submission, status, ip, now)
        if result["status"] == "incorrect" and max_attempts:
            result["attempts_remaining"] = max(0, max_attempts - fails - 1)
        result["message"] = (
            message if result["status"] != "already_solved" else result["message"]
        )
    finally:
        if locked:
            _unlock(lock_key)

    for hook in POST_COMMIT_HOOKS:
        try:
            hook(event, ec, entity, result)
        except Exception:  # noqa: BLE001 - never alter a committed result
            log.exception("post-commit hook %s failed", getattr(hook, "__name__", hook))
    return result


RETRYABLE_MYSQL_ERRORS = (1205, 1213)  # lock wait timeout, deadlock
MAX_TXN_RETRIES = 3


def _is_retryable(exc):
    orig = getattr(exc, "orig", None)
    code = orig.args[0] if orig is not None and getattr(orig, "args", None) else None
    return code in RETRYABLE_MYSQL_ERRORS


def _record(event, ec, entity, user, submission, status, ip, now):
    """Persist the attempt. Retries the whole transaction on InnoDB deadlock /
    lock-wait timeout so that a correct answer is never lost."""
    for attempt_no in range(MAX_TXN_RETRIES):
        try:
            return _record_once(event, ec, entity, user, submission, status, ip, now)
        except OperationalError as e:
            db.session.rollback()
            if not _is_retryable(e) or attempt_no == MAX_TXN_RETRIES - 1:
                log.error("submission transaction failed: %s", type(e).__name__)
                raise ApiError(
                    503, "Temporary database contention; please retry", code="retry"
                )
            time.sleep(0.05 * (attempt_no + 1))
    raise ApiError(503, code="retry")  # pragma: no cover


def _record_once(event, ec, entity, user, submission, status, ip, now):
    digest = hashlib.sha256(submission.encode()).hexdigest()

    def make_submission():
        return ArenaSubmission(
            event_id=event.id,
            event_challenge_id=ec.id,
            entity_key=entity.key,
            user_id=user.id,
            status=status,
            provided_hash=digest,
            provided_preview=submission[:64] if status == "incorrect" else None,
            ip=ip,
            created_at=now,
        )

    if status != "correct":
        sub = make_submission()
        db.session.add(sub)
        db.session.commit()
        return {"status": "incorrect", "submission_id": sub.id}

    try:
        # Lock ordering: take the exclusive row lock on the event challenge
        # BEFORE inserting rows whose foreign keys take shared locks on it.
        # (S-lock then X-lock upgrades across transactions deadlock in InnoDB.)
        t = ArenaEventChallenge.__table__
        db.session.execute(
            update(t).where(t.c.id == ec.id).values(solve_count=t.c.solve_count + 1)
        )
        sub = make_submission()
        db.session.add(sub)
        db.session.flush()
        solve = ArenaSolve(
            event_id=event.id,
            event_challenge_id=ec.id,
            entity_key=entity.key,
            user_id=user.id,
            submission_id=sub.id,
            created_at=now,
        )
        db.session.add(solve)
        db.session.flush()  # UNIQUE(event, challenge, entity) enforced here
        db.session.refresh(ec)
        ec.current_value = dynamic_value(ec, ec.solve_count)
        for hook in SOLVE_TXN_HOOKS:
            hook(event, ec, entity, solve, now)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # also undoes the solve_count increment
        return {
            "status": "already_solved",
            "message": "You already solved this challenge",
        }
    return {
        "status": "correct",
        "submission_id": sub.id,
        "solve_id": solve.id,
        "value": ec.current_value,
    }
