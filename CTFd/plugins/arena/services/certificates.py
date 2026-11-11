"""Optional certificates (Phase 12).

Eligibility is computed only from stored facts after the event completed:
    participation  approved registrant with >= criteria.min_solves solves
                   (default 1) and >= criteria.min_score (default 0)
    winner         rank 1 of the final standings
    top_n          rank <= criteria.top_n (default 3)
    team           every member of a team that meets min_score/min_solves
    custom         criteria.user_ids, each must be an approved registrant
Issuing creates rows (idempotent: UNIQUE(template, recipient)) and renders
PDFs in a worker job; HTTP requests never render.
"""

import datetime
import hashlib
import hmac
import io
import os
import uuid

from flask import current_app
from sqlalchemy.exc import IntegrityError

from CTFd.models import Users, db
from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import ApiError
from CTFd.plugins.arena.models import (
    CERT_KINDS,
    ArenaCertificate,
    ArenaCertTemplate,
    ArenaEvent,
    ArenaRegistration,
    ArenaSolve,
    ArenaTeam,
    ArenaTeamMember,
)
from CTFd.plugins.arena.services import entity as entity_service
from CTFd.plugins.arena.services import events as event_service

DEFAULT_BODY = "This certifies that {name} {team_clause}participated in {event} and achieved rank {rank}."


def enabled(app=None):
    app = app or current_app
    return app.extensions["arena"]["settings"].certificates_enabled


def _now():
    return datetime.datetime.utcnow()


# ---------------------------------------------------------------- templates


def save_template(event, data, row=None):
    creating = row is None
    row = row or ArenaCertTemplate(event_id=event.id)
    if creating or "name" in data:
        row.name = v.string(data, "name", required=True, max_len=128, allow_empty=False)
    if creating or "kind" in data:
        row.kind = v.choice(data, "kind", CERT_KINDS, required=True)
    if "title" in data or creating:
        row.title = v.string(data, "title", max_len=128) or "Certificate of Achievement"
    if "body" in data or creating:
        body = v.string(data, "body", max_len=1000) or DEFAULT_BODY
        try:
            body.format(
                name="", team="", team_clause="", event="", rank="", date="", score=""
            )
        except (KeyError, IndexError, ValueError):
            raise v.ValidationError(
                "body",
                "allowed placeholders: {name} {team} {team_clause} {event} {rank} {date} {score}",
            )
        row.body = body
    if "accent_color" in data:
        if not v.HEX_COLOR_RE.match(str(data["accent_color"] or "")):
            raise v.ValidationError("accent_color", "must be #rrggbb")
        row.accent_color = data["accent_color"]
    if "criteria" in data:
        c = data["criteria"] or {}
        if not isinstance(c, dict) or set(c) - {
            "min_score",
            "min_solves",
            "top_n",
            "user_ids",
        }:
            raise v.ValidationError(
                "criteria", "allowed: min_score, min_solves, top_n, user_ids"
            )
        for k in ("min_score", "min_solves", "top_n"):
            if k in c and (
                not isinstance(c[k], int) or isinstance(c[k], bool) or c[k] < 0
            ):
                raise v.ValidationError(
                    f"criteria.{k}", "must be a non-negative integer"
                )
        if "user_ids" in c and (
            not isinstance(c["user_ids"], list)
            or len(c["user_ids"]) > 10000
            or not all(isinstance(i, int) for i in c["user_ids"])
        ):
            raise v.ValidationError("criteria.user_ids", "must be a list of user ids")
        row.criteria = c
    if "enabled" in data:
        row.enabled = bool(v.boolean(data, "enabled"))
    if creating:
        db.session.add(row)
    db.session.commit()
    return row


def serialize_template(t):
    return {
        "id": t.id,
        "name": t.name,
        "kind": t.kind,
        "title": t.title,
        "body": t.body,
        "criteria": t.criteria or {},
        "accent_color": t.accent_color,
        "enabled": t.enabled,
    }


# -------------------------------------------------------------- eligibility


def _approved_users(event):
    return {
        u
        for (u,) in db.session.query(ArenaRegistration.user_id).filter_by(
            event_id=event.id, status="approved"
        )
    }


