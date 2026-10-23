"""
Background worker process entrypoint for Sales Buddy.

Runs the heavy background schedulers (MSX / WorkIQ / meeting sync) and the
durable job-queue consumer in a process **separate** from the web server, so a
slow or hung background job can never wedge the UI. The web server (waitress or
``flask run``) runs with the default ``web`` role and no longer starts these
schedulers.

Launch:

    python -m app.worker

Dev convenience wrapper: ``scripts/worker.ps1``.
"""
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# SyncStatus sync_type used as the worker liveness heartbeat that /health reads.
WORKER_HEARTBEAT_KEY = "worker_process"
_HEARTBEAT_INTERVAL_SECONDS = 30.0
STARTUP_ACCEPTANCE_TIMEOUT_SECONDS = 1200.0


def _requires_startup_acceptance() -> bool:
    """Require managed admission except for the explicitly marked local dev stack."""
    supervised = os.environ.get('SALESBUDDY_SUPERVISED', '').strip().lower() in (
        '1', 'true', 'yes',
    )
    direct_dev_stack = (
        os.environ.get('SALESBUDDY_DEV_STACK', '').strip() == '1'
        and os.environ.get('FLASK_ENV', '').strip().lower() == 'development'
        and not os.environ.get('SALESBUDDY_BOOT_ID', '').strip()
    )
    return supervised and not direct_dev_stack


def wait_for_startup_acceptance(
    stop_event: threading.Event,
    timeout_seconds: float = STARTUP_ACCEPTANCE_TIMEOUT_SECONDS,
) -> bool:
    """Await preliminary activation admission, never trusting a previous boot."""
    if not _requires_startup_acceptance():
        return True
    boot_id = os.environ.get('SALESBUDDY_BOOT_ID')
    if not boot_id:
        raise RuntimeError('Supervised worker has no boot identity')
    from scripts.upgrade import startup_path

    path = startup_path(Path(__file__).resolve().parent.parent)
    deadline = time.monotonic() + timeout_seconds
    while not stop_event.is_set():
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        if state.get('boot_id') == boot_id:
            if state.get('state') == 'failed':
                raise RuntimeError('Supervisor rejected startup')
            children = state.get('children', [])
            if (
                state.get('state') == 'starting'
                and state.get('phase') == 'activating'
                and state.get('activation_allowed') is True
                and any(
                    child.get('name') == 'worker'
                    and child.get('pid') == os.getpid()
                    for child in children
                )
            ):
                return True
        if time.monotonic() >= deadline:
            raise TimeoutError('Supervisor startup acceptance timed out')
        stop_event.wait(min(0.5, max(0, deadline - time.monotonic())))
    return False


def _publish_worker_heartbeat(app, ready_event: threading.Event) -> None:
    """Publish initialization completion independently from process liveness."""
    from app.services.sync_runtime import worker_heartbeat

    with app.app_context():
        worker_heartbeat(ready=ready_event.is_set())


def _start_worker_heartbeat(
    app, stop_event: threading.Event, ready_event: threading.Event,
) -> None:
    """Keep worker liveness observable even during a long SQLite write."""
    _publish_worker_heartbeat(app, ready_event)

    def _beat() -> None:
        while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
            try:
                _publish_worker_heartbeat(app, ready_event)
            except Exception:
                logger.exception("Worker heartbeat could not be published")

    threading.Thread(target=_beat, daemon=True, name="worker-heartbeat").start()


def main() -> None:
    """Boot the worker process and run the job loop until signalled."""
    # Set role before construction, but defer remote schedulers until acceptance.
    os.environ["SALESBUDDY_ROLE"] = "worker"

    from app import create_app, start_background_services
    from app.services import activity_coverage, activity_enrichment  # noqa: F401
    from app.services.job_queue import run_worker_loop

    app = create_app(defer_background_services=True)

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Worker received signal %s, shutting down", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    supervised = _requires_startup_acceptance()
    ready_event = threading.Event()
    try:
        if supervised:
            _start_worker_heartbeat(app, stop_event, ready_event)
        if not wait_for_startup_acceptance(stop_event):
            return
        start_background_services(app)
        ready_event.set()
        if supervised:
            _publish_worker_heartbeat(app, ready_event)
        else:
            _start_worker_heartbeat(app, stop_event, ready_event)
        logger.info("Sales Buddy worker starting job loop")
        run_worker_loop(app, stop_event=stop_event)
        logger.info("Sales Buddy worker exited")
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
