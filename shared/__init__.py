"""Code genuinely shared across this project's independently-deployable processes (the backend,
the Streamlit frontend, and — where it applies — the MCP server), containing nothing that only
one of them needs. See `shared/events.py`'s module docstring for why this package exists: a real
coupling bug, not a style preference."""
