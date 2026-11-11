"""Certificate API — registered ONLY when ARENA_CERTIFICATES_ENABLED=true."""

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
    ArenaCertificate,
    ArenaCertTemplate,
    ArenaEvent,
    ArenaTeamMember,
)
from CTFd.plugins.arena.services import activity
from CTFd.plugins.arena.services import certificates as cert_service
from CTFd.plugins.arena.services.audit import audit
from CTFd.utils.user import authed, get_current_user

certificates_api = api_blueprint("arena_certificates_api", __name__)
A = f"{API_PREFIX}/admin/events/<int:event_id>/certificates"
E = f"{API_PREFIX}/events/<int:event_id>"
C = f"{API_PREFIX}/certificates"
MANAGE = rbac.PERM_CERTIFICATES_MANAGE


@certificates_api.route(f"{A}/templates")
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def list_templates(event):
    return ok(
        [
            cert_service.serialize_template(t)
            for t in scoped_query(ArenaCertTemplate, event)
        ]
    )


@certificates_api.route(f"{A}/templates", methods=["POST"])
@event_permission_required(MANAGE)
def create_template(event):
    tpl = cert_service.save_template(event, json_body())
    audit(
        "certificate.template_create",
        event_id=event.id,
        target=("cert_template", tpl.id),
    )
    return ok(cert_service.serialize_template(tpl), status=201)


@certificates_api.route(f"{A}/templates/<int:tpl_id>", methods=["PATCH"])
@event_permission_required(MANAGE)
def update_template(event, tpl_id):
    tpl = scoped_get_or_404(ArenaCertTemplate, tpl_id, event)
    cert_service.save_template(event, json_body(), row=tpl)
    return ok(cert_service.serialize_template(tpl))


@certificates_api.route(f"{A}/templates/<int:tpl_id>/issue", methods=["POST"])
@event_permission_required(MANAGE)
def issue(event, tpl_id):
    tpl = scoped_get_or_404(ArenaCertTemplate, tpl_id, event)
    job = cert_service.request_issue(event, tpl)
    audit("certificate.issue", event_id=event.id, target=("cert_template", tpl.id))
    return ok({"job_id": job.id, "status": job.status}, status=202)


@certificates_api.route(A)
@event_permission_required(rbac.PERM_EVENT_VIEW_ADMIN)
def list_certificates(event):
    page, per_page = pagination(default_per_page=50)
    data, meta = paginate(
        scoped_query(ArenaCertificate, event).order_by(ArenaCertificate.id),
        page,
        per_page,
        lambda c: cert_service.serialize(c, admin=True),
    )
    return ok(data, meta=meta)


@certificates_api.route(f"{A}/<int:cert_id>/revoke", methods=["POST"])
@event_permission_required(MANAGE)
def revoke(event, cert_id):
    cert = scoped_get_or_404(ArenaCertificate, cert_id, event)
    reason = v.string(
        json_body(), "reason", required=True, max_len=255, min_len=3, allow_empty=False
    )
    cert_service.revoke(cert, reason)
    audit(
        "certificate.revoke",
        event_id=event.id,
        target=("certificate", cert.public_id),
        meta={"reason": reason},
    )
    return ok(cert_service.serialize(cert, admin=True))


@certificates_api.route(f"{E}/my-certificates")
@event_participant_required
def mine(event):
    user = get_current_user()
    team_ids = [
        m.team_id
        for m in ArenaTeamMember.query.filter_by(event_id=event.id, user_id=user.id)
    ]
    q = (
        scoped_query(ArenaCertificate, event)
        .filter(ArenaCertificate.status == "issued")
        .filter(
            (ArenaCertificate.user_id == user.id)
            | (
                ArenaCertificate.team_id.in_(team_ids or [0])
                & ArenaCertificate.user_id.is_(None)
            )
        )
    )
    return ok(
        [
            dict(cert_service.serialize(c), download_url=f"{C}/{c.public_id}/download")
            for c in q
        ]
    )


@certificates_api.route(f"{C}/<public_id>/download")
def download(public_id):
    if not authed():
        raise ApiError(401)
    cert = ArenaCertificate.query.filter_by(public_id=str(public_id)[:36]).first()
    user = get_current_user()
    if (
        cert is None
        or not cert_service.can_download(cert, user)
        or not cert.file_location
    ):
        raise ApiError(404)  # same answer for missing and foreign certificates
    from CTFd.utils.uploads import get_uploader

    activity.record(
        "certificate.download",
        user_id=user.id,
        event_id=cert.event_id,
        target=("certificate", cert.public_id),
    )
    response = get_uploader().download(cert.file_location)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@certificates_api.route(f"{C}/verify/<public_id>")
@ratelimit(method="GET", limit=30, interval=60, key_prefix="arena-cert-verify")
def verify(public_id):
    """Public verification. Requires the code printed on the certificate, so
    certificate holders' names cannot be enumerated by id alone."""
    import hmac

    from flask import request

    cert = ArenaCertificate.query.filter_by(public_id=str(public_id)[:36]).first()
    code = request.args.get("code", "")
    if cert is None or not hmac.compare_digest(cert.verify_code, code[:64]):
        return ok({"valid": False})
    event = db.session.get(ArenaEvent, cert.event_id)
    return ok(
        {
            "valid": cert.status == "issued",
            "status": cert.status,
            "recipient": cert.recipient_name,
            "team": cert.team_name,
            "event": event.name if event else None,
            "kind": cert.kind,
            "rank": cert.rank,
            "issued_at": v.iso(cert.issued_at),
            "revoked_at": v.iso(cert.revoked_at),
        }
    )
