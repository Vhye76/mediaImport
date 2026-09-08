import errno
import fcntl
import json
import logging
import os
import time

log = logging.getLogger("locks")

OWNER_FILE = "owner.json"


class AlreadyRunning(RuntimeError):
    pass


class LockWaitInterrupted(RuntimeError):
    pass


DEFAULT_WAIT_INTERVAL = 15
DEFAULT_REPORT_EVERY = 300


#----- The single-instance lock
class InstanceLock:
    def __init__(self, path):
        self.path = str(path)
        self._fd = None

    def acquire(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = _read_holder(fd)
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning(
                    "another mediaimport instance already holds %s%s"
                    % (self.path, holder)
                )
            raise
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "started": time.time()}).encode())
        os.fsync(fd)
        self._fd = fd
        log.info("instance lock acquired on %s by pid %d", self.path, os.getpid())
        return self

    def acquire_blocking(self, stop_event=None, timeout=0,
                         interval=DEFAULT_WAIT_INTERVAL,
                         report_every=DEFAULT_REPORT_EVERY, on_wait=None):
        deadline = (time.time() + timeout) if timeout else None
        waited = 0.0
        last_report = None
        while True:
            if stop_event is not None and stop_event.is_set():
                raise LockWaitInterrupted("asked to stop while waiting for the instance lock")
            try:
                return self.acquire()
            except AlreadyRunning as exc:
                if deadline is not None and time.time() >= deadline:
                    raise
                if on_wait and (last_report is None or waited - last_report >= report_every):
                    on_wait(exc, waited)
                    last_report = waited
            if stop_event is not None:
                if stop_event.wait(interval):
                    raise LockWaitInterrupted(
                        "asked to stop while waiting for the instance lock"
                    )
            else:
                time.sleep(interval)
            waited += interval

    def release(self):
        if self._fd is None:
            return
        log.info("instance lock released on %s", self.path)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


#----- Holder identity
def _read_holder(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = json.loads(os.read(fd, 4096) or b"{}")
    except (OSError, ValueError):
        return ""
    pid = data.get("pid")
    if not pid:
        return ""
    return " (pid %s, %s)" % (pid, "alive" if pid_alive(pid) else "not alive")


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


#----- Job directory ownership
def claim_job_dir(job_dir):
    log.debug("claiming job directory %s for pid %d", job_dir, os.getpid())
    with open(os.path.join(job_dir, OWNER_FILE), "w") as fh:
        json.dump({"pid": os.getpid(), "started": time.time()}, fh)


def job_owner(job_dir):
    try:
        with open(os.path.join(job_dir, OWNER_FILE)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def job_is_orphaned(job_dir):
    owner = job_owner(job_dir)
    if owner is None:
        log.debug("job directory %s has no owner file, treated as orphaned", job_dir)
        return True
    alive = pid_alive(owner.get("pid"))
    log.debug("job directory %s owned by pid %s, alive=%s", job_dir, owner.get("pid"), alive)
    return not alive
