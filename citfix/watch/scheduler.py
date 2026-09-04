"""Scheduler registration: prefer APScheduler, fallback to threading timers."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from citfix.watch.config import WatchConfig, load_watch_config
from citfix.watch.service import run_watch_cycle


class _ThreadScheduler:
    """Minimal interval scheduler when apscheduler is not installed."""

    def __init__(self) -> None:
        self._jobs: list[tuple[str, float, Callable[[], None]]] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def add_job(
        self,
        func: Callable[[], None],
        trigger: str,
        seconds: int = 60,
        id: str = "",
        replace_existing: bool = True,
        max_instances: int = 1,
        **_kwargs: Any,
    ) -> None:
        self._jobs.append((id or func.__name__, float(seconds), func))

    def start(self) -> None:
        self._stop.clear()

        def _loop(interval: float, fn: Callable[[], None]) -> None:
            while not self._stop.is_set():
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    print(f"[citfix.watch] job error: {e}")
                self._stop.wait(interval)

        for jid, interval, fn in self._jobs:
            t = threading.Thread(target=_loop, args=(interval, fn), name=jid, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self, wait: bool = False) -> None:
        self._stop.set()
        if wait:
            for t in self._threads:
                t.join(timeout=2)


def register_jobs(scheduler: Any, cfg: WatchConfig | None = None) -> Any:
    cfg = cfg or load_watch_config()

    def _runs() -> None:
        run_watch_cycle(cfg, include_batches=False)

    def _batches() -> None:
        run_watch_cycle(cfg, include_batches=True)

    def _retries() -> None:
        run_watch_cycle(cfg, include_batches=False)

    scheduler.add_job(
        _runs,
        "interval",
        seconds=cfg.scan_runs_seconds,
        id="citfix_scan_runs",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _batches,
        "interval",
        seconds=cfg.scan_batches_seconds,
        id="citfix_scan_batches",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _retries,
        "interval",
        seconds=cfg.scan_retry_seconds,
        id="citfix_due_retries",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler


def create_background_scheduler(cfg: WatchConfig | None = None) -> Any:
    """Prefer APScheduler; fall back to threading if package missing."""
    cfg = cfg or load_watch_config()
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        sched: Any = BackgroundScheduler()
        backend = "apscheduler"
    except ImportError:
        sched = _ThreadScheduler()
        backend = "threading"
        print(
            "[citfix.watch] apscheduler not installed — using threading fallback. "
            "Optional: pip install -r requirements-watch.txt"
        )
    register_jobs(sched, cfg)
    sched._citfix_backend = backend  # type: ignore[attr-defined]
    return sched
