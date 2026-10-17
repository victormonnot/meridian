#!/bin/sh
set -eu
# Only a DNS hostname can be substituted into the Nginx configuration.
case "${MERIDIAN_DOMAIN:-}" in
  ''|*[!a-zA-Z0-9.-]*) echo 'MERIDIAN_DOMAIN must be a hostname' >&2; exit 1 ;;
esac
envsubst '${MERIDIAN_DOMAIN}' < /etc/nginx/meridian.conf.template > /tmp/nginx.conf
exec nginx -c /tmp/nginx.conf -g 'daemon off;'
