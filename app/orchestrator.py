import logging
import os
import queue
import shutil
import threading
import time
import uuid

from . import audit, compare, encode, media, probe as probemod, provider as providermod
from . import standards, state, tags, titles

log = logging.getLogger("orchestrator")

VIDEO_EXTENSIONS = probemod.VIDEO_EXTENSIONS
JOB_SIDECAR = "encode.job"


#----- Per-title overrides
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


#----- Encoder pools
PASSTHROUGH_WORKERS = 1
POOLS = (encode.CPU, encode.GPU, encode.PASSTHROUGH)


class Pools:
    def __init__(self, cfg):
        self.sizes = {
            encode.CPU: max(cfg.cpu_slots, 0),
            encode.GPU: max(cfg.gpu_slots, 0),
            encode.PASSTHROUGH: PASSTHROUGH_WORKERS,
        }
        self.queues = {name: queue.Queue() for name in POOLS}
        self.active = {name: 0 for name in POOLS}
        self._lock = threading.Lock()

    def enter(self, name):
        with self._lock:
            self.active[name] += 1

    def leave(self, name):
        with self._lock:
            self.active[name] -= 1

    def snapshot(self):
        with self._lock:
            return {
                "gpu_active": self.active[encode.GPU],
                "cpu_active": self.active[encode.CPU],
                "passthrough_active": self.active[encode.PASSTHROUGH],
            }

    def depths(self):
        return {name: q.qsize() for name, q in self.queues.items()}


#----- Stage outcomes
class HoldError(RuntimeError):
    def __init__(self, reasons, stage=None):
        self.reasons = state.normalise_reasons(
            [{"stage": stage, "text": reasons}] if isinstance(reasons, str) else reasons
        )
        super().__init__("; ".join(e["text"] for e in self.reasons))


