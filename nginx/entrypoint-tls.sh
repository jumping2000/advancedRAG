#!/bin/sh
# Nginx entrypoint (REQ-002): HTTPS when cert.pem and key.pem are in /etc/nginx/certs (mounted from nginx/certs/),
# otherwise HTTP only. It chooses the server template, then hands over to the image's entrypoint, which renders it
# with envsubst into /etc/nginx/conf.d/default.conf and starts Nginx. It runs at every start, so adding or removing
# the files takes effect at the next restart of the container.
set -eu

CERTS_DIR=/etc/nginx/certs
SOURCE_DIR=/etc/nginx/server-templates
TEMPLATE=/etc/nginx/templates/default.conf.template

mkdir -p "$(dirname "$TEMPLATE")"
if [ -f "$CERTS_DIR/cert.pem" ] && [ -f "$CERTS_DIR/key.pem" ]; then
    # Public HTTPS port for the redirect: the default 443 is left out of the URL
    HTTPS_PORT="${HTTPS_PORT:-443}"
    if [ "$HTTPS_PORT" = "443" ]; then
        HTTPS_PORT_SUFFIX=""
    else
        HTTPS_PORT_SUFFIX=":$HTTPS_PORT"
    fi
    export HTTPS_PORT_SUFFIX
    cp "$SOURCE_DIR/tls.conf.template" "$TEMPLATE"
    echo "entrypoint-tls: cert.pem and key.pem found in $CERTS_DIR: HTTPS on port $HTTPS_PORT, HTTP redirected"
else
    cp "$SOURCE_DIR/default.conf.template" "$TEMPLATE"
    echo "entrypoint-tls: no cert.pem and key.pem in $CERTS_DIR: HTTP only"
fi

exec /docker-entrypoint.sh "$@"
