#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${HOME}/.streamlit"

streamlit run /app/app.py \
  --server.port=8501 \
  --server.address=127.0.0.1 \
  --server.headless=true \
  --server.enableCORS=false \
  --browser.gatherUsageStats=false &
STREAMLIT_PID=$!

cleanup() {
  kill "${STREAMLIT_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

exec nginx -c /app/deployment/nginx.conf -g 'daemon off;'
