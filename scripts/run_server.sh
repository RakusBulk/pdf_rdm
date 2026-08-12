#!/usr/bin/env bash
# Starts the license server for local development.
# For production, run this behind a TLS-terminating reverse proxy
# (nginx/caddy) — keys are sent over this API and must never travel
# over plain HTTP outside localhost.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${PDF_DRM_ADMIN_TOKEN:-}" ]; then
  echo "Set PDF_DRM_ADMIN_TOKEN before starting the server, e.g.:"
  echo "  export PDF_DRM_ADMIN_TOKEN=\$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  exit 1
fi

uvicorn server.app:app --host 0.0.0.0 --port 8443 --reload
