"""Shared helpers for Arena JSON APIs.

Response envelope (consistent with native CTFd):
    {"success": true, "data": ...}            (+ "meta" for paginated lists)
    {"success": false, "errors": [...]}       with an HTTP error status

Errors never include stack traces, SQL, credentials or runner details.
"""

from flask import jsonify, request
from werkzeug.exceptions import HTTPException

from CTFd.plugins.arena.core.schema import arena_blueprint
from CTFd.plugins.arena.validation import ValidationError

API_PREFIX = "/api/v1/arena"

_DEFAULT_MESSAGES = {
    400: "Bad request",
    401: "Authentication required",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Payload too large",
    422: "Unprocessable entity",
    429: "Too many requests",
    503: "Service unavailable",
}


class ApiError(Exception):
    """Raise from services/handlers to return a JSON error."""

    def __init__(self, status: int, message: str = None, code: str = None):
        super().__init__(message or _DEFAULT_MESSAGES.get(status, "Error"))
        self.status = status
        self.message = message or _DEFAULT_MESSAGES.get(status, "Error")
        self.code = code


def ok(data=None, status=200, meta=None):
    body = {"success": True, "data": data}
    if meta is not None:
        body["meta"] = meta
    response = jsonify(body)
    response.status_code = status
    return response


def error(status, *messages, code=None):
    body = {
        "success": False,
        "errors": list(messages) or [_DEFAULT_MESSAGES.get(status, "Error")],
    }
    if code:
        body["code"] = code
    response = jsonify(body)
    response.status_code = status
    return response


def json_body():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ApiError(400, "JSON object expected")
    return data


def pagination(default_per_page=25, max_per_page=100):
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("per_page", default_per_page))
    except (TypeError, ValueError):
        raise ApiError(400, "page and per_page must be integers")
    per_page = min(max(1, per_page), max_per_page)
    return page, per_page


def paginate(query, page, per_page, serializer):
    total = query.order_by(None).count()
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    meta = {
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        }
    }
    return [serializer(i) for i in items], meta


def api_blueprint(name, import_name):
    bp = arena_blueprint(name, import_name)

    @bp.errorhandler(ApiError)
    def _api_error(e):
        return error(e.status, e.message, code=e.code)

    @bp.errorhandler(ValidationError)
    def _validation_error(e):
        return error(400, e.message, code=f"invalid:{e.field}")

    def _http_error(e):
        code = getattr(e, "code", None) or 500
        return error(code, _DEFAULT_MESSAGES.get(code, "Error"))

    # Flask consults app-level *code* handlers (CTFd registers HTML pages for
    # 401/403/404/500/502) before blueprint *class* handlers, so register per
    # code to keep Arena API errors JSON.
    for code in list(_DEFAULT_MESSAGES) + [500, 502]:
        bp.register_error_handler(code, _http_error)
    bp.register_error_handler(HTTPException, _http_error)

    return bp
