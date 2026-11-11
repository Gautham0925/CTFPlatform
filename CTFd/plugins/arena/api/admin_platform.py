"""Platform-wide operations (superadmin only): jobs, workers, system health."""

from flask import request

from CTFd.plugins.arena import validation as v
from CTFd.plugins.arena.api import (
    API_PREFIX,
    ApiError,
    api_blueprint,
    ok,
    paginate,
    pagination,
)
from CTFd.plugins.arena.core import rbac
from CTFd.plugins.arena.core.authz import global_permission_required
from CTFd.plugins.arena.models import JOB_STATUSES, ArenaJob
from CTFd.plugins.arena.services import jobs
from CTFd.plugins.arena.services.audit import audit
from CTFd.plugins.arena.worker import worker_health

platform_api = api_blueprint("arena_platform_api", __name__)
P = f"{API_PREFIX}/admin/platform"
PLATFORM = rbac.PERM_PLATFORM_MANAGE


@platform_api.route(f"{P}/jobs")
@global_permission_required(PLATFORM)
def list_jobs():
    q = ArenaJob.query
    status = request.args.get("status")
    if status:
        if status not in JOB_STATUSES:
            raise ApiError(400, "unknown status")
        q = q.filter(ArenaJob.status == status)
    if request.args.get("kind"):
        q = q.filter(ArenaJob.kind == request.args["kind"][:48])
    page, per_page = pagination(default_per_page=50)
    data, meta = paginate(
        q.order_by(ArenaJob.id.desc()), page, per_page, jobs.serialize
    )
    return ok(data, meta=meta)


@platform_api.route(f"{P}/jobs/stats")
@global_permission_required(PLATFORM)
def job_stats():
    return ok(jobs.stats())


@platform_api.route(f"{P}/jobs/<int:job_id>/retry", methods=["POST"])
@global_permission_required(PLATFORM)
def retry(job_id):
    if not jobs.retry_dead(job_id):
        raise ApiError(409, "Only dead jobs can be retried")
    audit("job.retry", target=("job", job_id))
    return ok({"requeued": job_id})


@platform_api.route(f"{P}/workers")
@global_permission_required(PLATFORM)
def workers():
    max_age = v.integer(request.args, "max_age", minimum=5, maximum=3600) or 60
    return ok(worker_health(max_age_seconds=max_age))
