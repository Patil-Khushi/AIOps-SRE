#!/bin/sh
# Regenerates the runtime config the SPA reads, from container env vars.
# Runs automatically via nginx's /docker-entrypoint.d mechanism at start.
set -e

: "${VITE_USER_SERVICE_URL:=http://localhost:8001}"
: "${VITE_ORDER_SERVICE_URL:=http://localhost:8002}"

cat > /usr/share/nginx/html/config.js <<EOF
window.__APP_CONFIG__ = {
  USER_SERVICE_URL: "${VITE_USER_SERVICE_URL}",
  ORDER_SERVICE_URL: "${VITE_ORDER_SERVICE_URL}"
};
EOF

echo "[app-config] wrote config.js -> user=${VITE_USER_SERVICE_URL} order=${VITE_ORDER_SERVICE_URL}"