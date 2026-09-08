import logging
import os

log = logging.getLogger("encode")

PASSTHROUGH_CODECS = ("hevc", "av1")
SD_DISPLAY_HEIGHT = 720

X265_COMMON = "psy-rd=2.0:psy-rdoq=1.0:deblock=-1,-1"
AQ_DEFAULT = "aq-mode=3"
AQ_FILM = "aq-mode=4:tune=grain"
X265_SDR_COLOUR = "colorprim=smpte170m:transfer=smpte170m:colormatrix=smpte170m:range=limited"

X265_PRESET = "slow"
SVTAV1_PRESET = "4"
SVTAV1_CRF = 24
SVTAV1_PARAMS = "tune=0:film-grain=8"

QSV_PRESET = "veryslow"
QSV_GLOBAL_QUALITY = 26

RENDER_NODE = os.environ.get("RENDER_NODE", "/dev/dri/renderD128")

PASSTHROUGH = "passthrough"
ENCODE = "encode"

LIBX265 = "libx265"
LIBSVTAV1 = "libsvtav1"
AV1_QSV = "av1_qsv"

CPU = "cpu"
GPU = "gpu"

DEVICE_BY_ENCODER = {LIBX265: CPU, LIBSVTAV1: CPU, AV1_QSV: GPU}


class Decision:
    def __init__(self, action, gate, reason, encoder=None, grain=None, notes=None):
        self.action = action
        self.gate = gate
        self.reason = reason
        self.encoder = encoder
        self.device = DEVICE_BY_ENCODER.get(encoder)
        self.grain = grain
        self.notes = list(notes or [])

    @property
    def is_passthrough(self):
        return self.action == PASSTHROUGH

    def as_dict(self):
        return {
            "action": self.action,
            "gate": self.gate,
            "reason": self.reason,
            "encoder": self.encoder,
            "device": self.device,
            "grain": self.grain,
            "notes": self.notes,
        }

    def __repr__(self):
        return "<Decision %s gate=%d encoder=%s device=%s>" % (
            self.action,
            self.gate,
            self.encoder,
            self.device,
        )


def _threads(cfg):
    value = getattr(cfg, "encode_threads_per_job", None)
    log.debug("encoder thread figure resolved to %s", value)
    if value:
        return max(1, int(value))
    return max(1, os.cpu_count() or 1)


def is_sd(video):
    return int(video.get("display_height") or 0) < SD_DISPLAY_HEIGHT


def select(video, kind, cfg, grain=None, gpu_available=True, override=None):
    decision = _select(video, kind, cfg, grain, gpu_available, override)
    log.debug(
        "router gate %s: %s, %s",
        decision.gate, decision.encoder or "passthrough", decision.reason,
    )
    return decision


def describe(decision):
    if decision.is_passthrough:
        return "gate %s: passthrough, %s" % (decision.gate, decision.reason)
    return "gate %s: %s, %s" % (decision.gate, decision.encoder, decision.reason)


def _select(video, kind, cfg, grain=None, gpu_available=True, override=None):
    override = override or {}
    notes = []
    log.debug(
        "routing %s %s, grain=%s, gpu_available=%s, override=%s",
        kind, video.get("codec"), grain, gpu_available, override,
    )

    codec = (video.get("codec") or "").lower()
    if codec in PASSTHROUGH_CODECS:
        return Decision(
            PASSTHROUGH,
            1,
            "source video is already %s" % codec,
            grain=grain,
        )

    if kind == "tv" and is_sd(video) and not cfg.tv_encode_sd:
        return Decision(
            PASSTHROUGH,
            2,
            "SD television, display height %s is below %d"
            % (video.get("display_height"), SD_DISPLAY_HEIGHT),
            grain=grain,
        )

    if "film" in override:
        grain = bool(override["film"])
        notes.append("grain forced to %s by encode.job" % grain)

    if video.get("dolby_vision"):
        return Decision(
            ENCODE,
            3,
            "Dolby Vision RPU present, an AV1 re-encode would discard it",
            encoder=LIBX265,
            grain=grain,
            notes=notes,
        )

    codec_target = (override.get("output_codec") or cfg.output_codec).lower()

    if codec_target == "av1":
        if grain:
            return Decision(
                ENCODE,
                4,
                "AV1 requested and source is grainy, film-grain synthesis needs the CPU encoder",
                encoder=LIBSVTAV1,
                grain=grain,
                notes=notes,
            )
        if not gpu_available:
            notes.append("GPU unavailable, av1_qsv fell back to libsvtav1")
            return Decision(
                ENCODE,
                5,
                "AV1 requested but the GPU is unavailable",
                encoder=LIBSVTAV1,
                grain=grain,
                notes=notes,
            )
        return Decision(
            ENCODE,
            5,
            "AV1 requested and source is not grainy",
            encoder=AV1_QSV,
            grain=grain,
            notes=notes,
        )

    if grain:
        return Decision(
            ENCODE,
            6,
            "grainy source, x265 grain tune",
            encoder=LIBX265,
            grain=grain,
            notes=notes,
        )
    return Decision(
        ENCODE,
        7,
        "clean source, x265 default",
        encoder=LIBX265,
        grain=grain,
        notes=notes,
    )


