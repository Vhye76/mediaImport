import logging
import os
import queue
import shutil
import threading
import time
import uuid

from . import compare, encode, media, probe as probemod, provider as providermod
from . import standards, state, tags, titles

log = logging.getLogger("orchestrator")

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv")
JOB_SIDECAR = "encode.job"


def read_sidecar(directory):
    path = os.path.join(directory, JOB_SIDECAR)
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().lower()] = value.strip()
    out = {}
    if "film" in values:
        out["film"] = values["film"] not in ("0", "false", "no")
    if "codec" in values:
        out["output_codec"] = values["codec"].lower()
    if "crf" in values:
        try:
            out["crf"] = int(values["crf"])
        except ValueError:
            pass
    if "crop" in values:
        out["crop"] = "crop=%s" % values["crop"]
    return out


class Slots:
    def __init__(self, cfg):
        self.gpu = threading.Semaphore(max(cfg.gpu_slots, 0) or 1)
        self.cpu = threading.Semaphore(max(cfg.cpu_slots, 0) or 1)
        self.gpu_active = 0
        self.cpu_active = 0
        self._lock = threading.Lock()

    def acquire(self, device):
        sem = self.gpu if device == encode.GPU else self.cpu
        sem.acquire()
        with self._lock:
            if device == encode.GPU:
                self.gpu_active += 1
            else:
                self.cpu_active += 1
        return sem

    def release(self, device, sem):
        with self._lock:
            if device == encode.GPU:
                self.gpu_active -= 1
            else:
                self.cpu_active -= 1
        sem.release()

    def snapshot(self):
        with self._lock:
            return {"gpu_active": self.gpu_active, "cpu_active": self.cpu_active}


class HoldError(RuntimeError):
    pass


class QuarantineError(RuntimeError):
    pass


class RetryLater(RuntimeError):
    pass


RETRY_MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 120


