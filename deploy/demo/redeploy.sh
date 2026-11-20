#!/usr/bin/env bash
# deploy/demo/redeploy.sh vX.Y.Z: move the demo to a release.
#
# Checks out the tag in DEMO_RELEASE (refusing anything that is not a vX.Y.Z
# tag, or a release checkout with local changes), pulls the images that tag's
# deploy/compose/compose.yml names, builds ShopLab and StreamPulse from the
# same tag's source, then runs reset.sh. The recipe itself (this directory) is
# not touched: move it with git in DEMO_RECIPE's checkout (README.md).
set -euo pipefail

# shellcheck source=deploy/demo/lib.sh
. "$(dirname "$0")/lib.sh"

tag=${1:-}
if ! printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+'; then
    demo_log "usage: redeploy.sh vX.Y.Z (a release tag), got '${tag}'"
    exit 2
fi
if [ "$DEMO_RELEASE" = "$(cd "$DEMO_RECIPE/../.." && pwd)" ]; then
    demo_log "DEMO_RELEASE is the recipe's own checkout; set it to the release checkout (README.md, Layout)"
    exit 2
fi
if [ -n "$(git -C "$DEMO_RELEASE" status --porcelain)" ]; then
    demo_log "$DEMO_RELEASE has local changes; refusing to check out over them"
    exit 1
fi

demo_require
git -C "$DEMO_RELEASE" fetch --quiet --tags origin
git -C "$DEMO_RELEASE" checkout --quiet --detach "refs/tags/$tag"
demo_log "release checkout: $tag ($(git -C "$DEMO_RELEASE" rev-parse --short HEAD))"

unset DEMO_API_IMAGE
dc pull postgres redis api web shoplab-simulator streampulse-simulator
dc build --pull shoplab streampulse
exec "$DEMO_RECIPE/reset.sh"
