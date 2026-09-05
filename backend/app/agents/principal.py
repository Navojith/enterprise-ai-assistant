"""Reconstructs a `Principal` from graph state.

`AgentState` carries `principal_username`/`principal_role` as plain strings, not a `Principal`,
because `AgentState` is a `TypedDict` LangGraph checkpoints as JSON — a `Principal` model would
round-trip through that just as well, but keeping state to primitives and reconstructing the
richer type only where it is used (here) keeps `state.py` free of importing `core.security`,
and keeps this one small function as the only place that reconstruction happens, rather than
every node that needs a `Principal` repeating `Principal(username=..., role=Role(...))` itself.
"""

from __future__ import annotations

from backend.app.agents.state import AgentState
from backend.app.core.security.rbac import Principal, Role


def principal_from_state(state: AgentState) -> Principal:
    return Principal(username=state["principal_username"], role=Role(state["principal_role"]))
