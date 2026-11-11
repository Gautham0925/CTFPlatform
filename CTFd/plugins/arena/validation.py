"""Small, explicit input validation helpers (no new dependencies).

All user/organizer input entering Arena services passes through these
functions. They raise ValidationError with a field name so API handlers can
return 400 with a precise, non-sensitive message.
"""

import datetime
import re
from typing import Any, Iterable, Optional


class ValidationError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
EMAIL_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


def string(
    data: dict,
    field: str,
    *,
    required: bool = False,
    max_len: int = 255,
    min_len: int = 0,
    allow_empty: bool = True,
) -> Optional[str]:
    value = data.get(field)
    if value is None:
        if required:
            raise ValidationError(field, "is required")
        return None
    if not isinstance(value, str):
        raise ValidationError(field, "must be a string")
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(field, "must not be empty")
    if len(value) > max_len:
        raise ValidationError(field, f"must be at most {max_len} characters")
    if len(value) < min_len:
        raise ValidationError(field, f"must be at least {min_len} characters")
    return value


def slug(data: dict, field: str = "slug", required: bool = False) -> Optional[str]:
    value = string(data, field, required=required, max_len=64)
    if value is None:
        return None
    value = value.lower()
    if not SLUG_RE.match(value):
        raise ValidationError(
            field, "must be lowercase letters, digits and dashes (2-64 chars)"
        )
    return value


def integer(
    data: dict,
    field: str,
    *,
    required: bool = False,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    nullable: bool = True,
) -> Optional[int]:
    if field not in data or data.get(field) is None:
        if required:
            raise ValidationError(field, "is required")
        return None
    value = data[field]
    if isinstance(value, bool):
        raise ValidationError(field, "must be an integer")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValidationError(field, "must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(field, f"must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(field, f"must be <= {maximum}")
    return value


def boolean(data: dict, field: str, *, required: bool = False) -> Optional[bool]:
    if field not in data or data.get(field) is None:
        if required:
            raise ValidationError(field, "is required")
        return None
    value = data[field]
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValidationError(field, "must be a boolean")


def choice(
    data: dict, field: str, allowed: Iterable[str], *, required: bool = False
) -> Optional[str]:
    value = string(data, field, required=required, max_len=64)
    if value is None:
        return None
    allowed = tuple(allowed)
    if value not in allowed:
        raise ValidationError(field, f"must be one of: {', '.join(allowed)}")
    return value


def parse_datetime(value: Any, field: str) -> Optional[datetime.datetime]:
    """ISO-8601 → naive UTC datetime. Accepts 'Z' and offsets."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            raise ValidationError(field, "must be an ISO-8601 datetime")
    else:
        raise ValidationError(field, "must be an ISO-8601 datetime")
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def dt(data: dict, field: str) -> Optional[datetime.datetime]:
    return parse_datetime(data.get(field), field)


def string_list(
    data: dict, field: str, *, max_items: int = 100, max_len: int = 255
) -> Optional[list]:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError(field, "must be a list of strings")
    if len(value) > max_items:
        raise ValidationError(field, f"must have at most {max_items} items")
    out = []
    for v in value:
        v = v.strip()
        if len(v) > max_len:
            raise ValidationError(field, f"items must be at most {max_len} characters")
        if v:
            out.append(v)
    return out


def iso(value: Optional[datetime.datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"