def _reason(stage, text):
    return {"stage": stage, "text": text}


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
        self.pools = Pools(cfg)
        self.queue = queue.Queue()
        self.workers = []
        self._grain_lock = threading.Semaphore(1)
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self._seen_sizes = {}
        self._procs = set()
        self._procs_lock = threading.Lock()
        self._progress = {}
        self.auditor = audit.Auditor(cfg, layout, store, self.stop_event)

    #----- Lifecycle
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
            t = threading.Thread(target=self._assess_worker, name="assess-%d" % n, daemon=True)
            t.start()
            self.workers.append(t)
        for name in POOLS:
            for n in range(self.pools.sizes[name]):
                t = threading.Thread(
                    target=self._work_worker, args=(name,), name="%s-%d" % (name, n), daemon=True
                )
                t.start()
                self.workers.append(t)
        t = threading.Thread(target=self._watch, name="watcher", daemon=True)
        t.start()
        self.workers.append(t)
        self.requeue_resumable()
        self.auditor.start()

    def stop(self):
        self.stop_event.set()
        self.terminate_encodes()

    #----- Encode progress
    def _progress_handler(self, title_id, duration):
        def handle(fields):
            micros = fields.get("out_time_us") or fields.get("out_time_ms")
            try:
                seconds = int(micros) / 1000000.0
            except (TypeError, ValueError):
                return
            speed = 0.0
            try:
                speed = float((fields.get("speed") or "0").rstrip("xX ").strip())
            except ValueError:
                pass
            row = {
                "seconds": round(seconds, 1),
                "duration": round(duration, 1) if duration else None,
                "fps": fields.get("fps"),
                "speed": speed or None,
            }
            if duration:
                row["percent"] = round(min(100.0, seconds / duration * 100.0), 1)
                if speed > 0:
                    row["eta_s"] = int(max(0.0, duration - seconds) / speed)
            self._progress[title_id] = row
        return handle

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

    #----- Requeueing
    def requeue_retries(self):
        for row in self.store.due_for_retry(RETRY_MAX_ATTEMPTS):
            log.info(
                "title %s retrying %s (attempt %d)",
                row["id"], row["source_path"], (row["attempts"] or 0) + 1,
            )
            self.store.advance(row["id"], state.DETECTED, "retry due")
            self.queue.put(row["id"])

    def requeue_resumable(self):
        for row in self.store.resumable():
            log.info(
                "title %s resuming %s from stage %s",
                row["id"], row["source_path"], row["stage"],
            )
            self._enqueue(row)

    def _pool_for(self, decision):
        if not decision or not decision.get("action"):
            return None
        if decision.get("action") == encode.PASSTHROUGH:
            return encode.PASSTHROUGH
        return decision.get("device") or encode.CPU

    def _enqueue(self, row):
        pool = None if row["stage"] in state.ASSESSMENT else self._pool_for(row.get("decision"))
        if pool is None:
            if row["stage"] not in state.ASSESSMENT:
                log.info("title %s has no routing decision, re-assessing", row["id"])
                self.store.advance(row["id"], state.DETECTED, "re-assessed, no routing decision stored")
            self.queue.put(row["id"])
            return
        self.pools.queues[pool].put(row["id"])

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

    #----- Watching the import directory
    def scan(self):
        root = self.layout.imports
        if not os.path.isdir(root):
            return
        for gone in [p for p in self._seen_sizes if not os.path.exists(p)]:
            del self._seen_sizes[gone]
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
                    if existing["stage"] in state.TERMINAL:
                        log.info(
                            "title %s %s reappeared in import after %s, treating it as a new import",
                            existing["id"], name, existing["stage"],
                        )
                        self.store.reset_for_reimport(existing["id"])
                        self.store.link_import(existing["id"], path)
                        self.queue.put(existing["id"])
                    else:
                        log.debug(
                            "skipping %s, title %s already claims this path at stage %s",
                            name, existing["id"], existing["stage"],
                        )
                    continue
                title_id = self.store.upsert_source(path)
                log.info("title %s detected %s", title_id, path)
                self.store.link_import(title_id, path)
                self.queue.put(title_id)

    def _stable(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return False
        if time.time() - stat.st_mtime < self.cfg.mtime_quiet:
            self._seen_sizes[path] = stat.st_size
            return False
        #----- stability means matching the size recorded by the previous poll, not merely being quiet.
        previous = self._seen_sizes.get(path)
        self._seen_sizes[path] = stat.st_size
        return previous == stat.st_size

    #----- Workers, one half each
    def _assess_worker(self):
        while not self.stop_event.is_set():
            try:
                title_id = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._run(title_id, self._assess)
            finally:
                self.queue.task_done()

    def _work_worker(self, pool):
        q = self.pools.queues[pool]
        while not self.stop_event.is_set():
            try:
                title_id = q.get(timeout=1)
            except queue.Empty:
                continue
            self.pools.enter(pool)
            try:
                self._run(title_id, self._work)
            finally:
                self.pools.leave(pool)
                q.task_done()

    def _run(self, title_id, half):
        try:
            half(title_id)
        except Exception as exc:
            log.exception("title %s failed", title_id)
            self.store.advance(title_id, state.FAILED, str(exc), reason=str(exc))

    #----- The chain
    def _assess(self, title_id):
        row = self.store.get(title_id)
        if row is None:
            return
        source = row["source_path"]
        try:
            container = self._probe(title_id, source)
            kind = self._classify(title_id, source, container)
            reasons = self._screen(title_id, source, container, kind)
            identity, more = self._identify(title_id, container, kind, source)
            reasons += more
            reasons += self._compare(title_id, container, identity, kind, source)
            if reasons:
                raise HoldError(reasons)
            pool = self._route(title_id, container, kind, source)
            self.pools.queues[pool].put(title_id)
        except HoldError as exc:
            log.warning("held: %s: %s", source, exc)
            self.store.hold(title_id, exc.reasons)
        except RetryLater as exc:
            self._retry_later(title_id, source, exc)
        except QuarantineError as exc:
            log.debug("quarantine raised on %s: %s", source, exc)
            self._quarantine(title_id, source, str(exc))

    def _work(self, title_id):
        row = self.store.get(title_id)
        if row is None:
            return
        source = row["source_path"]
        job_id = row["job_id"] or uuid.uuid4().hex[:12]
        kind = row["kind"]
        identity = row.get("identity") or {}
        try:
            container = row.get("probe") or probemod.probe(source).container
            workdir, work = self._stage(title_id, source, job_id, container)
            work = self._remux(title_id, work, source)
            if not self.cfg.dry_run:
                container = probemod.probe(work).container
            carry = self._tag(title_id, work, identity, kind)
            self._ready(title_id, work, identity, kind)
            work = self._encode(title_id, work, workdir, container, kind, source, identity, carry)
            self._verify(title_id, work, source, identity, kind)
            self._publish(title_id, work, identity, kind)
            try:
                self._retire(title_id, source, job_id)
            except Exception as exc:
                log.exception("cleanup failed for %s after a successful publish", source)
                self.store.update(title_id, reason="cleanup failed: %s" % exc)
                self.store.record(title_id, state.PUBLISHED, "cleanup failed: %s" % exc)
        except HoldError as exc:
            log.warning("held: %s: %s", source, exc)
            self.store.hold(title_id, exc.reasons)
        except RetryLater as exc:
            self._retry_later(title_id, source, exc)
        except QuarantineError as exc:
            log.debug("quarantine raised on %s: %s", source, exc)
            self._quarantine(title_id, source, str(exc))

    def _retry_later(self, title_id, source, exc):
        row = self.store.get(title_id) or {}
        attempts = row.get("attempts") or 0
        if attempts + 1 >= RETRY_MAX_ATTEMPTS:
            log.warning("giving up after %d attempts: %s: %s", attempts + 1, source, exc)
            self.store.hold(title_id, "%s (gave up after %d attempts)" % (exc, attempts + 1))
        else:
            delay = RETRY_BASE_DELAY * (2 ** attempts)
            log.warning("transient failure on %s, retrying in %ds: %s", source, delay, exc)
            self.store.hold_for_retry(title_id, str(exc), delay)


    #----- Stages, in chain order
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
            return []
        verdict = standards.screen(container, kind, path=source)
        if not verdict.ok:
            self.store.record(
                title_id, state.SCREENED, "failed minimum standards: %s" % "; ".join(verdict.problems)
            )
            return [_reason(state.SCREENED, p) for p in verdict.problems]
        self.store.advance(title_id, state.SCREENED, "; ".join(verdict.warnings) or "passed")
        return []

    def _identify(self, title_id, container, kind, source):
        row = self.store.get(title_id)
        identity = None
        if self.provider is None:
            problem = "no provider configured, cannot resolve a provider ID"
        else:
            try:
                identity = self.provider.identify(row, container, kind)
            except providermod.RateLimited as exc:
                raise RetryLater(str(exc))
            except providermod.ProviderError as exc:
                raise RetryLater("provider lookup failed: %s" % exc)
            problem = "provider ID could not be resolved and must never be guessed"
        if identity is None:
            if row.get("overridden"):
                identity = self._unidentified(kind, source)
                self.store.advance(
                    title_id,
                    state.IDENTIFIED,
                    "unidentified, forced through under its source name %r" % identity["title"],
                    title=identity["title"],
                    identity=identity,
                )
                return identity, []
            self.store.record(title_id, state.IDENTIFIED, problem)
            return None, [_reason(state.IDENTIFIED, problem)]
        self.store.advance(
            title_id,
            state.IDENTIFIED,
            "resolved %s from %s"
            % (identity.get("title"), identity.get("identified_from") or "provider search"),
            title=identity.get("title"),
            year=identity.get("year"),
            show=identity.get("show"),
            season=identity.get("season"),
            episode=identity.get("episode"),
            tmdb=identity.get("tmdb"),
            imdb=identity.get("imdb"),
            tvdb=identity.get("tvdb"),
            poster_url=self._poster_url(kind, identity),
            identity=identity,
        )
        return identity, []

    @staticmethod
    def _unidentified(kind, source):
        stem = os.path.splitext(os.path.basename(str(source)))[0]
        return {
            "unidentified": True,
            "title": titles.to_filename(stem),
            "year": None,
            "show": None,
            "season": None,
            "episode": None,
            "tmdb": None,
            "imdb": None,
            "tvdb": None,
        }

    def _poster_url(self, kind, identity):
        try:
            url = self.provider.tmdb_poster(kind, identity.get("tmdb"))
            if url:
                return url
            if kind == "tv":
                return self.provider.tvdb_poster(identity.get("tvdb"))
        except Exception as exc:
            log.debug("poster url lookup failed: %s", exc)
        return None

    #----- Comparison and the library lookup
    def _compare(self, title_id, container, identity, kind, source):
        row = self.store.get(title_id)
        if row.get("overridden"):
            log.info("title %s comparison overridden by operator", title_id)
            self.store.advance(
                title_id, state.COMPARED, "comparison overridden by operator"
            )
            return []
        if identity is None:
            self.store.record(title_id, state.COMPARED, "skipped, no identity resolved")
            return []
        if not self.layout.libraries:
            self.store.advance(
                title_id, state.COMPARED, "no library mounted, comparison skipped"
            )
            return []
        incumbent_path, route = self._find_incumbent(identity, kind, skip=row.get("origin_path"))
        if incumbent_path is None:
            log.info("title %s has no incumbent, treated as new: %s", title_id, route)
            self.store.advance(
                title_id, state.COMPARED, "no incumbent, treated as new: %s" % route
            )
            return []
        log.info(
            "title %s comparing against incumbent %s, %s",
            title_id, os.path.basename(incumbent_path), route,
        )
        try:
            incumbent = probemod.probe(incumbent_path).container
        except probemod.ProbeError as exc:
            self.store.advance(title_id, state.COMPARED, "incumbent unreadable: %s" % exc)
            return []
        crops = self._crop_both(title_id, container, incumbent, source, incumbent_path)
        result = compare.compare(
            compare.measure(source, crop=crops[0]),
            compare.measure(incumbent_path, crop=crops[1]),
        )
        self.store.update(title_id, comparison=result.as_dict())
        if result.is_loss:
            raise QuarantineError("not better than the incumbent: %s" % result.reason)
        if result.verdict == compare.AMBIGUOUS:
            self.store.record(title_id, state.COMPARED, "inconclusive: %s" % result.reason)
            return [_reason(state.COMPARED, "comparison against the incumbent was inconclusive: %s" % result.reason)]
        self.store.advance(
            title_id, state.COMPARED, "%s (incumbent %s)" % (result.reason, route)
        )
        return []

    def _crop_both(self, title_id, incoming, incumbent, incoming_path, incumbent_path):
        new_video = incoming.get("video") or {}
        old_video = incumbent.get("video") or {}
        if not (
            standards.is_letterbox_candidate(new_video)
            or standards.is_letterbox_candidate(old_video)
        ):
            log.debug("neither side is a letterbox candidate, gate 3 cropdetect not run")
            return None, None
        log.info("title %s running cropdetect on both sides for the letterbox gate", title_id)
        pairs = []
        for path, video, container in (
            (incoming_path, new_video, incoming),
            (incumbent_path, old_video, incumbent),
        ):
            try:
                found = media.detect_crop(path, video, container=container)
                if found is None:
                    found = {
                        "bars_px": 0,
                        "picture_pixels": int(video.get("display_pixels") or 0) or None,
                    }
                pairs.append(found)
            except Exception as exc:
                log.warning("cropdetect failed on %s: %s", os.path.basename(str(path)), exc)
                pairs.append(None)
        return pairs[0], pairs[1]

    def _find_incumbent(self, identity, kind, skip=None):
        root = self.layout.libraries.get(kind)
        if not root or not os.path.isdir(root):
            return None, "no %s library mounted" % kind
        if kind == "movie":
            found, route = self._find_movie_incumbent(root, identity)
        else:
            found, route = self._find_tv_incumbent(root, identity)
        if found and skip and os.path.realpath(found) == os.path.realpath(skip):
            log.info("incumbent is the file this title was imported from, comparison skipped")
            return None, "the incumbent %s is this title's own origin, imported for repair" % route
        return found, route

    @staticmethod
    def _folder_ids(name):
        found = {}
        for key, pattern in (
            ("tmdb", titles.TMDBID_IN_NAME),
            ("imdb", titles.IMDBID_IN_NAME),
            ("tvdb", titles.TVDBID_IN_NAME),
        ):
            match = pattern.search(name)
            if match:
                found[key] = match.group(1).lower()
        return found

    @classmethod
    def _id_match(cls, identity, name, keys):
        folder_ids = cls._folder_ids(name)
        for key in keys:
            want = identity.get(key)
            have = folder_ids.get(key)
            if want and have and str(want).lower() == have:
                return "%sid-%s" % (key, want)
        return None

    @staticmethod
    def _first_mkv(folder):
        for entry in sorted(os.listdir(folder)):
            if entry.lower().endswith(".mkv"):
                return os.path.join(folder, entry)
        return None

    def _find_movie_incumbent(self, root, identity):
        prefix = "%s (%s)" % (titles.to_filename(identity["title"]), identity.get("year"))
        by_name = None
        scanned = 0
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            scanned += 1
            matched = self._id_match(identity, name, ("tmdb", "imdb"))
            if matched:
                found = self._first_mkv(folder)
                if found:
                    return found, "matched on %s" % matched
                return None, "folder matched on %s but holds no mkv" % matched
            if by_name is None and name.startswith(prefix):
                by_name = folder
        if by_name is not None:
            found = self._first_mkv(by_name)
            if found:
                return found, "matched on folder name, no provider id match"
        return None, "scanned %d library folder(s), none matched" % scanned

    def _find_tv_incumbent(self, root, identity):
        show = titles.to_filename(identity.get("show") or "")
        season = identity.get("season")
        code = titles.episode_code(season, identity.get("episode"))
        by_name = None
        scanned = 0
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            scanned += 1
            matched = self._id_match(identity, name, ("tvdb", "tmdb"))
            if matched:
                found = self._episode_file(folder, season, code)
                if found:
                    return found, "matched on %s" % matched
                return None, "show matched on %s, %s not in the library" % (matched, code)
            if by_name is None and name.startswith(show + " ("):
                by_name = folder
        if by_name is not None:
            found = self._episode_file(by_name, season, code)
            if found:
                return found, "matched on folder name, no provider id match"
        return None, "scanned %d library folder(s), none matched" % scanned

    @staticmethod
    def _episode_file(folder, season, code):
        season_dir = os.path.join(folder, titles.season_folder(season))
        if not os.path.isdir(season_dir):
            return None
        for entry in sorted(os.listdir(season_dir)):
            if code in entry and entry.lower().endswith(".mkv"):
                return os.path.join(season_dir, entry)
        return None

    #----- Routing, the last assessment step
    def _route(self, title_id, container, kind, source):
        override = read_sidecar(os.path.dirname(source))
        video = container["video"]
        decision = encode.select(
            video, kind, self.cfg, grain=None,
            gpu_available=self.gpu.available, override=override,
        )
        if not decision.is_passthrough and "film" not in override:
            result = self._grain(title_id, source, video, container)
            self.store.update(title_id, grain_ratio=result.get("ratio"))
            decision = encode.select(
                video, kind, self.cfg, grain=result["grain"],
                gpu_available=self.gpu.available, override=override,
            )
            log.info(
                "title %s grain probe %s: %s",
                title_id, os.path.basename(source), result["reason"],
            )
        log.info("title %s router %s", title_id, encode.describe(decision))
        for note in decision.notes:
            log.info("title %s router note: %s", title_id, note)
        pool = self._pool_for(decision.as_dict())
        self.store.advance(
            title_id, state.ROUTED, encode.describe(decision),
            decision=decision.as_dict(), encoder=decision.encoder,
        )
        return pool

    def _grain(self, title_id, source, video, container):
        if self.cfg.dry_run:
            return {"grain": False, "ratio": None, "reason": "dry run"}
        scratch = os.path.join(self.layout.encode, ".probe", str(title_id))
        with self._grain_lock:
            self.layout.guarded_makedirs(scratch)
            try:
                return media.grain_probe(
                    source, video, scratch,
                    threshold=self.cfg.grain_threshold, container=container,
                )
            finally:
                shutil.rmtree(scratch, ignore_errors=True)

    #----- Staging, remux and tagging
    def _stage(self, title_id, source, job_id, container):
        size = container.get("size_bytes") or os.path.getsize(source)
        ok, need = self.layout.has_headroom(size)
        while not ok and not self.stop_event.is_set():
            log.info(
                "title %s waiting for encode space: need %d bytes, have %d",
                title_id,
                need,
                self.layout.encode_free_bytes(),
            )
            self.stop_event.wait(30)
            ok, need = self.layout.has_headroom(size)

        workdir = self.layout.make_job_dir(job_id)
        work = os.path.join(workdir, os.path.basename(source))
        if self.cfg.dry_run:
            log.info("title %s DRY RUN would copy %s -> %s", title_id, source, work)
        else:
            copy_started = time.time()
            shutil.copy2(source, work)
            copied = time.time() - copy_started
            log.info(
                "title %s staged %s, %.1f GB in %.0fs (%.0f MB/s)",
                title_id, os.path.basename(source), size / 1e9, copied,
                (size / 1e6 / copied) if copied > 0 else 0,
            )
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
                log.info("title %s DRY RUN would remux %s -> %s", title_id, current, target)
            else:
                info = media.to_matroska(current, target)
                detail.append(info["method"])
                os.remove(current)
            current = target

        stripped = os.path.join(os.path.dirname(current), "stripped.mkv")
        if self.cfg.dry_run:
            log.info("title %s DRY RUN would strip foreign tracks from %s", title_id, current)
        else:
            info = media.strip_foreign(current, stripped)
            if info["stripped"]:
                detail.append("stripped %d foreign track(s)" % info["stripped"])
                if current != source:
                    os.remove(current)
                current = stripped
            media.fix_flags_and_language(current)
            detail.append("flags and languages normalised")
            video = probemod.probe(current).video
            if video.get("hdr"):
                repair = media.repair_hdr_declaration(current, video)
                if repair["repaired"]:
                    detail.append(
                        "hdr declaration repaired from the bitstream: %s"
                        % ", ".join(repair["repaired"])
                    )
                if repair["unrepairable"]:
                    detail.append(
                        "hdr declaration cannot be repaired by a header edit: %s"
                        % ", ".join(repair["unrepairable"])
                    )

        self.store.advance(
            title_id, state.REMUXED, "; ".join(detail) or "no remux needed", work_path=current
        )
        return current

    def _tag_xml(self, identity, kind, carry):
        if identity.get("unidentified"):
            return tags.build_unidentified_xml(kind, identity["title"], carry)
        if kind == "movie":
            return tags.build_movie_xml(
                identity["title"], identity["year"], identity["tmdb"], identity["imdb"], carry
            )
        return tags.build_tv_xml(
            identity["show"], identity["tvdb"], identity["tmdb"],
            identity["season"], identity["title"], identity["episode"], carry,
        )

    def _apply_tags(self, path, identity, kind, carry):
        xml = self._tag_xml(identity, kind, carry)
        return tags.write_tags(path, xml, segment_title=identity["title"])

    def _tag(self, title_id, work, identity, kind):
        if self.cfg.dry_run:
            log.info("title %s DRY RUN would write %s tags to %s", title_id, kind, work)
            self.store.advance(title_id, state.TAGGED, "dry run")
            return {}
        carry = tags.carry_forward(tags.read_tags(work))
        ratio = self._apply_tags(work, identity, kind, carry)
        self.store.advance(title_id, state.TAGGED, "statistics byte-sum ratio %.4f" % ratio)
        return carry

    def _ready(self, title_id, work, identity, kind):
        if self.cfg.dry_run:
            self.store.advance(title_id, state.READY, "dry run")
            return
        ok, problems = tags.readiness(
            work, kind, identity["title"], show=identity.get("show"),
            unidentified=bool(identity.get("unidentified")),
        )
        if not ok:
            if self._forced(title_id, state.READY, problems):
                return
            raise HoldError([_reason(state.READY, p) for p in problems])
        self.store.advance(title_id, state.READY, "readiness checks passed")

    def _forced(self, title_id, stage, problems):
        row = self.store.get(title_id) or {}
        if not row.get("overridden"):
            return False
        detail = "forced past %s by operator: %s" % (stage, "; ".join(problems))
        log.warning("title %s %s", title_id, detail)
        self.store.advance(title_id, stage, detail)
        return True

    #----- Encoding
    def _encode(self, title_id, work, workdir, container, kind, source, identity, carry):
        row = self.store.get(title_id)
        override = read_sidecar(os.path.dirname(source))
        video = container["video"]
        stored = row.get("decision") or {}
        decision = encode.Decision(
            stored.get("action") or encode.PASSTHROUGH,
            stored.get("gate") or 0,
            stored.get("reason") or "no routing decision stored",
            encoder=stored.get("encoder"),
            grain=stored.get("grain"),
            notes=stored.get("notes"),
        )

        if decision.is_passthrough:
            self.store.advance(
                title_id, state.ENCODED, "passthrough: %s" % decision.reason
            )
            return work

        crop = None
        if override.get("crop"):
            crop = override["crop"]
        elif not self.cfg.dry_run:
            detected = media.detect_crop(work, video, container=container)
            if detected:
                crop = detected["filter"]
                log.info(
                    "title %s cropping %s: %d px of bars",
                    title_id, os.path.basename(work), detected["bars_px"],
                )

        target = os.path.join(workdir, "encoded.mkv")
        try:
            cmd = encode.build_command(
                decision, work, target, video, self.cfg, crop=crop, crf=override.get("crf")
            )
        except ValueError as exc:
            raise HoldError(str(exc), stage=state.ENCODING)

        if self.cfg.dry_run:
            log.info("title %s DRY RUN would encode with: %s", title_id, " ".join(cmd))
            self.store.advance(title_id, state.ENCODED, "dry run")
            return work

        started = time.time()
        duration = probemod.usable_duration(video, container)
        self.store.advance(
            title_id, state.ENCODING,
            "%s on %s" % (decision.encoder, decision.device),
        )
        try:
            log.info(
                "title %s encoding %s with %s on %s",
                title_id, os.path.basename(work), decision.encoder, decision.device,
            )
            proc = media.run_cancellable(
                cmd,
                register=self._register_proc,
                unregister=self._unregister_proc,
                on_progress=self._progress_handler(title_id, duration),
            )
            if self.stop_event.is_set():
                raise RetryLater("shutting down, encode cancelled")
            if proc.returncode != 0:
                raise RuntimeError(
                    "%s failed: %s" % (decision.encoder, (proc.stderr or "").strip()[-400:])
                )
        finally:
            self._progress.pop(title_id, None)

        elapsed = int(time.time() - started)
        tags.refresh_statistics(target)
        media.fix_flags_and_language(target)
        self._apply_tags(target, identity, kind, carry)
        tags.refresh_statistics(target)
        os.remove(work)
        log.info(
            "title %s encoded with %s on %s in %d min",
            title_id, decision.encoder, decision.device, elapsed // 60,
        )
        self.store.advance(
            title_id,
            state.ENCODED,
            "%s on %s in %d min" % (decision.encoder, decision.device, elapsed // 60),
            work_path=target,
        )
        return target

    #----- Verification and publication
    def _verify(self, title_id, work, source, identity, kind):
        if self.cfg.dry_run:
            self.store.advance(title_id, state.VERIFIED, "dry run")
            return
        notes = []
        problems = []
        src_duration = probemod.video_duration(source)
        out_duration = probemod.video_duration(work)
        if src_duration and out_duration and abs(src_duration - out_duration) > 2.0:
            problems.append(
                "video stream duration moved by %.1fs, output may be truncated"
                % (out_duration - src_duration)
            )
        notes.append("duration %.1f min" % ((out_duration or 0) / 60))
        packets_in = media.packet_count(source)
        packets_out = media.packet_count(work)
        if packets_in and packets_out and packets_in != packets_out:
            problems.append(
                "video packet count changed, %d in and %d out, streams may be incomplete"
                % (packets_in, packets_out)
            )
        if packets_in and packets_out:
            notes.append("%d video packets preserved" % packets_out)
        else:
            notes.append("packet count unavailable, stream fidelity not checked")
        ratio = tags.byte_sum_ratio(work)
        if ratio <= tags.STATS_RATIO_FLOOR:
            problems.append("track statistics missing after encode, byte-sum ratio %.4f" % ratio)
        notes.append("statistics ratio %.4f" % ratio)
        baseline = ((self.store.get(title_id) or {}).get("probe") or {}).get("video")
        ok, readiness_problems = tags.readiness(
            work, kind, identity["title"], show=identity.get("show"), hdr_baseline=baseline,
            unidentified=bool(identity.get("unidentified")),
        )
        if not ok:
            problems += ["published file failed readiness: %s" % p for p in readiness_problems]
        else:
            notes.append("tag structure verified")
            if (baseline or {}).get("hdr"):
                notes.append("hdr declaration intact")
        self.store.update(title_id, output_probe=compare.measure(work))
        if problems:
            if self._forced(title_id, state.VERIFIED, problems):
                return
            raise HoldError([_reason(state.VERIFIED, p) for p in problems])
        log.info("title %s verified: %s", title_id, "; ".join(notes))
        self.store.advance(title_id, state.VERIFIED, "; ".join(notes))

    def _publish(self, title_id, work, identity, kind):
        if identity.get("unidentified"):
            outdir = self.layout.completed
            filename = titles.assert_component("%s.mkv" % identity["title"])
        elif kind == "movie":
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
        forced = bool((self.store.get(title_id) or {}).get("overridden"))

        if self.cfg.dry_run:
            if os.path.exists(destination) and not forced:
                raise HoldError("destination already exists: %s" % destination, stage=state.PUBLISHED)
            log.info("title %s DRY RUN would publish -> %s", title_id, destination)
            self.store.advance(title_id, state.PUBLISHED, "dry run", output_path=destination)
            return

        detail = "published to completed"
        try:
            self.layout.publish_file(work, destination)
        except FileExistsError as exc:
            if not forced:
                raise HoldError(str(exc), stage=state.PUBLISHED)
            destination = self.layout.unique_path(outdir, filename)
            self.layout.move_file(work, destination)
            detail = "destination existed, forced through beside it as %s" % os.path.basename(destination)
            log.warning("title %s %s", title_id, detail)
        self.store.advance(title_id, state.PUBLISHED, detail, output_path=destination)

    #----- Housekeeping after publication
    def _retire(self, title_id, source, job_id):
        if self.cfg.dry_run:
            self.store.advance(title_id, state.CLEANUP, "dry run")
            return
        destination = self.layout.quarantine_path(source)
        self.layout.move_file(source, destination)
        log.info("title %s retired %s to quarantine", title_id, os.path.basename(source))
        self.layout.wipe_job_dir(job_id)
        self.store.advance(
            title_id, state.CLEANUP, "source retired, work area wiped",
            quarantine_path=destination,
        )

    def _quarantine(self, title_id, source, reason):
        if not os.path.exists(source):
            log.info(
                "title %s source %s no longer exists, removing the title rather than quarantining",
                title_id, source,
            )
            self.store.forget(title_id)
            return "forgotten"
        if self.cfg.dry_run:
            self.store.advance(title_id, state.QUARANTINED, reason)
            return "quarantined"
        destination = self.layout.quarantine_path(source)
        self.layout.move_file(source, destination)
        self.store.advance(
            title_id, state.QUARANTINED, reason, reason=reason, quarantine_path=destination
        )
        log.info("title %s quarantined %s: %s", title_id, os.path.basename(source), reason)
        return "quarantined"

    #----- Library repair
    def import_finding(self, finding_id):
        finding = self.store.finding(finding_id)
        if finding is None:
            raise ValueError("no such finding")
        if finding.get("imported_title_id"):
            row = self.store.get(finding["imported_title_id"])
            if row and row["stage"] not in state.TERMINAL + state.STOPPED:
                raise ValueError(
                    "already in the pipeline as title %d at %s"
                    % (row["id"], row["stage"])
                )
        if self.cfg.dry_run:
            log.info("DRY RUN would copy %s into import for repair", finding["path"])
            return {"ok": True, "action": "dry run", "path": finding["path"]}
        try:
            destination = self.layout.copy_to_import(finding["path"])
        except FileExistsError as exc:
            raise ValueError(str(exc))
        except OSError as exc:
            raise ValueError(str(exc))
        self.store.finding_mark_import(finding_id, destination)
        log.info("finding %d queued for repair, copied to %s", finding_id, destination)
        return {"ok": True, "action": "copied to import", "path": destination}

    #----- Reporting
    def status(self):
        return {
            "started_at": self.started_at,
            "uptime_s": int(time.time() - self.started_at),
            "queue_depth": self.queue.qsize(),
            "queues": dict({"assess": self.queue.qsize()}, **self.pools.depths()),
            "slots": self.pools.snapshot(),
            "progress": dict(self._progress),
            "stages": self.store.counts_by_stage(),
            "gpu": self.gpu.as_dict(),
            "encode": {
                "path": self.layout.encode,
                "separate_filesystem": self.layout.encode_is_separate(),
                "free_bytes": self.layout.encode_free_bytes(),
            },
            "libraries_mounted": bool(self.layout.libraries),
            "audit": self.auditor.status(),
            "config": self.cfg.as_dict(),
        }