def eligible(event, tpl):
    """[(recipient_key, user_id, team_id, rank, score)] for the template."""
    from CTFd.plugins.arena.services import scoring

    crit = tpl.criteria or {}
    approved = _approved_users(event)
    standings = scoring.compute_standings(event, scope="admin")
    by_key = {r["key"]: r for r in standings if r["status"] == "active"}
    solves = dict(
        db.session.query(ArenaSolve.entity_key, db.func.count(ArenaSolve.id))
        .filter(ArenaSolve.event_id == event.id)
        .group_by(ArenaSolve.entity_key)
    )

    def ok_entity(key):
        row = by_key.get(key)
        score = row["score"] if row else 0
        return (solves.get(key, 0) >= crit.get("min_solves", 1)) and score >= crit.get(
            "min_score", 0
        )

    def members(team_id):
        return [
            m.user_id
            for m in ArenaTeamMember.query.filter_by(team_id=team_id, event_id=event.id)
            if m.user_id in approved
        ]

    out = []
    if tpl.kind in ("winner", "top_n"):
        limit = 1 if tpl.kind == "winner" else crit.get("top_n", 3)
        for row in [r for r in standings if r["status"] == "active"][:limit]:
            kind, ident = entity_service.parse(row["key"])
            if kind == "t":
                out += [
                    (f"u:{u}", u, ident, row["rank"], row["score"])
                    for u in members(ident)
                ]
            elif ident in approved:
                out.append((row["key"], ident, None, row["rank"], row["score"]))
    elif tpl.kind == "team":
        if event.team_mode != "team":
            return []
        for team in ArenaTeam.query.filter_by(event_id=event.id, status="active"):
            key = entity_service.team_key(team.id)
            if ok_entity(key):
                row = by_key.get(key, {})
                out.append((key, None, team.id, row.get("rank"), row.get("score", 0)))
    elif tpl.kind == "participation":
        for uid in sorted(approved):
            if event.team_mode == "team":
                m = ArenaTeamMember.query.filter_by(
                    event_id=event.id, user_id=uid
                ).first()
                key = entity_service.team_key(m.team_id) if m else None
                team_id = m.team_id if m else None
            else:
                key, team_id = entity_service.user_key(uid), None
            if key and ok_entity(key):
                row = by_key.get(key, {})
                out.append(
                    (f"u:{uid}", uid, team_id, row.get("rank"), row.get("score", 0))
                )
    elif tpl.kind == "custom":
        for uid in crit.get("user_ids", []):
            if uid in approved:
                out.append((f"u:{uid}", uid, None, None, None))
    return out


# ------------------------------------------------------------------ issuing


def _code(public_id, recipient_key, event_id):
    secret = (
        current_app.config.get("ARENA_SECRET_KEY") or current_app.config["SECRET_KEY"]
    )
    if isinstance(secret, str):
        secret = secret.encode()
    return hmac.new(
        secret, f"cert:{public_id}:{recipient_key}:{event_id}".encode(), hashlib.sha256
    ).hexdigest()[:20]


def request_issue(event, tpl):
    if event_service.effective_phase(event) not in ("completed", "archived"):
        raise ApiError(
            409,
            "Certificates can only be issued after the event has completed",
            code="event_not_completed",
        )
    if not tpl.enabled:
        raise ApiError(409, "Template is disabled")
    from CTFd.plugins.arena.services import jobs

    return jobs.enqueue(
        "certificates.issue",
        {"template_id": tpl.id},
        dedupe_key=f"certificates.issue:{tpl.id}",
        event_id=event.id,
        priority=80,
    )


def issue(tpl):
    """Worker job body: create certificate rows for every eligible recipient."""
    event = db.session.get(ArenaEvent, tpl.event_id)
    created = 0
    for key, user_id, team_id, rank, score in eligible(event, tpl):
        user = db.session.get(Users, user_id) if user_id else None
        team = db.session.get(ArenaTeam, team_id) if team_id else None
        public_id = str(uuid.uuid4())
        cert = ArenaCertificate(
            public_id=public_id,
            event_id=event.id,
            template_id=tpl.id,
            recipient_key=key,
            user_id=user_id,
            team_id=team_id,
            recipient_name=(user.name if user else team.name if team else key)[:128],
            team_name=team.name[:128] if team else None,
            rank=rank,
            score=score,
            kind=tpl.kind,
            status="pending",
            verify_code=_code(public_id, key, event.id),
        )
        try:
            db.session.add(cert)
            db.session.commit()
            created += 1
        except IntegrityError:
            db.session.rollback()  # already issued for this recipient
    pending = ArenaCertificate.query.filter_by(
        template_id=tpl.id, status="pending"
    ).all()
    for cert in pending:
        render_and_store(cert)
    return {"created": created, "rendered": len(pending)}


