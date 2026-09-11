FROM alpine:3.24

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LIBVA_DRIVER_NAME=iHD

#----- Base image and packages
RUN set -eux; \
    apk add --no-cache \
        ffmpeg \
        mkvtoolnix \
        python3 \
        bash \
        coreutils \
        findutils \
        jq \
        ca-certificates \
        tini \
        su-exec \
        shadow \
        libcap \
        libcap-utils \
        libva \
        libva-utils \
        intel-media-driver \
        libvpl \
        onevpl-intel-gpu \
    ; \
    ln -sf /sbin/nologin /usr/sbin/nologin

#----- Build gate:  the image must not ship claiming encoders it lacks
RUN set -eux; \
    missing=""; \
    for enc in libx265 libsvtav1 av1_qsv; do \
        ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "[[:space:]]${enc}[[:space:]]" \
            || missing="${missing} ${enc}"; \
    done; \
    if [ -n "${missing}" ]; then \
        echo "BUILD GATE FAILED: ffmpeg in this image lacks:${missing}" >&2; \
        echo "Do NOT delete this check. It exists so the image cannot ship claiming" >&2; \
        echo "encoders it does not have. Escalate the ffmpeg source instead:" >&2; \
        echo "  1. av1_vaapi   same hardware, VAAPI rather than oneVPL" >&2; \
        echo "  2. ffmpeg from the alpine edge community repository, pinned" >&2; \
        exit 1; \
    fi; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -E "libx265|libsvtav1|av1_qsv|av1_vaapi"; \
    if ! ffmpeg -hide_banner -h encoder=libx265 2>/dev/null | grep -q "^  -dolbyvision"; then \
        echo "BUILD GATE FAILED: this ffmpeg's libx265 wrapper has no -dolbyvision option," >&2; \
        echo "so a Dolby Vision RPU cannot be carried through an encode. Needs ffmpeg 7.1 or later." >&2; \
        exit 1; \
    fi

#----- Image metadata
LABEL org.opencontainers.image.title="mediaimport" \
      org.opencontainers.image.description="Automatic media import, tag and encode pipeline" \
      org.opencontainers.image.source="https://github.com/Vhye76/mediaImport" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/Vhye76/mediaImport/main/media/mediaImport.png" \
      mediaimport.ffmpeg="alpine"

#----- Application
WORKDIR /opt/mediaimport
COPY app/ ./app/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

#----- Binding 443 as a non-root process needs the capability on the resolved binary
RUN set -eux; \
    target="$(readlink -f /usr/bin/python3)"; \
    setcap cap_net_bind_service=+ep "${target}"; \
    getcap "${target}" | grep -q cap_net_bind_service

EXPOSE 443

#----- Liveness, not identity:  a loopback probe cannot verify a hostname certificate
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import ssl,urllib.request,os; \
        ctx=ssl._create_unverified_context(); \
        urllib.request.urlopen('https://127.0.0.1:%s/api/status' % os.environ.get('WEB_PORT','443'), timeout=5, context=ctx)" \
        || exit 1

#----- Entry
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python3", "-m", "app.main"]
