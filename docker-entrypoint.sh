#!/bin/bash
set -e

# Activate virtual environment
source .venv/bin/activate

# If arguments are passed — execute them directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

# Multi-server mode (SSE or streamable-http)
# Set SERENA_MULTI_SERVER=1 to enable
# SERENA_TRANSPORT: "sse" (default) or "streamable-http"
if [ "${SERENA_MULTI_SERVER}" = "1" ]; then
    exec serena start-multi-server \
        --transport "${SERENA_TRANSPORT:-sse}" \
        --base-port "${SERENA_BASE_PORT:-9200}" \
        --host "${SERENA_HOST:-0.0.0.0}" \
        ${SERENA_PROJECTS_DIR:+--projects-dir "$SERENA_PROJECTS_DIR"} \
        ${SERENA_ADMIN_PORT:+--admin-port "$SERENA_ADMIN_PORT"}
fi

# Daemon mode — container stays alive for `docker exec` connections
echo "Serena running in daemon mode. Connect via:"
echo "  docker exec -i <container> serena-mcp-server --project /path/to/project"
exec tail -f /dev/null
