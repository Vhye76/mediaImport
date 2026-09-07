FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LIBVA_DRIVER_NAME=iHD

RUN set -eux; \
    sed -i 's/^Components: .*/Components: main contrib non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
        python3 \
        bash \
        coreutils \
        findutils \
        jq \
        ca-certificates \
        tini \
        gosu \
        libcap2-bin \
        vainfo \
        intel-media-va-driver-non-free \
        libvpl2 \
        libigdgmm12 \
        intel-gpu-tools \
    ; \
    rm -rf /var/lib/apt/lists/*

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
        echo "  2. jellyfin-ffmpeg from the Jellyfin apt repository" >&2; \
        exit 1; \
    fi; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -E "libx265|libsvtav1|av1_qsv|av1_vaapi"

LABEL org.opencontainers.image.title="mediaimport" \
      org.opencontainers.image.description="Automatic media import, tag and encode pipeline" \
      org.opencontainers.image.source="https://github.com/Vhye76/mediaImport" \
      mediaimport.ffmpeg="debian"

WORKDIR /opt/mediaimport
COPY app/ ./app/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN set -eux; \
    target="$(readlink -f /usr/bin/python3)"; \
    setcap cap_net_bind_service=+ep "${target}"; \
    getcap "${target}" | grep -q cap_net_bind_service

EXPOSE 443

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import ssl,urllib.request,os; \
        ctx=ssl._create_unverified_context(); \
        urllib.request.urlopen('https://127.0.0.1:%s/api/status' % os.environ.get('WEB_PORT','443'), timeout=5, context=ctx)" \
        || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python3", "-m", "app.main"]
