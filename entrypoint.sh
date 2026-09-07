#!/usr/bin/env bash
set -euo pipefail

PUID="${PUID:?PUID must be set}"
PGID="${PGID:?PGID must be set}"
APP_USER=mediaimport
APP_GROUP=mediaimport

if ! getent group "${PGID}" >/dev/null 2>&1; then
    groupadd -g "${PGID}" "${APP_GROUP}"
fi
PRIMARY_GROUP="$(getent group "${PGID}" | cut -d: -f1)"

if ! getent passwd "${PUID}" >/dev/null 2>&1; then
    useradd -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin "${APP_USER}"
fi
RUN_USER="$(getent passwd "${PUID}" | cut -d: -f1)"

if [ -n "${RENDER_GID:-}" ]; then
    if ! getent group "${RENDER_GID}" >/dev/null 2>&1; then
        groupadd -g "${RENDER_GID}" render_host
    fi
    RENDER_GROUP="$(getent group "${RENDER_GID}" | cut -d: -f1)"
    usermod -aG "${RENDER_GROUP}" "${RUN_USER}"
    echo "entrypoint: ${RUN_USER} added to group ${RENDER_GROUP} (gid ${RENDER_GID}) for /dev/dri"
else
    echo "entrypoint: RENDER_GID is unset, GPU encoding will be unavailable"
fi

missing=""
for spec in \
    "MEDIA_IMPORT:${MEDIA_IMPORT:-/media/import}" \
    "MEDIA_ENCODE:${MEDIA_ENCODE:-/media/encode}" \
    "MEDIA_COMPLETE:${MEDIA_COMPLETE:-/media/complete}" \
    "MEDIA_HOLD:${MEDIA_HOLD:-/media/hold}" \
    "MEDIA_CONFIG:${MEDIA_CONFIG:-/media/config}" \
    "CERT_DIR:${CERT_DIR:-/certs}" \
; do
    name="${spec%%:*}"
    path="${spec#*:}"
    if [ ! -d "${path}" ]; then
        echo "entrypoint: ${name} ${path} is not mounted" >&2
        missing="yes"
    fi
done
if [ -n "${missing}" ]; then
    exit 2
fi

echo "entrypoint: running as ${RUN_USER}:${PRIMARY_GROUP} (${PUID}:${PGID})"
exec gosu "${PUID}:${PGID}" "$@"
