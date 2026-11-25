# planner-mcp over HTTP, for Carolina CloudApps (OpenShift). See deploy/README.md.
FROM python:3.12-slim

# gh CLI: GitHub intake shells out to it. It authenticates from GH_TOKEN (an OpenShift Secret).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gpg \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | gpg --dearmor -o /usr/share/keyrings/githubcli.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && apt-get purge -y curl gpg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# OpenShift runs the container as a random UID in group 0, so anything it writes must be
# group-writable. /data is the persistent volume holding the token cache.
RUN mkdir -p /data && chgrp -R 0 /data /app && chmod -R g=u /data /app

ENV HOME=/tmp \
    PATH=/app/.venv/bin:$PATH \
    PLANNER_MCP_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1

USER 1001
EXPOSE 8080
CMD ["planner-mcp", "serve"]
