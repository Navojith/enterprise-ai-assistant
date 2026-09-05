"""Role→permission matrix and the `Principal` identity type.

`Principal` is the one shape every downstream authorization check reads from — the tool
registry (Cycle 4) and the retrieval `access_level` filter (`retrieval/models.py`,
`allowed_access_levels`) both take a role, not a token or a request, so they can be unit
tested without any HTTP or JWT machinery in the loop. It is deliberately constructed in
exactly one place, `api/deps.py::current_principal`, from a verified JWT claim — never from
anything a graph node or the model itself produces. See docs/DECISIONS.md §6.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from backend.app.core.errors import AuthorizationError


class Role(StrEnum):
    """The three roles ASSESSMENT.md's RBAC section requires, by name."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMINISTRATOR = "administrator"


class Permission(StrEnum):
    """Capabilities gated by role. Named after what ASSESSMENT.md's RBAC table grants or
    withholds, not after specific tool implementations, so a new tool in a later cycle slots
    into an existing permission instead of forcing a new one for every addition."""

    CHAT = "chat"
    SEARCH = "search"
    ANALYTICS_TOOLS = "analytics_tools"
    MCP_TOOLS = "mcp_tools"
    ADMIN_TOOLS = "admin_tools"


# ASSESSMENT.md's RBAC table, transcribed directly:
#   Viewer        -> chat, search                            (not administrative tools)
#   Analyst       -> search, analytics tools, MCP tools
#   Administrator -> all tools
# Administrator is `frozenset(Permission)` rather than an enumerated list so it is
# structurally "every permission that exists", including ones added later — matching the
# brief's wording exactly instead of needing a matching edit every time a permission is added.
_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.CHAT, Permission.SEARCH}),
    Role.ANALYST: frozenset(
        {Permission.CHAT, Permission.SEARCH, Permission.ANALYTICS_TOOLS, Permission.MCP_TOOLS}
    ),
    Role.ADMINISTRATOR: frozenset(Permission),
}


class Principal(BaseModel):
    """The authenticated caller for one request, read from request-scoped context.

    Everything that makes an authorization decision — the tool registry's bind-time filter
    and execution-boundary re-check, the retrieval access-level filter — takes a `Principal`,
    never a raw token or role string, so the type system itself flags any code path that
    tries to skip verification.
    """

    username: str
    role: Role

    def has_permission(self, permission: Permission) -> bool:
        return permission in _ROLE_PERMISSIONS[self.role]

    def require_permission(self, permission: Permission) -> None:
        """Raise `AuthorizationError` unless this principal's role grants `permission`.

        The single call site every tool-execution boundary and admin-only endpoint should use
        — see docs/DECISIONS.md §6 for why this check belongs here, in application code that
        reads request-scoped identity, and never in a prompt.
        """
        if not self.has_permission(permission):
            raise AuthorizationError(
                f"Role {self.role.value!r} does not grant {permission.value!r}.",
                details={"role": self.role.value, "required_permission": permission.value},
            )
