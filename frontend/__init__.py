"""The Streamlit chat UI (`app.py`) and its typed backend client (`api_client.py`).

Kept as an importable package rather than a single ad-hoc script so `api_client.py`'s SSE
parsing — the one piece of real logic in the frontend — is unit-testable
(`tests/frontend/test_api_client.py`) without a running Streamlit process or a running backend.
"""
