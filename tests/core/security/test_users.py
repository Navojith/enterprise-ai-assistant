"""Tests for the static user store's password hashing and lookup.

Does not assert the demo passwords' literal values against a hardcoded string in the test —
that would just duplicate `users.py` rather than testing behaviour. Instead it exercises the
actual contract: a correct password verifies, a wrong one does not, and the hash is never
stored or compared as plaintext.
"""

from __future__ import annotations

from backend.app.core.security.rbac import Role
from backend.app.core.security.users import get_user, verify_password


def test_every_role_has_exactly_one_demo_account() -> None:
    roles = {get_user(username).role for username in ("viewer", "analyst", "admin")}  # type: ignore[union-attr]
    assert roles == {Role.VIEWER, Role.ANALYST, Role.ADMINISTRATOR}


def test_username_lookup_is_case_insensitive() -> None:
    assert get_user("Admin") is not None
    assert get_user("Admin").username == "admin"  # type: ignore[union-attr]


def test_unknown_username_returns_none() -> None:
    assert get_user("nobody") is None


def test_correct_password_verifies() -> None:
    user = get_user("viewer")
    assert user is not None
    assert verify_password(user, "ViewerPass123!")


def test_wrong_password_does_not_verify() -> None:
    user = get_user("viewer")
    assert user is not None
    assert not verify_password(user, "not the password")


def test_password_is_never_stored_as_plaintext() -> None:
    user = get_user("admin")
    assert user is not None
    assert b"AdminPass123!" not in user.hashed_password
