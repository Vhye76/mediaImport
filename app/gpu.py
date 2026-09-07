import logging
import os
import subprocess

log = logging.getLogger("gpu")

RENDER_NODE = os.environ.get("RENDER_NODE", "/dev/dri/renderD128")
VAINFO = os.environ.get("VAINFO", "vainfo")

REQUIRED_PROFILE = "VAProfileAV1Profile0"
REQUIRED_ENTRYPOINT = "VAEntrypointEncSlice"


class GpuStatus:
    def __init__(self, available, reason, render_node=None, profiles=None):
        self.available = available
        self.reason = reason
        self.render_node = render_node
        self.profiles = profiles or []

    @property
    def degraded(self):
        return not self.available

    def as_dict(self):
        return {
            "available": self.available,
            "degraded": self.degraded,
            "reason": self.reason,
            "render_node": self.render_node,
            "av1_encode_profiles": self.profiles,
        }

    def __repr__(self):
        return "<GpuStatus available=%s reason=%r>" % (self.available, self.reason)


def _vainfo(render_node, timeout=30):
    log.debug("running %s --display drm --device %s", VAINFO, render_node)
    return subprocess.run(
        [VAINFO, "--display", "drm", "--device", render_node],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_vainfo(text):
    profiles = []
    for line in (text or "").splitlines():
        line = line.strip()
        if REQUIRED_PROFILE in line and REQUIRED_ENTRYPOINT in line:
            profiles.append(" ".join(line.split()))
    return profiles


def probe(cfg=None, render_node=None):
    status = _probe(cfg, render_node)
    if status.available:
        log.info("gpu available on %s: %s", status.render_node, status.reason)
    else:
        log.warning("gpu degraded on %s: %s", status.render_node, status.reason)
    return status


def _probe(cfg=None, render_node=None):
    node = render_node or getattr(cfg, "render_node", None) or RENDER_NODE
    log.debug("probing render node %s", node)

    if cfg is not None and not cfg.gpu_enabled:
        return GpuStatus(False, "RENDER_GID is unset, GPU support is not configured", node)

    if not os.path.exists(node):
        return GpuStatus(False, "render node %s does not exist" % node, node)

    if not os.access(node, os.R_OK | os.W_OK):
        return GpuStatus(
            False,
            "render node %s is not readable and writable by this process, check RENDER_GID" % node,
            node,
        )

    try:
        proc = _vainfo(node)
    except FileNotFoundError:
        return GpuStatus(False, "vainfo is not installed in this image", node)
    except subprocess.TimeoutExpired:
        return GpuStatus(False, "vainfo timed out against %s" % node, node)

    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return GpuStatus(
            False,
            "vainfo exited %d: %s" % (proc.returncode, combined.strip()[-300:]),
            node,
        )

    profiles = parse_vainfo(combined)
    log.debug("vainfo returned %d line(s), %d matching profile(s)",
              len(combined.splitlines()), len(profiles))
    for line in profiles:
        log.debug("profile %s", line)
    if not profiles:
        return GpuStatus(
            False,
            "%s with %s not reported by the driver, AV1 hardware encode is unavailable"
            % (REQUIRED_PROFILE, REQUIRED_ENTRYPOINT),
            node,
        )

    return GpuStatus(True, "AV1 hardware encode available", node, profiles)