class Orchestrator:
    def __init__(self, cfg, layout, store, gpu_status, provider=None):
        self.cfg = cfg
        self.layout = layout
        self.store = store
        self.gpu = gpu_status
        self.provider = provider
        self.slots = Slots(cfg)
        self.queue = queue.Queue()
        self.workers = []
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self._seen_sizes = {}
        self._procs = set()
        self._procs_lock = threading.Lock()

    def start(self):
        removed, skipped = self.layout.sweep_encode()
        if removed:
            log.info("startup swept %d orphaned encode job(s): %s", len(removed), ", ".join(removed))
        for name, owner in skipped:
            log.warning(
                "encode job %s is owned by live pid %s and was NOT swept",
                name, (owner or {}).get("pid"),
            )
        for n in range(self.cfg.max_jobs):
            t = threading.Thread(target=self._worker, name="worker-%d" % n, daemon=True)
            t.start()
            self.workers.append(t)
        t = threading.Thread(target=self._watch, name="watcher", daemon=True)
        t.start()
        self.workers.append(t)
        self.requeue_resumable()

    def stop(self):
        self.stop_event.set()
        self.terminate_encodes()

    def _register_proc(self, proc):
        with self._procs_lock:
            self._procs.add(proc)

    def _unregister_proc(self, proc):
        with self._procs_lock:
            self._procs.discard(proc)

    def terminate_encodes(self, grace=20):
        with self._procs_lock:
            procs = list(self._procs)
        if not procs:
            return
        log.info("terminating %d running encode(s)", len(procs))
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = time.time() + grace
        for proc in procs:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except Exception:
                try:
                    log.warning("encode pid %s ignored SIGTERM, killing", proc.pid)
                    proc.kill()
                except OSError:
                    pass

    def requeue_retries(self):
        for row in self.store.due_for_retry(RETRY_MAX_ATTEMPTS):
            log.info(
                "retrying %s (attempt %d)", row["source_path"], (row["attempts"] or 0) + 1
            )
            self.store.advance(row["id"], state.DETECTED, "retry due")
            self.queue.put(row["id"])

    def requeue_resumable(self):
        for row in self.store.resumable():
            log.info("resuming %s from stage %s", row["source_path"], row["stage"])
            self.queue.put(row["id"])

    def _watch(self):
        while not self.stop_event.is_set():
            try:
                self.scan()
            except Exception:
                log.exception("import scan failed")
            try:
                self.requeue_retries()
            except Exception:
                log.exception("retry sweep failed")
            self.stop_event.wait(self.cfg.poll_interval)

    def scan(self):
        root = self.layout.imports
        if not os.path.isdir(root):
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in sorted(filenames):
                if not name.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                if name.startswith(".") or name.endswith(".part"):
                    continue
                path = os.path.join(dirpath, name)
                if not self._stable(path):
                    continue
                existing = self.store.by_source(path)
                if existing:
                    continue
                title_id = self.store.upsert_source(path)
                log.info("detected %s", path)
                self.queue.put(title_id)

    def _stable(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return False
        if time.time() - stat.st_mtime < self.cfg.mtime_quiet:
            self._seen_sizes[path] = stat.st_size
            return False
        previous = self._seen_sizes.get(path)
        self._seen_sizes[path] = stat.st_size
        return previous == stat.st_size

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                title_id = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process(title_id)
            except Exception as exc:
                log.exception("title %s failed", title_id)
                self.store.advance(title_id, state.FAILED, str(exc), reason=str(exc))
            finally:
                self.queue.task_done()

    def process(self, title_id):
        row = self.store.get(title_id)
        if row is None:
            return
        source = row["source_path"]
        job_id = row["job_id"] or uuid.uuid4().hex[:12]

        try:
            container = self._probe(title_id, source)
            kind = self._classify(title_id, source, container)
            self._screen(title_id, source, container, kind)
            identity = self._identify(title_id, container, kind)
            self._compare(title_id, container, identity, kind, source)
            workdir, work = self._stage(title_id, source, job_id, container)
            work = self._remux(title_id, work, source)
            container = probemod.probe(work).container
            self._tag(title_id, work, identity, kind)
            self._ready(title_id, work, identity, kind)
            work = self._encode(title_id, work, workdir, container, kind, source)
            self._verify(title_id, work, source)
            self._publish(title_id, work, identity, kind)
            self._retire(title_id, source, job_id)
        except HoldError as exc:
            log.warning("held: %s: %s", source, exc)
            self.store.hold(title_id, str(exc))
        except RetryLater as exc:
            row = self.store.get(title_id) or {}
            attempts = row.get("attempts") or 0
            if attempts + 1 >= RETRY_MAX_ATTEMPTS:
                log.warning("giving up after %d attempts: %s: %s", attempts + 1, source, exc)
                self.store.hold(title_id, "%s (gave up after %d attempts)" % (exc, attempts + 1))
            else:
                delay = RETRY_BASE_DELAY * (2 ** attempts)
                log.warning("transient failure on %s, retrying in %ds: %s", source, delay, exc)
                self.store.hold_for_retry(title_id, str(exc), delay)
        except QuarantineError as exc:
            log.info("quarantined: %s: %s", source, exc)
            self._quarantine(title_id, source, str(exc))

    def _probe(self, title_id, source):
        container = probemod.probe(source).container
        self.store.advance(title_id, state.PROBED, "probed", probe=container)
        return container

    def _classify(self, title_id, source, container):
        from . import episodes
        kind = episodes.classify(source)
        self.store.update(title_id, kind=kind)
        return kind

    def _screen(self, title_id, source, container, kind):
        row = self.store.get(title_id)
        if row.get("overridden"):
            self.store.advance(title_id, state.SCREENED, "standards overridden by operator")
            return
        verdict = standards.screen(container, kind, path=source)
        if not verdict.ok:
            raise HoldError("failed minimum standards: %s" % "; ".join(verdict.problems))
        self.store.advance(title_id, state.SCREENED, "; ".join(verdict.warnings) or "passed")

    def _identify(self, title_id, container, kind):
        if self.provider is None:
            raise HoldError("no provider configured, cannot resolve a provider ID")
        row = self.store.get(title_id)
        try:
            identity = self.provider.identify(row, container, kind)
        except providermod.RateLimited as exc:
            raise RetryLater(str(exc))
        except providermod.ProviderError as exc:
            raise RetryLater("provider lookup failed: %s" % exc)
        if identity is None:
            raise HoldError(
                "provider ID could not be resolved and must never be guessed"
            )
        self.store.advance(
            title_id,
            state.IDENTIFIED,
            "resolved %s" % identity.get("title"),
            title=identity.get("title"),
            year=identity.get("year"),
            show=identity.get("show"),
            season=identity.get("season"),
            episode=identity.get("episode"),
            tmdb=identity.get("tmdb"),
            imdb=identity.get("imdb"),
            tvdb=identity.get("tvdb"),
        )
        return identity

    def _compare(self, title_id, container, identity, kind, source):
        if not self.layout.libraries:
            self.store.advance(
                title_id, state.COMPARED, "no library mounted, comparison skipped"
            )
            return
        incumbent_path = self._find_incumbent(identity, kind)
        if incumbent_path is None:
            self.store.advance(title_id, state.COMPARED, "no incumbent, treated as new")
            return
        try:
            incumbent = probemod.probe(incumbent_path).container
        except probemod.ProbeError as exc:
            self.store.advance(title_id, state.COMPARED, "incumbent unreadable: %s" % exc)
            return
        result = compare.compare(
            compare.attributes(container, source),
            compare.attributes(incumbent, incumbent_path),
        )
        self.store.update(title_id, comparison=result.as_dict())
        if result.is_loss:
            raise QuarantineError("not better than the incumbent: %s" % result.reason)
        if result.verdict == compare.AMBIGUOUS:
            raise HoldError("comparison against the incumbent was inconclusive")
        self.store.advance(title_id, state.COMPARED, result.reason)

    def _find_incumbent(self, identity, kind):
        root = self.layout.libraries.get(kind)
        if not root or not os.path.isdir(root):
            return None
        if kind == "movie":
            wanted = titles.to_filename(identity["title"])
            for name in os.listdir(root):
                if name.startswith("%s (%s)" % (wanted, identity.get("year"))):
                    folder = os.path.join(root, name)
                    for f in sorted(os.listdir(folder)):
                        if f.lower().endswith(".mkv"):
                            return os.path.join(folder, f)
            return None
        show = titles.to_filename(identity.get("show") or "")
        season = identity.get("season")
        episode = identity.get("episode")
        for name in os.listdir(root):
            if not name.startswith(show + " ("):
                continue
            season_dir = os.path.join(root, name, titles.season_folder(season))
            if not os.path.isdir(season_dir):
                return None
            code = titles.episode_code(season, episode)
            for f in sorted(os.listdir(season_dir)):
                if code in f and f.lower().endswith(".mkv"):
                    return os.path.join(season_dir, f)
        return None

    def _stage(self, title_id, source, job_id, container):
        size = container.get("size_bytes") or os.path.getsize(source)
        ok, need = self.layout.has_headroom(size)
        while not ok and not self.stop_event.is_set():
            log.info(
                "waiting for encode space: need %d bytes, have %d",
                need,
                self.layout.encode_free_bytes(),
            )
            self.stop_event.wait(30)
            ok, need = self.layout.has_headroom(size)

        workdir = self.layout.make_job_dir(job_id)
        work = os.path.join(workdir, os.path.basename(source))
        if self.cfg.dry_run:
            log.info("DRY RUN would copy %s -> %s", source, work)
        else:
            shutil.copy2(source, work)
        self.store.advance(
            title_id, state.STAGED, "copied to the encode area", job_id=job_id, work_path=work
        )
        return workdir, work

    def _remux(self, title_id, work, source):
        base, ext = os.path.splitext(work)
        detail = []
        current = work
        if ext.lower() != ".mkv":
            target = base + ".mkv"
            if self.cfg.dry_run:
                log.info("DRY RUN would remux %s -> %s", current, target)
            else:
                info = media.to_matroska(current, target)
                detail.append(info["method"])
                os.remove(current)
            current = target

        stripped = os.path.join(os.path.dirname(current), "stripped.mkv")
        if self.cfg.dry_run:
            log.info("DRY RUN would strip foreign tracks from %s", current)
        else:
            info = media.strip_foreign(current, stripped)
            if info["stripped"]:
                detail.append("stripped %d foreign track(s)" % info["stripped"])
                if current != source:
                    os.remove(current)
                current = stripped
            media.fix_flags_and_language(current)
            detail.append("flags and languages normalised")

        self.store.advance(
            title_id, state.REMUXED, "; ".join(detail) or "no remux needed", work_path=current
        )
        return current

    def _tag(self, title_id, work, identity, kind):
        if self.cfg.dry_run:
            log.info("DRY RUN would write %s tags to %s", kind, work)
            self.store.advance(title_id, state.TAGGED, "dry run")
            return
        existing = tags.read_tags(work)
        carry = tags.carry_forward(existing)
        if kind == "movie":
            xml = tags.build_movie_xml(
                identity["title"], identity["year"], identity["tmdb"], identity["imdb"], carry
            )
            segment = identity["title"]
        else:
            xml = tags.build_tv_xml(
                identity["show"], identity["tvdb"], identity["tmdb"],
                identity["season"], identity["title"], identity["episode"], carry,
            )
            segment = identity["title"]
        ratio = tags.write_tags(work, xml, segment_title=segment)
        self.store.advance(title_id, state.TAGGED, "statistics byte-sum ratio %.4f" % ratio)

    def _ready(self, title_id, work, identity, kind):
        if self.cfg.dry_run:
            self.store.advance(title_id, state.READY, "dry run")
            return
        ok, problems = tags.readiness(
            work, kind, identity["title"], show=identity.get("show")
        )
        if not ok:
            raise HoldError("failed readiness checks: %s" % "; ".join(problems))
        self.store.advance(title_id, state.READY, "readiness checks passed")

    def _encode(self, title_id, work, workdir, container, kind, source):
        row = self.store.get(title_id)
        override = read_sidecar(os.path.dirname(source))
        video = container["video"]

        grain = None
        decision = encode.select(
            video, kind, self.cfg, grain=None,
            gpu_available=self.gpu.available, override=override,
        )
        if not decision.is_passthrough and "film" not in override:
            result = media.grain_probe(
                work, video, workdir, threshold=self.cfg.grain_threshold
            ) if not self.cfg.dry_run else {
                "grain": False, "ratio": None, "reason": "dry run"
            }
            grain = result["grain"]
            self.store.update(title_id, grain_ratio=result.get("ratio"))
            decision = encode.select(
                video, kind, self.cfg, grain=grain,
                gpu_available=self.gpu.available, override=override,
            )
            log.info("grain probe %s: %s", os.path.basename(work), result["reason"])

        self.store.update(title_id, decision=decision.as_dict(), encoder=decision.encoder)

        if decision.is_passthrough:
            self.store.advance(
                title_id, state.ENCODED, "passthrough: %s" % decision.reason
            )
            return work

        crop = None
        if override.get("crop"):
            crop = override["crop"]
        elif not self.cfg.dry_run:
            detected = media.detect_crop(work, video)
            if detected:
                crop = detected["filter"]
                log.info("cropping %s: %d px of bars", os.path.basename(work), detected["bars_px"])

        target = os.path.join(workdir, "encoded.mkv")
        cmd = encode.build_command(
            decision, work, target, video, self.cfg, crop=crop, crf=override.get("crf")
        )

        if self.cfg.dry_run:
            log.info("DRY RUN would encode with: %s", " ".join(cmd))
            self.store.advance(title_id, state.ENCODED, "dry run")
            return work

        sem = self.slots.acquire(decision.device)
        started = time.time()
        try:
            log.info(
                "encoding %s with %s on %s",
                os.path.basename(work), decision.encoder, decision.device,
            )
            proc = media.run_cancellable(
                cmd, register=self._register_proc, unregister=self._unregister_proc
            )
            if self.stop_event.is_set():
                raise RetryLater("shutting down, encode cancelled")
            if proc.returncode != 0:
                raise RuntimeError(
                    "%s failed: %s" % (decision.encoder, (proc.stderr or "").strip()[-400:])
                )
        finally:
            self.slots.release(decision.device, sem)

        elapsed = int(time.time() - started)
        tags.refresh_statistics(target)
        media.fix_flags_and_language(target)
        os.remove(work)
        self.store.advance(
            title_id,
            state.ENCODED,
            "%s on %s in %d min" % (decision.encoder, decision.device, elapsed // 60),
            work_path=target,
        )
        return target

    def _verify(self, title_id, work, source):
        if self.cfg.dry_run:
            self.store.advance(title_id, state.VERIFIED, "dry run")
            return
        notes = []
        src_duration = probemod.video_duration(source)
        out_duration = probemod.video_duration(work)
        if src_duration and out_duration and abs(src_duration - out_duration) > 2.0:
            raise HoldError(
                "video stream duration moved by %.1fs, output may be truncated"
                % (out_duration - src_duration)
            )
        notes.append("duration %.1f min" % ((out_duration or 0) / 60))
        ratio = tags.byte_sum_ratio(work)
        if ratio <= tags.STATS_RATIO_FLOOR:
            raise HoldError("track statistics missing after encode, byte-sum ratio %.4f" % ratio)
        notes.append("statistics ratio %.4f" % ratio)
        self.store.advance(title_id, state.VERIFIED, "; ".join(notes))

    def _publish(self, title_id, work, identity, kind):
        if kind == "movie":
            folder = titles.movie_folder(
                identity["title"], identity["year"], identity["tmdb"], identity["imdb"]
            )
            filename = titles.movie_filename(identity["title"], identity["year"])
            outdir = os.path.join(self.layout.completed, folder)
        else:
            folder = titles.show_folder(
                identity["show"], identity["show_year"], identity["tvdb"], identity["tmdb"]
            )
            filename = titles.episode_filename(
                identity["show"], identity["season"], identity["episode"], identity["title"],
                last=identity.get("episode_last"),
            )
            outdir = os.path.join(
                self.layout.completed, folder, titles.season_folder(identity["season"])
            )

        destination = os.path.join(outdir, filename)

        if self.cfg.dry_run:
            if os.path.exists(destination):
                raise HoldError("destination already exists: %s" % destination)
            log.info("DRY RUN would publish -> %s", destination)
            self.store.advance(title_id, state.PUBLISHED, "dry run", output_path=destination)
            return

        try:
            self.layout.publish_file(work, destination)
        except FileExistsError as exc:
            raise HoldError(str(exc))
        self.store.advance(
            title_id, state.PUBLISHED, "published to completed", output_path=destination
        )

    def _retire(self, title_id, source, job_id):
        if self.cfg.dry_run:
            self.store.advance(title_id, state.RETIRED, "dry run")
            return
        destination = self.layout.quarantine_path(source)
        os.replace(source, destination)
        self.layout.wipe_job_dir(job_id)
        self.store.advance(title_id, state.RETIRED, "source retired to quarantine")

    def _quarantine(self, title_id, source, reason):
        if self.cfg.dry_run:
            self.store.advance(title_id, state.QUARANTINED, reason)
            return
        destination = self.layout.quarantine_path(source)
        os.replace(source, destination)
        self.store.advance(title_id, state.QUARANTINED, reason, reason=reason)

    def status(self):
        return {
            "started_at": self.started_at,
            "uptime_s": int(time.time() - self.started_at),
            "queue_depth": self.queue.qsize(),
            "slots": self.slots.snapshot(),
            "stages": self.store.counts_by_stage(),
            "gpu": self.gpu.as_dict(),
            "encode": {
                "path": self.layout.encode,
                "separate_filesystem": self.layout.encode_is_separate(),
                "free_bytes": self.layout.encode_free_bytes(),
            },
            "libraries_mounted": bool(self.layout.libraries),
            "config": self.cfg.as_dict(),
        }
