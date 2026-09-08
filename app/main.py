import logging
import os
import signal
import sys
import threading
import time

from . import gpu as gpumod
from . import locks
from . import provider as providermod
from . import VERSION
from . import state, webui
from .config import Config, ConfigError
from .orchestrator import Orchestrator
from .paths import Layout, LayoutError

log = logging.getLogger("main")

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def setup_logging(level, log_path=None):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            handlers.append(logging.FileHandler(log_path))
        except OSError:
            pass
    logging.basicConfig(
        level=LEVELS.get(level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)-13s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def main():
    try:
        cfg = Config()
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2

    layout = Layout(cfg)
    log_path = os.path.join(layout.logs, "mediaimport.log")
    setup_logging(cfg.log_level, log_path)

    log.info("mediaimport starting, version %s", VERSION)
    for line in cfg.banner().splitlines():
        log.info("config  %s", line)
    for name in sorted(cfg.sources):
        log.debug("config  %s came from %s", name, cfg.sources[name])
    log.debug(
        "config  encoder threads %d from %s, %d per job at CPU_SLOTS=%d",
        cfg.encode_threads, cfg.thread_source, cfg.encode_threads_per_job, cfg.cpu_slots,
    )

    try:
        layout.ensure()
    except (LayoutError, PermissionError) as exc:
        log.error("layout error: %s", exc)
        return 2

    shutdown = threading.Event()
    running = {}

    def handle_signal(signum, frame):
        log.info("signal %d received, stopping", signum)
        shutdown.set()
        orch = running.get("orchestrator")
        if orch is not None:
            orch.stop()
        ui = running.get("ui")
        if ui is not None:
            ui.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    def on_wait(exc, waited):
        if waited == 0:
            log.warning("%s", exc)
            log.warning(
                "waiting for it to exit rather than starting a second instance; "
                "two instances sharing the same mounts would sweep each other's "
                "encode area and publish the same title twice"
            )
        else:
            log.warning("still waiting for the instance lock after %d min", int(waited // 60))

    instance_lock = locks.InstanceLock(layout.instance_lock)
    try:
        instance_lock.acquire_blocking(
            stop_event=shutdown,
            timeout=cfg.lock_wait_timeout,
            interval=cfg.lock_wait_interval,
            on_wait=on_wait,
        )
    except locks.LockWaitInterrupted:
        log.info("stopped while waiting for the instance lock")
        return 0
    except locks.AlreadyRunning as exc:
        log.error("giving up after LOCK_WAIT_TIMEOUT=%ds: %s", cfg.lock_wait_timeout, exc)
        return 3
    log.info("holding the instance lock")

    for line in layout.describe().splitlines():
        log.info("layout  %s", line)

    if not layout.encode_is_separate():
        log.warning(
            "the encode area is on the same filesystem as the rest of the root, "
            "per-title work will not get the fast-storage benefit"
        )
    if not layout.libraries:
        log.warning("no library mounted, the incumbent comparison is disabled")

    gpu_status = gpumod.probe(cfg)
    if gpu_status.available:
        log.info("gpu     %s", gpu_status.reason)
    else:
        log.warning("gpu     degraded: %s", gpu_status.reason)
        if cfg.output_codec == "av1":
            log.warning("gpu     OUTPUT_CODEC is av1, every encode will fall back to libsvtav1")

    try:
        webui.build_ssl_context(cfg)
    except webui.TLSError as exc:
        log.error("%s", exc)
        log.error("the web UI serves HTTPS only and never falls back to plain HTTP")
        return 2

    if cfg.dry_run:
        log.warning("DRY_RUN is set, no file will be moved, encoded or quarantined")

    store = state.Store(layout.state_db)
    client = providermod.Client(layout.provider_cache)
    provider = providermod.Provider(client, import_root=layout.imports)

    orchestrator = Orchestrator(cfg, layout, store, gpu_status, provider=provider)
    ui = webui.WebUI(cfg, orchestrator, store, log_path=log_path)

    running["orchestrator"] = orchestrator
    running["ui"] = ui

    orchestrator.start()
    ui.start()
    log.info("ready, watching %s", layout.imports)

    try:
        while not orchestrator.stop_event.is_set() and not shutdown.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        orchestrator.stop()

    ui.stop()
    orchestrator.terminate_encodes()
    store.close()
    instance_lock.release()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
