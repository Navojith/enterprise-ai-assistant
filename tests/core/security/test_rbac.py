"""Tests for the role→permission matrix — ASSESSMENT.md's RBAC table transcribed as code.

These are the tests that would fail if a future edit accidentally widened Viewer's
permissions or narrowed Administrator's, which is exactly the class of bug docs/DECISIONS.md
§6 says must be structurally impossible for a prompt to cause, and just as important not to
introduce by an ordinary refactor.
"""

from __future__ import annotations

import pytest

from backend.app.core.errors import AuthorizationError
from backend.app.core.security.rbac import Permission, Principal, Role


class TestViewer:
    def test_viewer_can_chat_and_search(self) -> None:
        principal = Principal(username="v", role=Role.VIEWER)
        assert principal.has_permission(Permission.CHAT)
        assert principal.has_permission(Permission.SEARCH)

    @pytest.mark.parametrize(
        "permission", [Permission.ANALYTICS_TOOLS, Permission.MCP_TOOLS, Permission.ADMIN_TOOLS]
    )
    def test_viewer_cannot_use_administrative_or_analytics_tools(
        self, permission: Permission
    ) -> None:
        principal = Principal(username="v", role=Role.VIEWER)
        assert not principal.has_permission(permission)
        with pytest.raises(AuthorizationError):
            principal.require_permission(permission)


class TestAnalyst:
    @pytest.mark.parametrize(
        "permission",
        [Permission.CHAT, Permission.SEARCH, Permission.ANALYTICS_TOOLS, Permission.MCP_TOOLS],
    )
    def test_analyst_has_search_analytics_and_mcp_tools(self, permission: Permission) -> None:
        principal = Principal(username="a", role=Role.ANALYST)
        assert principal.has_permission(permission)

    def test_analyst_cannot_use_admin_tools(self) -> None:
        principal = Principal(username="a", role=Role.ANALYST)
        assert not principal.has_permission(Permission.ADMIN_TOOLS)


class TestAdministrator:
    @pytest.mark.parametrize("permission", list(Permission))
    def test_administrator_has_every_permission(self, permission: Permission) -> None:
        """Administrator is defined as `frozenset(Permission)` — this test is what keeps that
        claim true as new permissions are added, rather than trusting the definition by eye."""
        principal = Principal(username="admin", role=Role.ADMINISTRATOR)
        assert principal.has_permission(permission)


def test_require_permission_error_names_the_role_and_the_missing_permission() -> None:
    principal = Principal(username="v", role=Role.VIEWER)

    with pytest.raises(AuthorizationError) as exc_info:
        principal.require_permission(Permission.ADMIN_TOOLS)

    assert exc_info.value.details == {"role": "viewer", "required_permission": "admin_tools"}
