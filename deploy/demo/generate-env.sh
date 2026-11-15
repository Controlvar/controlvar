#!/bin/sh
# deploy/demo/generate-env.sh: write the demo box's .env. Run it once, before
# the first reset: like deploy/compose/generate-env.sh, which it calls for the
# production secrets, it refuses to overwrite an existing file.
#
#   PUBLIC_BASE_URL=https://demo.controlvar.com ENV_FILE=/etc/controlvar-demo/demo.env \
#     deploy/demo/generate-env.sh
#
# Then it adds what the demo needs (compose.demo.yml requires each one): the
# published demo account, the host and origin lists for the
# three demo hosts, the SDK rate limit README.md's "Disk" section is worked
# out for, and two random storefront keys (they end up in public browser
# bundles; random, so the repository's development keys never work here).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
env_file=${ENV_FILE:-$here/.env}
public_base_url=${PUBLIC_BASE_URL:-https://demo.controlvar.com}

if [ -e "$env_file" ]; then
    echo "$env_file exists; not overwriting it." >&2
    exit 1
fi

case $public_base_url in
    https://*) ;;
    *)
        echo "PUBLIC_BASE_URL must be an https:// URL, got '$public_base_url'." >&2
        exit 1
        ;;
esac
host=${public_base_url#https://}
host=${host%%/*}
# Spelled out: a range such as a-z follows the locale's collation, and in
# some locales it takes upper case letters too.
case $host in
    "" | *[!abcdefghijklmnopqrstuvwxyz0123456789.-]*)
        echo "PUBLIC_BASE_URL must be https://<lower-case host name>, got '$public_base_url'." >&2
        exit 1
        ;;
esac
if [ "$public_base_url" != "https://$host" ]; then
    echo "PUBLIC_BASE_URL must be https://<lower-case host name> with no path, got '$public_base_url'." >&2
    exit 1
fi

key() { printf 'eptk_%s' "$(openssl rand -hex 16)"; }
shoplab_key=$(key)
streampulse_key=$(key)
for value in "$shoplab_key" "$streampulse_key"; do
    if [ "$value" = "eptk_" ]; then
        echo "Generating a storefront key failed; nothing was written." >&2
        exit 1
    fi
done

# The whole file is written beside the target, then linked into place, which
# fails if the target appeared meanwhile: never half a file, never two writers.
umask 077
tmp="$env_file.tmp.$$"
trap 'rm -f "$tmp"' EXIT
ENV_FILE=$tmp PUBLIC_BASE_URL=$public_base_url "$here/../compose/generate-env.sh" >/dev/null
# The box runs the core profile; the base generator takes the profile from the
# environment, so refuse anything else it wrote.
if ! grep -qx '[A-Z]*_PROFILE=core' "$tmp"; then
    echo "The production generator wrote a profile other than core; nothing was written." >&2
    exit 1
fi
cat >>"$tmp" <<EOF

# The demo box (deploy/demo/generate-env.sh). compose.demo.yml requires each.
# The database user, named here because the seed service needs it too; the
# database is created afresh at every reset.
POSTGRES_USER=controlvar
DEMO_ACCOUNT_EMAIL=viewer@demo.com
DEMO_ACCOUNT_PASSWORD=Demo1234!
ALLOWED_HOSTS=$host,api
CORS_ORIGINS=https://shoplab.$host,https://streampulse.$host
SDK_RATE_LIMIT_PER_MINUTE=120
SHOPLAB_API_KEY=$shoplab_key
STREAMPULSE_API_KEY=$streampulse_key
EOF
ln "$tmp" "$env_file"
echo "Wrote $env_file."