def _needs_sdr_stamp(video):
    return not video.get("hdr") and not video.get("colour_tagged")


def _map_args():
    return [
        "-map", "0:v:0",
        "-map", "0:a",
        "-map", "0:s?",
        "-map", "0:t?",
        "-map_chapters", "0",
    ]


def _tail_args():
    return ["-c:a", "copy", "-c:s", "copy", "-map_metadata", "0"]


def _sdr_ffmpeg_colour_args():
    return [
        "-color_primaries", "smpte170m",
        "-color_trc", "smpte170m",
        "-colorspace", "smpte170m",
        "-color_range", "tv",
    ]


def build_command(decision, src, dst, video, cfg, crop=None, crf=None):
    if decision.is_passthrough:
        raise ValueError("build_command called on a passthrough decision")

    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1"]

    if decision.encoder == AV1_QSV:
        node = getattr(cfg, "render_node", RENDER_NODE)
        args += ["-init_hw_device", "qsv=hw:%s" % node, "-filter_hw_device", "hw"]

    args += ["-i", str(src)]
    args += _map_args()

    filters = []
    if crop:
        filters.append(crop)
    if decision.encoder == AV1_QSV:
        filters += ["format=p010le", "hwupload=extra_hw_frames=64"]
    if filters:
        args += ["-vf", ",".join(filters)]

    stamp = _needs_sdr_stamp(video)

    if decision.encoder == LIBX265:
        aq = AQ_FILM if decision.grain else AQ_DEFAULT
        params = "%s:%s" % (aq, X265_COMMON)
        if stamp:
            params = "%s:%s" % (params, X265_SDR_COLOUR)
        params = "%s:pools=%d" % (params, _threads(cfg))
        args += [
            "-c:v", "libx265",
            "-preset", X265_PRESET,
            "-crf", str(crf if crf is not None else cfg.crf),
            "-pix_fmt", "yuv420p10le",
            "-x265-params", params,
        ]
        if stamp:
            args += ["-color_range", "tv"]

    elif decision.encoder == LIBSVTAV1:
        args += [
            "-c:v", "libsvtav1",
            "-preset", SVTAV1_PRESET,
            "-crf", str(crf if crf is not None else SVTAV1_CRF),
            "-pix_fmt", "yuv420p10le",
            "-svtav1-params", "%s:lp=%d" % (SVTAV1_PARAMS, _threads(cfg)),
        ]
        if stamp:
            args += _sdr_ffmpeg_colour_args()

    elif decision.encoder == AV1_QSV:
        args += [
            "-c:v", "av1_qsv",
            "-preset", QSV_PRESET,
            "-global_quality", str(crf if crf is not None else QSV_GLOBAL_QUALITY),
        ]
        if stamp:
            args += _sdr_ffmpeg_colour_args()

    else:
        raise ValueError("unknown encoder %r" % decision.encoder)

    args += _tail_args()
    args += ["-f", "matroska", str(dst)]
    log.debug("encode argv: %s", " ".join(str(a) for a in args))
    return args
