# Streamlit frontend image. Built from the repo root, not `frontend/` — `frontend/app.py` and
# `frontend/api_client.py` import `shared.events` (the shared ActivityEvent schema, so the Agent
# Activity Panel never drifts from what the graph actually emits; see shared/events.py's
# docstring for why that used to be a `backend.app` import instead, and why that was a real
# coupling bug rather than an untidy one). See backend/Dockerfile's header comment for why every
# service image copies the whole repo tree regardless of what it actually imports.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# `streamlit run <script>` puts the script's own directory on `sys.path`, not the working
# directory the way `python -m ...` or a bare `python script.py` does — so `shared.events` (and
# any other repo-root-relative absolute import) is otherwise unresolvable from inside a Streamlit
# app, regardless of whether the import points at a genuinely standalone package. This is the
# actual fix for `ModuleNotFoundError: No module named 'backend'`/`'shared'`; decoupling the
# import (shared/events.py) fixed *what* the frontend depends on, this fixes *how Python finds
# it*, and both were needed.
ENV PYTHONPATH=/app

EXPOSE 8501

CMD ["streamlit", "run", "frontend/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
