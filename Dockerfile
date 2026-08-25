# Amazing Marvin MCP server (HTTP mode).
# Fail closed: the container refuses to start without MCP_AUTH_TOKEN
# (or MCP_AUTH_TOKEN_FILE); set MCP_ALLOW_UNAUTHENTICATED=true only for
# deliberate localhost experiments.
FROM python:3.12-slim

# Non-root user. /data holds the persisted rate-limit counter; pre-chown
# lets Docker's named-volume initialization set the right owner on first
# start.
RUN useradd --system --uid 10003 --create-home --shell /usr/sbin/nologin marvinmcp \
    && mkdir -p /data \
    && chown 10003:10003 /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY marvin_mcp ./marvin_mcp
RUN pip install --no-cache-dir ".[http]" && pip cache purge || true

ENV STATE_DIR=/data \
    MCP_TRANSPORT=http \
    PORT=8787 \
    HOST=0.0.0.0

USER 10003
EXPOSE 8787
CMD ["python", "-m", "marvin_mcp"]
