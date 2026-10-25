#!/usr/bin/env bash
# Dispatch the container role from the first argument.
# Kept as a tiny wrapper so compose can run either service from one image.
set -euo pipefail

case "${1:-webui}" in
  webui)
    shift
    exec ssenate webui --host 0.0.0.0 --port "${STOCKSENATE_WEBUI_PORT:-8788}" "$@"
    ;;
  mcp)
    shift
    exec ssenate mcp http --host 0.0.0.0 --port "${STOCKSENATE_MCP_PORT:-8765}" "$@"
    ;;
  analyze|backtest|alert|report|queue|role|config|performance|version|--help|-h)
    exec ssenate "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
