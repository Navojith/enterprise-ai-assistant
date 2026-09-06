# Backend API image. Built from the repo root (see docker-compose.yml's `context: .`), not
# from this directory, because every module in this project uses absolute imports rooted at the
# repo root (pyproject.toml's `known-first-party = ["backend", "scripts", "mcp_server",
# "frontend", "shared"]`). Copying the whole tree into every service image, rather than a
# partial COPY per service, trades a slightly larger image for zero risk of an import breaking
# only inside a container. See docs/DECISIONS.md §7 and docs/ASSUMPTIONS_AND_TRADEOFFS.md
# trade-offs 23 and 25 — trade-off 25 is why `frontend/` no longer imports `backend.app` at all.
FROM python:3.11-slim

WORKDIR /app

# Installed as its own layer, before the rest of the source, so an application code change
# doesn't invalidate the (slow) dependency-install layer on rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --loop points at the custom event-loop factory required on native Windows (psycopg async vs.
# ProactorEventLoop — see backend/app/core/loop.py). It is an explicit no-op on Linux, so this
# stays identical to the documented native run command in docs/SETUP.md rather than special-
# casing the containerized path.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "backend.app.core.loop:selector_loop_factory"]
