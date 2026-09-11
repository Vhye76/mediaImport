import errno
import logging
import os
import shutil

from . import locks

log = logging.getLogger("paths")


class WriteGuardError(PermissionError):
    pass


class LayoutError(RuntimeError):
    pass


#----- Path helpers
def _norm(p):
    return os.path.normpath(os.path.realpath(os.path.abspath(os.path.expanduser(str(p)))))


def _under(path, root):
    path = _norm(path)
    root = _norm(root)
    return path == root or path.startswith(root + os.sep)


#----- The mount contract
class Layout:
    def __init__(self, cfg):
        self.cfg = cfg
        self.media_root = _norm(cfg.media_root)

        self.imports = os.path.join(self.media_root, "import")
        self.held = os.path.join(self.media_root, "hold")
        self.completed = os.path.join(self.media_root, "complete")
        self.quarantine = os.path.join(self.completed, ".quarantine")

        self.encode = _norm(cfg.media_encode)
        self.config = _norm(cfg.media_config)

        self.libraries = {}
        if cfg.library_movies:
            self.libraries["movie"] = _norm(cfg.library_movies)
        if cfg.library_tv:
            self.libraries["tv"] = _norm(cfg.library_tv)

        self.read_only_roots = tuple(self.libraries.values())
        roots = [self.media_root]
        for extra in (self.encode, self.config):
            if not _under(extra, self.media_root):
                roots.append(extra)
        self.writable_roots = tuple(roots)
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

    #----- Write guards
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

    #----- The encode area
    def encode_is_separate(self):
        try:
            return os.stat(self.encode).st_dev != os.stat(self.completed).st_dev
        except OSError:
            return False

    def encode_free_bytes(self):
        return shutil.disk_usage(self.encode).free

    def root_free_bytes(self):
        return shutil.disk_usage(self.media_root).free

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

    #----- Reserving, moving and publishing
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

    def move_file(self, source, destination):
        self.assert_writable(destination)
        self.guarded_makedirs(os.path.dirname(destination))
        try:
            os.replace(source, destination)
        #----- rename fails across mount points even when both sides are one device.
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                self._discard_reservation(destination)
                raise
            log.debug("rename crossed a mount boundary, falling back to copy")
            staging = destination + ".incoming"
            try:
                shutil.copy2(source, staging)
                os.replace(staging, destination)
            except BaseException:
                self._discard_reservation(staging)
                self._discard_reservation(destination)
                raise
            os.remove(source)
        return destination

    def _discard_reservation(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def publish_file(self, source, destination):
        self.reserve(destination)
        self.move_file(source, destination)
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

    def import_destination(self, source):
        source = _norm(source)
        if self.is_read_only(source) is None:
            raise WriteGuardError("refusing to import %s: it is not under a mounted library" % source)
        size = os.path.getsize(source)
        free = self.root_free_bytes()
        if free < size:
            raise OSError(
                errno.ENOSPC,
                "import area has %d bytes free, the copy needs %d" % (free, size),
            )
        destination = os.path.join(self.imports, os.path.basename(source))
        if os.path.exists(destination):
            raise FileExistsError("already present in import: %s" % destination)
        self.assert_writable(destination)
        return destination

    def copy_to_import(self, source):
        source = _norm(source)
        destination = self.import_destination(source)
        self.guarded_makedirs(self.imports)
        #----- the watcher ignores '.part', so the copy is invisible until the rename.
        staging = destination + ".part"
        try:
            shutil.copy2(source, staging)
            os.replace(staging, destination)
        except BaseException:
            self._discard_reservation(staging)
            raise
        log.info("copied %s into import for repair", os.path.basename(source))
        return destination

    #----- Reporting
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
