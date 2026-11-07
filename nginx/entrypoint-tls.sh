#!/bin/sh
# Nginx entrypoint (REQ-002): HTTPS when cert.pem and key.pem are in /etc/nginx/certs (mounted from nginx/certs/)
# and HTTPS_PORT is set (compose.tls.yaml sets it; compose.yaml alone does not publish 443), otherwise HTTP only.
# It chooses the server template, then hands over to the image's entrypoint, which renders it with envsubst into
# /etc/nginx/conf.d/default.conf and starts Nginx. It runs at every start, so adding or removing the files takes
# effect at the next restart of the container.
set -eu

CERTS_DIR=/etc/nginx/certs
SOURCE_DIR=/etc/nginx/server-templates
TEMPLATE=/etc/nginx/templates/default.conf.template

# Body limit of the document upload route (api-proxy.conf, templates): the file limit plus 1 MB of multipart framing.
# The backend accepts the same whole numbers (leading zeros too: 020 is 20), so the zeros are removed before the
# arithmetic, which would otherwise read them as octal. More than 5 digits (99999 MB) is refused as absurd.
MAX_UPLOAD_SIZE_MB="${MAX_UPLOAD_SIZE_MB:-20}"
UPLOAD_LIMIT_DIGITS=$(printf '%s' "$MAX_UPLOAD_SIZE_MB" | sed 's/^0*//')
case "$MAX_UPLOAD_SIZE_MB" in
    *[!0-9]*) UPLOAD_LIMIT_DIGITS="" ;;
esac
if [ -z "$UPLOAD_LIMIT_DIGITS" ] || [ "${#UPLOAD_LIMIT_DIGITS}" -gt 5 ]; then
    echo "entrypoint-tls: MAX_UPLOAD_SIZE_MB must be a whole number from 1 to 99999, got '$MAX_UPLOAD_SIZE_MB'" >&2
    exit 1
fi
UPLOAD_BODY_LIMIT_MB=$((UPLOAD_LIMIT_DIGITS + 1))
export UPLOAD_BODY_LIMIT_MB

mkdir -p "$(dirname "$TEMPLATE")"
HTTPS_PORT="${HTTPS_PORT:-}"
if [ -f "$CERTS_DIR/cert.pem" ] && [ -f "$CERTS_DIR/key.pem" ] && [ -n "$HTTPS_PORT" ]; then
    # Public HTTPS port for the redirect: the default 443 is left out of the URL
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
    if [ -f "$CERTS_DIR/cert.pem" ] && [ -f "$CERTS_DIR/key.pem" ]; then
        echo "entrypoint-tls: TLS disabled: HTTPS_PORT not set (start with compose.tls.yaml): HTTP only"
    elif [ -f "$CERTS_DIR/cert.pem" ]; then
        echo "entrypoint-tls: TLS disabled: key.pem missing in $CERTS_DIR: HTTP only"
    elif [ -f "$CERTS_DIR/key.pem" ]; then
        echo "entrypoint-tls: TLS disabled: cert.pem missing in $CERTS_DIR: HTTP only"
    else
        echo "entrypoint-tls: no cert.pem and key.pem in $CERTS_DIR: HTTP only"
    fi
fi

exec /docker-entrypoint.sh "$@"
