import logging
import os
import shutil

from . import locks

log = logging.getLogger("paths")


class WriteGuardError(PermissionError):
    pass


class LayoutError(RuntimeError):
    pass


def _norm(p):
    return os.path.normpath(os.path.realpath(os.path.abspath(os.path.expanduser(str(p)))))


def _under(path, root):
    path = _norm(path)
    root = _norm(root)
    return path == root or path.startswith(root + os.sep)


class Layout:
    def __init__(self, cfg):
        self.cfg = cfg
        self.encode = _norm(cfg.media_encode)

        self.imports = _norm(cfg.media_import)
        self.held = _norm(cfg.media_hold)
        self.completed = _norm(cfg.media_complete)
        self.config = _norm(cfg.media_config)
        self.quarantine = os.path.join(self.completed, ".quarantine")

        self.libraries = {}
        if cfg.library_movies:
            self.libraries["movie"] = _norm(cfg.library_movies)
        if cfg.library_tv:
            self.libraries["tv"] = _norm(cfg.library_tv)

        self.read_only_roots = tuple(self.libraries.values())
        self.writable_roots = (
            self.imports,
            self.held,
            self.completed,
            self.config,
            self.encode,
        )
        self.work_dirs = (
            self.imports,
            self.held,
            self.completed,
            self.quarantine,
            self.config,
            self.encode,
        )

    @property
    def instance_lock(self):
        return os.path.join(self.config, "mediaimport.lock")

    @property
    def state_db(self):
        return os.path.join(self.config, "state.db")

    @property
    def provider_cache(self):
        return os.path.join(self.config, "cache")

    @property
    def logs(self):
        return os.path.join(self.config, "logs")

    def is_read_only(self, path):
        for root in self.read_only_roots:
            if _under(path, root):
                return root
        return None

    def assert_writable(self, path):
        root = self.is_read_only(path)
        if root is not None:
            log.info("write guard refused %s, under read-only library %s", _norm(path), root)
            raise WriteGuardError(
                "refusing to write to %s: path is under read-only library %s" % (_norm(path), root)
            )
        target = _norm(path)
        if not any(_under(target, root) for root in self.writable_roots):
            log.info("write guard refused %s, outside every writable mount", target)
            log.debug("writable mounts are %s", ", ".join(self.writable_roots))
            raise WriteGuardError(
                "refusing to write to %s: path is outside every writable mount (%s)"
                % (target, ", ".join(self.writable_roots))
            )
        return target

    def guarded_makedirs(self, path, **kw):
        self.assert_writable(path)
        kw.setdefault("exist_ok", True)
        return os.makedirs(path, **kw)

    def guarded_replace(self, src, dst):
        self.assert_writable(dst)
        return os.replace(src, dst)

    def guarded_open(self, path, mode="r", **kw):
        if any(c in mode for c in "wxa+"):
            self.assert_writable(path)
        return open(path, mode, **kw)

    def ensure(self):
        for d in self.work_dirs:
            self.guarded_makedirs(d)
        for d in (self.config, self.logs, self.provider_cache):
            self.guarded_makedirs(d)
        for kind, root in self.libraries.items():
            if not os.path.isdir(root):
                raise LayoutError(
                    "LIBRARY_%s points at %s which is not a directory" % (kind.upper(), root)
                )

    def encode_is_separate(self):
        try:
            return os.stat(self.encode).st_dev != os.stat(self.completed).st_dev
        except OSError:
            return False

    def encode_free_bytes(self):
        return shutil.disk_usage(self.encode).free

    def has_headroom(self, source_bytes):
        need = int(source_bytes * self.cfg.encode_headroom)
        free = self.encode_free_bytes()
        log.debug(
            "admission check: need %d bytes at headroom %s, %d free on %s",
            need, self.cfg.encode_headroom, free, self.encode,
        )
        if free < need:
            log.info("admission deferred, encode area has %d bytes free, needs %d", free, need)
        return free >= need, need

    def job_dir(self, job_id):
        return self.assert_writable(os.path.join(self.encode, str(job_id)))

    def make_job_dir(self, job_id):
        d = self.job_dir(job_id)
        self.guarded_makedirs(d)
        locks.claim_job_dir(d)
        log.info("job directory created at %s", d)
        return d

    def wipe_job_dir(self, job_id):
        d = self.job_dir(job_id)
        log.info("wiping job directory %s", d)
        shutil.rmtree(d, ignore_errors=True)

    def sweep_encode(self):
        removed = []
        skipped = []
        if not os.path.isdir(self.encode):
            return removed, skipped
        for name in sorted(os.listdir(self.encode)):
            target = os.path.join(self.encode, name)
            if not os.path.isdir(target):
                continue
            self.assert_writable(target)
            owner = locks.job_owner(target)
            if not locks.job_is_orphaned(target):
                log.debug("job %s still owned by pid %s, not swept", name, (owner or {}).get("pid"))
                skipped.append((name, owner))
                continue
            log.info("sweeping orphaned job directory %s", name)
            log.debug("job %s was owned by pid %s", name, (owner or {}).get("pid"))
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
        return removed, skipped

    def reserve(self, destination):
        self.assert_writable(destination)
        self.guarded_makedirs(os.path.dirname(destination))
        try:
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            raise FileExistsError(
                "destination already exists, refusing to overwrite: %s" % destination
            )
        os.close(fd)
        return destination

    def publish_file(self, source, destination):
        self.reserve(destination)
        staging = destination + ".incoming"
        try:
            shutil.copy2(source, staging)
            os.replace(staging, destination)
        except BaseException:
            for leftover in (staging, destination):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            raise
        os.remove(source)
        log.info("published %s", destination)
        return destination

    def unique_path(self, directory, basename):
        self.assert_writable(directory)
        self.guarded_makedirs(directory)
        stem, ext = os.path.splitext(basename)
        candidate = os.path.join(directory, basename)
        n = 0
        while True:
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                n += 1
                candidate = os.path.join(directory, "%s.%d%s" % (stem, n, ext))
                continue
            os.close(fd)
            return candidate

    def quarantine_path(self, src):
        base = os.path.basename(os.path.normpath(src))
        return self.unique_path(self.quarantine, base)

    def held_path(self, src):
        base = os.path.basename(os.path.normpath(src))
        return self.unique_path(self.held, base)

    def describe(self):
        lines = [
            "import       %s" % self.imports,
            "hold         %s" % self.held,
            "complete     %s" % self.completed,
            "quarantine   %s" % self.quarantine,
            "config       %s" % self.config,
            "encode       %s  (%s)"
            % (
                self.encode,
                "separate filesystem"
                if self.encode_is_separate()
                else "SAME FILESYSTEM as complete",
            ),
        ]
        if self.libraries:
            for kind, root in sorted(self.libraries.items()):
                lines.append("library %-4s %s  (read only)" % (kind, root))
        else:
            lines.append("library      none mounted, incumbent comparison DISABLED")
        return "\n".join(lines)
