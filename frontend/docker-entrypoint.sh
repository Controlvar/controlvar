#!/bin/sh
# Controlvar dashboard container entrypoint (nginxinc/nginx-unprivileged).
#
# Renders /etc/nginx/templates/default.conf.template -> /etc/nginx/conf.d/default.conf,
# substituting ONLY ${API_UPSTREAM} (default http://api:8000) and
# ${CSP_CONNECT_SRC} so nginx's own $variables survive, then hands over to the
# stock nginx entrypoint (which runs the remaining /docker-entrypoint.d scripts
# and starts nginx).
#
# CSP_CONNECT_SRC: the Content-Security-Policy connect-src list. The default
# covers the same-origin deployment (nginx proxies /api, /ws and /health).
# When the bundle was built with an absolute NEXT_PUBLIC_API_URL (dashboard
# and API on different origins), add that origin, e.g.
#   CSP_CONNECT_SRC="'self' https://api.example.com wss://api.example.com"
set -eu

: "${API_UPSTREAM:=http://api:8000}"
# Strip a trailing slash: proxy_pass with a URI part would rewrite the path.
API_UPSTREAM="${API_UPSTREAM%/}"
export API_UPSTREAM

: "${CSP_CONNECT_SRC:='self' ws: wss:}"
export CSP_CONNECT_SRC

TEMPLATE=/etc/nginx/templates/default.conf.template
TARGET=/etc/nginx/conf.d/default.conf

if [ -f "$TEMPLATE" ]; then
    envsubst '${API_UPSTREAM} ${CSP_CONNECT_SRC}' < "$TEMPLATE" > "$TARGET"
    echo "[entrypoint] rendered $TARGET (API_UPSTREAM=$API_UPSTREAM, connect-src=$CSP_CONNECT_SRC)" >&2
fi

# The stock image's template step would re-render *.template files with every
# environment variable; point it at an empty directory so ours stays intact.
export NGINX_ENVSUBST_TEMPLATE_DIR=/etc/nginx/templates-disabled

exec /docker-entrypoint.sh "$@"