def _font(size):
    from PIL import ImageFont

    path = os.path.join(current_app.root_path, "fonts", "OpenSans-Bold.ttf")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size=size)


def render_pdf(cert, tpl, event, verify_url):
    from PIL import Image, ImageDraw

    w, h = 1754, 1240  # A4 landscape at 150 dpi
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([30, 30, w - 30, h - 30], outline=tpl.accent_color, width=12)
    d.rectangle([60, 60, w - 60, h - 60], outline="#c9d1d9", width=3)

    def center(text, y, size, fill="#0d1117"):
        font = _font(size)
        tw = d.textlength(text, font=font)
        d.text(((w - tw) / 2, y), text, font=font, fill=fill)

    def wrap(text, size, width_px):
        font, words, lines, line = _font(size), text.split(), [], ""
        for word in words:
            cand = (line + " " + word).strip()
            if d.textlength(cand, font=font) <= width_px:
                line = cand
            else:
                lines.append(line)
                line = word
        return lines + ([line] if line else [])

    center(tpl.title[:60], 170, 72, fill=tpl.accent_color)
    center(cert.recipient_name[:60], 380, 96)
    date = (cert.issued_at or _now()).strftime("%d %B %Y")
    body = tpl.body.format(
        name=cert.recipient_name,
        team=cert.team_name or "",
        event=event.name,
        team_clause=f"of team {cert.team_name} " if cert.team_name else "",
        rank=cert.rank if cert.rank is not None else "-",
        score=cert.score or 0,
        date=date,
    )
    y = 560
    for line in wrap(body, 40, w - 400)[:5]:
        center(line, y, 40, fill="#30363d")
        y += 60
    center(f"{event.name}  ·  {date}", h - 300, 34, fill="#57606a")
    center(f"Certificate ID {cert.public_id}", h - 220, 26, fill="#57606a")
    center(f"Verify: {verify_url}", h - 170, 26, fill="#57606a")
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150.0)
    buf.seek(0)
    return buf


def verify_url(cert):
    base = current_app.config.get("ARENA_PUBLIC_URL") or ""
    return f"{base.rstrip('/')}/arena/verify/{cert.public_id}?code={cert.verify_code}"


def render_and_store(cert):
    from werkzeug.datastructures import FileStorage

    from CTFd.utils.uploads import get_uploader

    tpl = db.session.get(ArenaCertTemplate, cert.template_id)
    event = db.session.get(ArenaEvent, cert.event_id)
    cert.issued_at = cert.issued_at or _now()
    try:
        pdf = render_pdf(cert, tpl, event, verify_url(cert))
        storage = FileStorage(
            stream=pdf,
            filename=f"certificate-{cert.public_id}.pdf",
            content_type="application/pdf",
        )
        location = get_uploader().upload(
            file_obj=storage,
            filename=storage.filename,
            path=f"arena-certificates-{uuid.uuid4().hex}",
        )
        cert.file_location, cert.status = location, "issued"
    except Exception:  # noqa: BLE001 - mark failed; admin can re-issue
        cert.status = "failed"
    db.session.commit()
    return cert


def revoke(cert, reason):
    if cert.status == "revoked":
        raise ApiError(409, "Already revoked")
    cert.status, cert.revoked_at = "revoked", _now()
    cert.revoke_reason = (reason or "")[:255]
    db.session.commit()
    return cert


def can_download(cert, user):
    from CTFd.plugins.arena.core import rbac

    if user is None:
        return False
    if rbac.has_permission(user, rbac.PERM_CERTIFICATES_MANAGE, cert.event_id):
        return True
    if cert.status != "issued":
        return False
    if cert.user_id == user.id:
        return True
    if cert.team_id is not None:
        return (
            ArenaTeamMember.query.filter_by(
                team_id=cert.team_id, user_id=user.id
            ).first()
            is not None
        )
    return False


def serialize(cert, admin=False):
    data = {
        "id": cert.public_id,
        "kind": cert.kind,
        "status": cert.status,
        "recipient": cert.recipient_name,
        "team": cert.team_name,
        "rank": cert.rank,
        "issued_at": v.iso(cert.issued_at),
        "template_id": cert.template_id,
    }
    if admin:
        data.update(
            db_id=cert.id,
            user_id=cert.user_id,
            team_id=cert.team_id,
            revoked_at=v.iso(cert.revoked_at),
            revoke_reason=cert.revoke_reason,
        )
    return data
