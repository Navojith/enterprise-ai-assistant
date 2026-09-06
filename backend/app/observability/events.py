"""Backward-compatible re-export.

The canonical `ActivityEvent`/`ActivityEventType` definitions moved to `shared/events.py` — see
that module's docstring for why (a real cross-process coupling bug the frontend hit under Docker,
not a style preference). Every existing backend import of `backend.app.observability.events`
(`agents/nodes/*.py`, `api/v1/chat.py`, `rlm/api.py`, ...) keeps working unchanged through this
re-export; only the frontend and its tests were updated to import `shared.events` directly,
since removing that dependency on the rest of `backend.app` was the actual point.
"""

from __future__ import annotations

from shared.events import ActivityEvent, ActivityEventType

__all__ = ["ActivityEvent", "ActivityEventType"]
