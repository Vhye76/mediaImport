import os


class ConfigError(RuntimeError):
    pass


_SOURCES = {}


def _source(name, from_env):
    _SOURCES[name] = "environment" if from_env else "default"


def _str(name, default=None, required=False):
    v = os.environ.get(name)
    _source(name, bool(v))
    if v is None or v == "":
        if required:
            raise ConfigError("%s is required and is not set" % name)
        return default
    return v


def _int(name, default):
    v = os.environ.get(name)
    _source(name, bool(v))
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise ConfigError("%s must be an integer, got %r" % (name, v))


def _float(name, default):
    v = os.environ.get(name)
    _source(name, bool(v))
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        raise ConfigError("%s must be a number, got %r" % (name, v))


def _bool(name, default=False):
    v = os.environ.get(name)
    _source(name, bool(v))
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


VALID_CODECS = ("hevc", "av1")

CPU_MAX_PATH = "/sys/fs/cgroup/cpu.max"


def parse_cpu_max(text):
    if not text:
        return None
    parts = text.split()
    if len(parts) < 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, quota // period)


def detect_cpus(path=CPU_MAX_PATH):
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_cpu_max(fh.read())
    except OSError:
        return None


class Config:
    def __init__(self, env=None):
        if env is not None:
            saved = dict(os.environ)
            os.environ.clear()
            os.environ.update(env)
            try:
                self._load()
            finally:
                os.environ.clear()
                os.environ.update(saved)
        else:
            self._load()

    def _load(self):
        _SOURCES.clear()
        self.media_root = _str("MEDIA_ROOT", "/media")
        encode_override = _str("MEDIA_ENCODE")
        config_override = _str("MEDIA_CONFIG")
        self.media_encode = encode_override or os.path.join(self.media_root, "encode")
        self.media_config = config_override or os.path.join(self.media_root, "config")
        self.encode_mounted = bool(encode_override)
        self.config_mounted = bool(config_override)
        self.library_movies = _str("LIBRARY_MOVIES")
        self.library_tv = _str("LIBRARY_TV")

        self.cert_dir = _str("CERT_DIR", "/certs")
        self.tls_cert_file = _str("TLS_CERT_FILE", "fullchain.pem")
        self.tls_key_file = _str("TLS_KEY_FILE", "privkey.pem")

        self.puid = _int("PUID", -1)
        self.pgid = _int("PGID", -1)
        self.render_gid = _int("RENDER_GID", -1)

        self.output_codec = _str("OUTPUT_CODEC", "hevc").lower()
        if self.output_codec not in VALID_CODECS:
            raise ConfigError(
                "OUTPUT_CODEC must be one of %s, got %r"
                % (", ".join(VALID_CODECS), self.output_codec)
            )

        self.max_jobs = _int("MAX_JOBS", 3)
        self.gpu_slots = _int("GPU_SLOTS", 1)
        self.cpu_slots = _int("CPU_SLOTS", 1)
        self.encode_headroom = _float("ENCODE_HEADROOM", 3.0)
        self.crf = _int("CRF", 18)
        self.tv_encode_sd = _bool("TV_ENCODE_SD", False)
        self.encode_threads = _int("ENCODE_THREADS", 0)

        self.web_port = _int("WEB_PORT", 443)
        self.dry_run = _bool("DRY_RUN", False)
        self.poll_interval = _int("POLL_INTERVAL", 60)
        self.mtime_quiet = _int("MTIME_QUIET", 120)
        self.log_level = _str("LOG_LEVEL", "info").lower()
        self.render_node = _str("RENDER_NODE", "/dev/dri/renderD128")
        self.grain_threshold = _float("GRAIN_THRESHOLD", 0.18)
        self.lock_wait_timeout = _int("LOCK_WAIT_TIMEOUT", 0)
        self.lock_wait_interval = _int("LOCK_WAIT_INTERVAL", 15)

        for name, value in (
            ("MAX_JOBS", self.max_jobs),
            ("GPU_SLOTS", self.gpu_slots),
            ("CPU_SLOTS", self.cpu_slots),
            ("POLL_INTERVAL", self.poll_interval),
            ("MTIME_QUIET", self.mtime_quiet),
        ):
            if value < 0:
                raise ConfigError("%s must not be negative, got %d" % (name, value))
        if self.max_jobs < 1:
            raise ConfigError("MAX_JOBS must be at least 1")
        if self.gpu_slots + self.cpu_slots < 1:
            raise ConfigError("GPU_SLOTS and CPU_SLOTS must not both be zero")
        if not 0.0 < self.grain_threshold < 1.0:
            raise ConfigError(
                "GRAIN_THRESHOLD must be between 0 and 1, got %s" % self.grain_threshold
            )
        if self.lock_wait_interval < 1:
            raise ConfigError("LOCK_WAIT_INTERVAL must be at least 1 second")
        if self.lock_wait_timeout < 0:
            raise ConfigError("LOCK_WAIT_TIMEOUT must not be negative")
        if self.encode_headroom < 1.0:
            raise ConfigError(
                "ENCODE_HEADROOM must be at least 1.0, got %s" % self.encode_headroom
            )
        if self.encode_threads < 0:
            raise ConfigError(
                "ENCODE_THREADS must not be negative, got %d" % self.encode_threads
            )
        required = [("MEDIA_ROOT", self.media_root)]
        if self.encode_mounted:
            required.append(("MEDIA_ENCODE", self.media_encode))
        if self.config_mounted:
            required.append(("MEDIA_CONFIG", self.media_config))
        for name, path in required:
            if not os.path.isdir(path):
                raise ConfigError("%s points at %s which is not a mounted directory" % (name, path))

        autodetected = self.encode_threads == 0
        detected_from_cgroup = False
        if autodetected:
            detected = detect_cpus()
            detected_from_cgroup = detected is not None
            self.encode_threads = detected or os.cpu_count() or 1
        if not 0 <= self.crf <= 51:
            raise ConfigError("CRF must be between 0 and 51, got %d" % self.crf)
        if not 1 <= self.web_port <= 65535:
            raise ConfigError("WEB_PORT must be a valid port, got %d" % self.web_port)

        self.sources = dict(_SOURCES)
        self.thread_source = (
            "cgroup cpu.max" if detected_from_cgroup else "os.cpu_count"
        ) if autodetected else "environment"

    @property
    def tls_cert(self):
        return os.path.join(self.cert_dir, self.tls_cert_file)

    @property
    def tls_key(self):
        return os.path.join(self.cert_dir, self.tls_key_file)

    @property
    def encode_threads_per_job(self):
        return max(1, self.encode_threads // max(1, self.cpu_slots))

    @property
    def gpu_enabled(self):
        return self.render_gid >= 0

    @property
    def libraries_mounted(self):
        return bool(self.library_movies or self.library_tv)

    def as_dict(self):
        return {
            "MEDIA_ROOT": self.media_root,
            "MEDIA_ENCODE": self.media_encode,
            "MEDIA_CONFIG": self.media_config,
            "LIBRARY_MOVIES": self.library_movies,
            "LIBRARY_TV": self.library_tv,
            "CERT_DIR": self.cert_dir,
            "TLS_CERT_FILE": self.tls_cert_file,
            "TLS_KEY_FILE": self.tls_key_file,
            "PUID": self.puid,
            "PGID": self.pgid,
            "RENDER_GID": self.render_gid,
            "OUTPUT_CODEC": self.output_codec,
            "MAX_JOBS": self.max_jobs,
            "GPU_SLOTS": self.gpu_slots,
            "CPU_SLOTS": self.cpu_slots,
            "ENCODE_HEADROOM": self.encode_headroom,
            "CRF": self.crf,
            "TV_ENCODE_SD": self.tv_encode_sd,
            "ENCODE_THREADS": self.encode_threads,
            "WEB_PORT": self.web_port,
            "DRY_RUN": self.dry_run,
            "POLL_INTERVAL": self.poll_interval,
            "MTIME_QUIET": self.mtime_quiet,
            "LOG_LEVEL": self.log_level,
            "RENDER_NODE": self.render_node,
            "GRAIN_THRESHOLD": self.grain_threshold,
            "LOCK_WAIT_TIMEOUT": self.lock_wait_timeout,
            "LOCK_WAIT_INTERVAL": self.lock_wait_interval,
        }

    def banner(self):
        rows = self.as_dict()
        width = max(len(k) for k in rows)
        return "\n".join(
            "%-*s  %s" % (width, k, "(unset)" if v is None else v) for k, v in rows.items()
        )
