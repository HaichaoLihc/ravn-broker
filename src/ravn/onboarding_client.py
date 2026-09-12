"""Small browser-login binding helper for application-owned OAuth completion.

Pass a stable ID for the *verified login session*, not a user-controlled request
field or only a user ID. This in-memory helper is for one-process apps/examples;
multi-worker applications must store and consume these bindings atomically in
their own session store. It never authenticates an end user for the application.
"""

import hmac
import secrets
import time
from dataclasses import dataclass, field

from ravn.common import digest


@dataclass(repr=False)
class ConnectBinding:
    actor: tuple[str, str, str]
    login_hash: str
    app_state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires: float = field(default_factory=lambda: time.monotonic() + 600)
    session_id: str | None = None
    consumed: bool = False

    @classmethod
    def for_login(cls, actor, verified_login_id):
        return cls(tuple(actor), digest(verified_login_id))

    def validate(self, actor, verified_login_id, query):
        if (
            self.consumed
            or self.expires <= time.monotonic()
            or tuple(actor) != self.actor
            or not hmac.compare_digest(self.login_hash, digest(verified_login_id))
            or not isinstance(query.get("app_state"), str)
            or not hmac.compare_digest(digest(self.app_state), digest(query["app_state"]))
            or not self.session_id
            or query.get("session_id") != self.session_id
        ):
            raise ValueError("Original browser login transaction does not match")

    def consume(self, actor, verified_login_id, query):
        self.validate(actor, verified_login_id, query)
        code = query.get("completion_code")
        if not isinstance(code, str) or not 32 <= len(code) <= 512:
            raise ValueError("Authorization did not supply a completion code")
        self.consumed = True
        return code
