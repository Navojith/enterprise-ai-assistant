# MCP server image (dummy employee directory / service catalog / incident records, Streamable
# HTTP). Built from the repo root — see backend/Dockerfile's header comment for why every
# service image copies the whole tree rather than just its own directory.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8100

CMD ["python", "-m", "mcp_server"]
