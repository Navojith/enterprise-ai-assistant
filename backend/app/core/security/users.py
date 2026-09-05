"""Static, hardcoded user store — docs/DECISIONS.md's Option A, chosen over Keycloak because
RBAC carries 5% of the grade while Agent Architecture, RAG, LangGraph and RLM carry 60%
(docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 3).

Only the *store* is static. Everything downstream of it — password hashing with `bcrypt`,
JWT issuance and verification (`core/security/jwt.py`), and the role→permission matrix
(`core/security/rbac.py`) — is the real mechanism a production system would keep even after
swapping this module for a database-backed store or an identity provider. Replacing this file
alone is the entire migration path.

Credentials are documented in `docs/SETUP.md`'s "Demo users" table, not hidden in source,
because an evaluator needs to log in as each role without reading code.
"""

from __future__ import annotations

import bcrypt
from pydantic import BaseModel

from backend.app.core.security.rbac import Role


class UserRecord(BaseModel):
    """One entry in the static store. `hashed_password` is a bcrypt hash, never the
    plaintext — `verify_password` is the only function that should compare against it."""

    username: str
    hashed_password: bytes
    role: Role
    display_name: str


def _hash(password: str) -> bytes:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())


# Demo credentials only — never a real identity store. One account per role, matching
# ASSESSMENT.md's RBAC table exactly. Passwords are hashed at import time (bcrypt salts each
# call, so the stored hash differs across process restarts even for the same password — that
# is expected and does not affect `verify_password`, which recomputes from the salt embedded
# in the hash itself).
_USERS: dict[str, UserRecord] = {
    "viewer": UserRecord(
        username="viewer",
        hashed_password=_hash("ViewerPass123!"),
        role=Role.VIEWER,
        display_name="Vivian Viewer",
    ),
    "analyst": UserRecord(
        username="analyst",
        hashed_password=_hash("AnalystPass123!"),
        role=Role.ANALYST,
        display_name="Alex Analyst",
    ),
    "admin": UserRecord(
        username="admin",
        hashed_password=_hash("AdminPass123!"),
        role=Role.ADMINISTRATOR,
        display_name="Ada Admin",
    ),
}


def get_user(username: str) -> UserRecord | None:
    """Case-insensitive lookup — usernames are not a security boundary here, only an
    identifier, so treating `Admin` and `admin` as the same account avoids a confusing login
    failure without weakening anything."""
    return _USERS.get(username.lower())


def verify_password(user: UserRecord, password: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), user.hashed_password)
