#!/usr/bin/env bash
set -euo pipefail
for f in /Volumes/Xugab/LAB/PrivateLink/credentials/.env /Volumes/Xugab/LAB/PrivateLink/.env.local; do
  if [[ -f "$f" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$f"
    set +a
  fi
done
export CSMART_MOCK_MUSE_SPARK="${CSMART_MOCK_MUSE_SPARK:-0}"
export CSMART_PORT="${CSMART_PORT:-18080}"
export CSMART_HOST="${CSMART_HOST:-127.0.0.1}"
export UPSTREAM_API_KEY="${UPSTREAM_API_KEY:-${OPENAI_API_KEY:-${ANTHROPIC_AUTH_TOKEN:-}}}"
exec /opt/homebrew/bin/python3 /Volumes/Xugab/LAB/Tria/anythingllm-proxy/csmart_proxy.py
