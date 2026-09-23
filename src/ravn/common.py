"""Small shared primitives; never serialize exception inputs or secret values."""

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def token(prefix: str) -> str:
    return f"rv_{prefix}_{uuid.uuid4().hex}_{secrets.token_urlsafe(32)}"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw: bytes) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)

    def depth(item, level=0):
        if level > 32:
            raise ValueError("JSON nesting limit")
        if isinstance(item, dict):
            for child in item.values():
                depth(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                depth(child, level + 1)

    depth(value)
    return value


class RavnError(Exception):
    def __init__(self, status: int, code: str, message: str, *, call_id: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.call_id = status, code, message, call_id

    def envelope(self, request_id: str) -> dict:
        value = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
            "retryable": False,
        }
        if self.call_id:
            value["details"] = {"call_id": self.call_id}
        return {"error": value}


@dataclass(frozen=True)
class Principal:
    app: str
    tenant: str
    user: str
    key_id: str | None = None
    session_id: str | None = None

    @property
    def namespace(self) -> tuple[str, str, str]:
        return self.app, self.tenant, self.user
