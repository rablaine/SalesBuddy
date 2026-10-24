"""
Process supervisor for Sales Buddy.

Spawns and watches the web server and the background worker, restarting either
one if it crashes (the process exits) or hangs (its health probe stops
responding). This is the local stand-in for what an Electron/Tauri main process
will eventually do; the responsibilities are identical so it can be swapped out
later without changing the web/worker processes.

Run:

    python -m app.supervisor

Health model:
- web: HTTP GET /health returns 200 and matches this boot, commit and child PID.
- worker: /health reports a fresh heartbeat matching this boot and worker PID.
  Unreachable web probes leave a running worker alone, but cannot extend its
  initial startup deadline indefinitely.

Restart safety:
- A per-child startup grace period avoids restarting a child that is still
  booting.
- A child must fail MAX_HEALTH_FAILURES probes in a row before it's treated as
  hung, so a single blip doesn't cause a restart.
- Each child has a finite attempt budget, reset only after sustained health.
  Exhaustion stops both children and leaves a durable failed boot status.
- Initial acceptance and each recovery window have a shared 720-second deadline,
  including bounded offline preflight and schema-stamp verification.
- Pending readiness is polled every half second; accepted operation and established
  child health failures retain the normal ten-second polling interval.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
import logging
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

from app.services import lifecycle

logger = logging.getLogger(__name__)

# Tunables
POLL_INTERVAL_SECONDS = 10
STARTUP_POLL_INTERVAL_SECONDS = 0.5
STARTUP_GRACE_SECONDS = 360       # five-minute schema lock wait plus startup overhead
MAX_HEALTH_FAILURES = 3           # consecutive failed probes => hung => restart
CRASH_LOOP_MAX = 5                # restarts within the window before backoff kicks in
CRASH_LOOP_WINDOW_SECONDS = 120
MAX_BACKOFF_SECONDS = 60
HEALTH_TIMEOUT_SECONDS = 5
MAX_CHILD_ATTEMPTS = 3
STABLE_HEALTH_SECONDS = 600
WORKER_HEARTBEAT_MAX_AGE_SECONDS = 90
STARTUP_DEADLINE_SECONDS = 720


def _utc_timestamp() -> str:
    """Return an aware UTC timestamp for durable startup metadata."""
    return datetime.now(timezone.utc).isoformat()


class StartupFailure(RuntimeError):
    """A child exhausted its bounded startup or recovery budget."""


def _preflight_process(repo: Path, preflight: Callable[[Path], None], connection) -> None:
    """Run offline rehearsal separately so the supervisor can bound its lifetime."""
    from scripts.upgrade import diagnostic_error

    try:
        preflight(repo)
        connection.send({'success': True})
    except Exception as error:
        details = getattr(error, 'upgrade_diagnostic', None)
        connection.send({
            'success': False,
            'error': details if isinstance(details, dict) else diagnostic_error(error),
        })
    finally:
        connection.close()


def _run_preflight(
    repo: Path, preflight: Callable[[Path], None], timeout: float,
) -> None:
    """Stop a rehearsal that consumes the remaining overall startup budget."""
    if timeout <= 0:
        raise TimeoutError('Supervisor startup timed out before preflight')
    deadline = time.monotonic() + timeout
    context = multiprocessing.get_context('spawn')
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_preflight_process, args=(repo, preflight, sending))
    try:
        process.start()
        sending.close()
        if not receiving.poll(max(0, deadline - time.monotonic())):
            raise TimeoutError('Supervisor startup timed out during preflight')
        try:
            result = receiving.recv()
        except EOFError as error:
            raise StartupFailure('Database preflight exited without a result') from error
        if result.get('success') is not True:
            error = StartupFailure('Database preflight failed')
            error.upgrade_diagnostic = result.get('error')
            raise error
    finally:
        receiving.close()
        sending.close()
        if process.pid is not None:
            process.join(timeout=0)
            if process.is_alive():
                if os.name == 'nt':
                    try:
                        subprocess.run(
                            ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                            capture_output=True, timeout=5, check=False,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                        )
                    except (OSError, subprocess.SubprocessError):
                        logger.exception('Could not stop the preflight process tree')
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            process.close()


def _run_ready_stamp(repo: Path, timeout: float) -> None:
    """Bound schema-stamp verification by the same remaining acceptance window."""
    from scripts.upgrade import mark_startup_ready

    _run_preflight(repo, mark_startup_ready, timeout)


class StartupStatus:
    """Publish only allowlisted, non-record startup diagnostics atomically."""

    def __init__(self, repo: Path, boot_id: str, expected_commit: Optional[str]):
        from scripts.upgrade import startup_path

        self.repo = repo
        self.path = startup_path(repo)
        self._started_at = self._phase_started_at = time.monotonic()
        self.data = {
            'schema_version': 1, 'state': 'starting', 'phase': 'preflight',
            'boot_id': boot_id, 'expected_commit': expected_commit,
            'started_at': _utc_timestamp(), 'updated_at': _utc_timestamp(),
            'children': [],
        }

    def publish(
        self, state: str, phase: str, children: List[ManagedChild],
        error: Optional[BaseException] = None,
        *, activation_allowed: bool = False,
    ) -> None:
        """Replace status without exception messages or application records."""
        from scripts.upgrade import atomic_json, diagnostic_error

        previous_phase = self.data['phase']
        self.data.update(
            state=state, phase=phase, updated_at=_utc_timestamp(),
            activation_allowed=activation_allowed,
            children=[
                {'name': child.name, 'pid': getattr(child.process, 'pid', None),
                 'attempts': child.attempts, 'ready': child.ready}
                for child in children
            ],
        )
        if error is not None:
            # Rehearsal subprocesses already returned sanitized schema diagnostics.
            details = getattr(error, 'upgrade_diagnostic', None)
            self.data['error'] = details if isinstance(details, dict) else diagnostic_error(error)
        atomic_json(self.path, self.data)
        if phase != previous_phase:
            now = time.monotonic()
            logger.info(
                'Startup transition: previous_phase=%s phase=%s phase_seconds=%.3f '
                'total_seconds=%.3f',
                previous_phase, phase, now - self._phase_started_at, now - self._started_at,
            )
            self._phase_started_at = now


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_port() -> int:
    return int(os.environ.get("PORT", "5151"))


def _port_is_free(port: int) -> bool:
    """True if the web port can be bound (nothing holding it). Strict test (no
    SO_REUSEADDR) so a still-alive squatter reads as busy."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def preflight_clear_stale_backend(port: int) -> None:
    """Self-healing pre-flight, run ONCE by the supervisor before it touches the DB
    or forks web + worker.

    A prior update whose teardown didn't fully complete can leave a STALE backend
    (supervisor / worker / waitress) squatting the web port or holding the DB open.
    That's what makes an update look like it "never came back up" - the new web
    can't bind. Here we kill any leftover install-scoped backend that isn't THIS
    process or its launcher, then wait for the port to actually free, so a fresh
    supervisor always starts from a clean slate. Windows-only (prod). Never raises.
    """
    if os.name != "nt":
        return

    # Never kill ourselves or our venv-launcher parent (both are install-scoped
    # python running app.supervisor). We haven't forked our own children yet, so
    # every OTHER matching process is a leftover from a previous boot.
    keep = {os.getpid()}
    try:
        keep.add(os.getppid())
    except Exception:
        pass

    try:
        root = str(_repo_root()).replace("'", "''")
        keep_list = ",".join(str(p) for p in keep)
        ps = (
            "$keep=@(" + keep_list + "); "
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR "
            "Name='pythonw.exe' OR Name='waitress-serve.exe'\" | Where-Object { "
            "$_.CommandLine -like '*" + root + "*' -and "
            "$keep -notcontains $_.ProcessId } | ForEach-Object { "
            "taskkill /PID $_.ProcessId /T /F 2>$null }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=30, check=False,
        )
        logger.info("preflight: swept stale install-scoped backend (kept %s)", keep)
    except Exception as exc:
        logger.warning("preflight sweep failed (continuing): %s", exc)

    # Wait (bounded) for the port to actually free so waitress binds on the first
    # try instead of crash-looping. A freshly-killed listener sits in TIME_WAIT
    # for a moment, so give it up to ~15s.
    deadline = time.time() + 15
    while time.time() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.5)
    logger.warning("preflight: port %d still busy after wait; web may retry", port)


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------

def web_health(
    port: int, boot_id: Optional[str] = None, expected_commit: Optional[str] = None,
    web_pid: Optional[int] = None,
    *, timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
) -> bool:
    """Return True if the web /health endpoint responds 200."""
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/health" + ("?startup=1" if boot_id else ""),
            timeout=timeout_seconds,
        ) as resp:
            if resp.status != 200:
                return False
            if boot_id is None and expected_commit is None and web_pid is None:
                return True
            data = json.loads(resp.read().decode("utf-8"))
            return (
                (boot_id is None or data.get('boot_id') == boot_id)
                and (expected_commit is None or data.get('commit_full') == expected_commit)
                and (web_pid is None or data.get('web_pid') == web_pid)
            )
    except Exception:
        return False


def worker_health(
    port: int, boot_id: Optional[str] = None, worker_pid: Optional[int] = None,
    expected_commit: Optional[str] = None,
    *, require_ready: bool = False, timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
) -> Optional[bool]:
    """Return worker liveness via the web /health 'worker' field.

    Returns True if alive, False if stale/not started, or None if it cannot be
    determined (web unreachable) - in which case the worker is left alone.
    """
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/health" + ("?startup=1" if boot_id else ""),
            timeout=timeout_seconds,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    worker = data.get("worker")
    if boot_id is not None:
        if (
            data.get('boot_id') != boot_id
            or data.get('worker_boot_id') != boot_id
            or worker_pid is None or data.get('worker_pid') != worker_pid
            or (expected_commit is not None and data.get('commit_full') != expected_commit)
        ):
            return False
        try:
            beat = datetime.fromisoformat(data['worker_heartbeat_at'])
            age = (datetime.now(timezone.utc) - beat).total_seconds()
            if not 0 <= age <= WORKER_HEARTBEAT_MAX_AGE_SECONDS:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    if worker == "alive":
        return not require_ready or data.get('worker_ready') is True
    if worker in ("stale", "not_started"):
        return False
    return None


# ---------------------------------------------------------------------------
# Restart decision (pure, unit-tested)
# ---------------------------------------------------------------------------

def decide_action(*, exited: bool, in_grace: bool,
                  health: Optional[bool], health_failures: int) -> str:
    """Decide what to do about a child this tick.

    Args:
        exited: The child process has exited.
        in_grace: The child is still within its startup grace period.
        health: Latest health probe (True/False/None), already folded into
            ``health_failures`` by the caller.
        health_failures: Consecutive failed health probes so far.

    Returns:
        ``'restart_crash'``, ``'restart_hang'``, or ``'ok'``.
    """
    if exited:
        return "restart_crash"
    if in_grace:
        return "ok"
    if health is False and health_failures >= MAX_HEALTH_FAILURES:
        return "restart_hang"
    return "ok"


# ---------------------------------------------------------------------------
# Managed child
# ---------------------------------------------------------------------------

class ManagedChild:
    """A supervised subprocess with restart bookkeeping."""

    def __init__(self, name: str, argv: List[str],
                 health_check: Optional[Callable[[], Optional[bool]]] = None,
                 env: Optional[dict] = None,
                 readiness_check: Optional[Callable[[], Optional[bool]]] = None):
        self.name = name
        self.argv = argv
        self.health_check = health_check
        self.readiness_check = readiness_check
        self.env = env
        self.process: Optional[subprocess.Popen] = None
        self.started_at = 0.0
        self.health_failures = 0
        self.startup_complete = False
        self.ready = False
        self.healthy = False
        self.attempts = 0
        self.healthy_since: Optional[float] = None
        self._restart_times: List[float] = []

    def start(self, now: Optional[float] = None) -> None:
        """Spawn a child while retaining its lifetime recovery budget."""
        self.attempts += 1
        self.ready = False
        self.healthy = False
        self.healthy_since = None
        now = time.monotonic() if now is None else now
        env = self.env
        executable = None
        if (
            os.name == 'nt' and sys.prefix != sys.base_prefix
            and os.path.normcase(self.argv[0]) == os.path.normcase(sys.executable)
        ):
            base_python = getattr(sys, '_base_executable', None)
            if not base_python or not Path(base_python).is_file():
                raise RuntimeError('The virtual environment base interpreter is unavailable')
            # Use CPython's venv-aware launch; keep argv[0] install-scoped for safe teardown.
            executable = base_python
            env = dict(os.environ if env is None else env)
            env['__PYVENV_LAUNCHER__'] = sys.executable
        self.process = subprocess.Popen(
            self.argv, executable=executable, cwd=str(_repo_root()), env=env,
        )
        self.started_at = now
        self.health_failures = 0
        self.startup_complete = False

    def poll(self) -> Optional[int]:
        return self.process.poll() if self.process else None

    def terminate(self) -> None:
        if not self.process:
            return
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        except Exception:
            logger.debug("terminate failed for %s", self.name, exc_info=True)

    def in_startup_grace(self, now: float) -> bool:
        return not self.startup_complete and (now - self.started_at) < STARTUP_GRACE_SECONDS

    def record_restart(self, now: float) -> None:
        self._restart_times.append(now)
        cutoff = now - CRASH_LOOP_WINDOW_SECONDS
        self._restart_times = [t for t in self._restart_times if t >= cutoff]

    def restarts_in_window(self, now: float) -> int:
        cutoff = now - CRASH_LOOP_WINDOW_SECONDS
        return sum(1 for t in self._restart_times if t >= cutoff)

    def backoff_seconds(self, now: float) -> float:
        """Exponential backoff once restarts exceed the crash-loop threshold."""
        excess = self.restarts_in_window(now) - CRASH_LOOP_MAX
        if excess <= 0:
            return 0.0
        return float(min(MAX_BACKOFF_SECONDS, 2 ** excess))


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self, children: List[ManagedChild],
                 poll_interval: float = POLL_INTERVAL_SECONDS,
                 stop_event: Optional[threading.Event] = None,
                 status: Optional[StartupStatus] = None,
                 startup_started_at: Optional[float] = None):
        self.children = children
        self.poll_interval = poll_interval
        self.stop_event = stop_event or threading.Event()
        self.status = status
        self._readiness_stamped = False
        started = time.monotonic() if startup_started_at is None else startup_started_at
        self._readiness_deadline: Optional[float] = started + STARTUP_DEADLINE_SECONDS

    def _check_deadline(self, now: Optional[float] = None) -> None:
        """Fail at the overall deadline, independently of individual child retries."""
        now = time.monotonic() if now is None else now
        if self._readiness_deadline is not None and now >= self._readiness_deadline:
            raise TimeoutError('Supervisor startup or recovery timed out')

    def probe_timeout(self) -> float:
        """Do not let an HTTP probe borrow time beyond the remaining startup budget."""
        self._check_deadline()
        if self._readiness_deadline is None:
            return HEALTH_TIMEOUT_SECONDS
        return max(0.001, min(
            HEALTH_TIMEOUT_SECONDS, self._readiness_deadline - time.monotonic(),
        ))

    def _poll_wait(self) -> float:
        """Poll pending readiness promptly without shortening hang detection intervals."""
        wait = self.poll_interval
        if self._readiness_deadline is not None:
            established_failure = any(
                child.startup_complete and child.health_failures for child in self.children
            )
            if not established_failure:
                wait = min(wait, STARTUP_POLL_INTERVAL_SECONDS)
            wait = min(wait, max(0, self._readiness_deadline - time.monotonic()))
        return wait

    def _publish(self, error: Optional[BaseException] = None) -> None:
        """Publish readiness only when every current child is healthy."""
        ready = bool(self.children) and all(
            child.ready and child.poll() is None for child in self.children
        )
        if error is None:
            now = time.monotonic()
            if not ready and self._readiness_deadline is None:
                self._readiness_deadline = now + STARTUP_DEADLINE_SECONDS
            self._check_deadline(now)
        if self.status is not None:
            state = 'failed' if error else ('ready' if ready else 'starting')
            preliminary = bool(self.children) and all(
                child.healthy and child.poll() is None for child in self.children
            )
            activating = preliminary and not ready and error is None
            phase = 'activating' if activating else state
            if ready and error is None and not self._readiness_stamped:
                remaining = (
                    self._readiness_deadline - time.monotonic()
                    if self._readiness_deadline is not None else STARTUP_DEADLINE_SECONDS
                )
                _run_ready_stamp(self.status.repo, remaining)
                self._check_deadline()
                self._readiness_stamped = True
            self.status.publish(
                state, phase, self.children, error,
                activation_allowed=preliminary and error is None,
            )
        if ready and error is None:
            self._readiness_deadline = None

    def _restart(self, child: ManagedChild, reason: str) -> None:
        child.ready = False
        child.healthy = False
        child.healthy_since = None
        if child.attempts >= MAX_CHILD_ATTEMPTS:
            raise StartupFailure('Child recovery budget exhausted')
        self._publish()
        now = time.monotonic()
        backoff = child.backoff_seconds(now)
        lifecycle.emit_event("child_restart", child=child.name,
                             reason=reason, backoff=backoff)
        logger.warning("Restarting %s (%s), backoff=%.0fs", child.name, reason, backoff)
        child.terminate()
        if backoff:
            self.stop_event.wait(backoff)
        child.record_restart(time.monotonic())
        child.start()
        self._publish()

    def _check(self, child: ManagedChild, now: float) -> None:
        exited = child.poll() is not None
        in_grace = child.in_startup_grace(now)

        health: Optional[bool] = None
        child.ready = False
        child.healthy = False
        if not exited and child.health_check is not None:
            health = child.health_check()
            self._check_deadline()
            child.healthy = health is True
            if health is True and child.readiness_check is not None:
                health = child.readiness_check()
                self._check_deadline()
            if health is True:
                child.ready = True
                if child.healthy_since is None:
                    child.healthy_since = now
                elif now - child.healthy_since >= STABLE_HEALTH_SECONDS:
                    child.attempts = 1
                    child._restart_times.clear()
                child.startup_complete = True
                in_grace = False
                child.health_failures = 0
            elif health is False:
                child.health_failures += 1
        if health is not True:
            child.healthy_since = None
        if (
            not exited and not in_grace and not child.startup_complete
            and health is None
        ):
            self._restart(child, 'startup_timeout')
            return

        action = decide_action(exited=exited, in_grace=in_grace,
                               health=health, health_failures=child.health_failures)
        if action == "restart_crash":
            code = child.process.returncode if child.process else None
            lifecycle.emit_event("child_crashed", child=child.name, exit_code=code)
            logger.error("%s exited (code=%s)", child.name, code)
            self._restart(child, "crash")
        elif action == "restart_hang":
            lifecycle.emit_event("child_unhealthy", child=child.name)
            logger.error("%s failed %d health checks", child.name, child.health_failures)
            self._restart(child, "hang")

    def run(self) -> None:
        """Run until stopped or fail terminally after a bounded child budget."""
        lifecycle.set_role("supervisor")
        lifecycle.emit_event("supervisor_start",
                             children=[c.name for c in self.children])
        logger.info("Supervisor starting: %s", [c.name for c in self.children])
        try:
            for child in self.children:
                child.start()
                self._publish()
                lifecycle.emit_event("child_started", child=child.name)
            while not self.stop_event.is_set():
                self._check_deadline()
                now = time.monotonic()
                for child in self.children:
                    if self.stop_event.is_set():
                        break
                    self._check_deadline()
                    self._check(child, now)
                self._publish()
                self.stop_event.wait(self._poll_wait())
        except Exception as error:
            for child in self.children:
                child.ready = False
            self._publish(error)
            raise
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        lifecycle.emit_event("supervisor_stop")
        logger.info("Supervisor shutting down children")
        for child in self.children:
            child.terminate()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _web_argv(port: int) -> List[str]:
    """Run the web server directly so its health PID is the supervised process."""
    return [sys.executable, "-m", "waitress", "--host=0.0.0.0",
            f"--port={port}", "--call", "app:create_app"]


def main() -> None:
    """Publish boot identity and fail closed before launching live services."""
    from scripts.upgrade import startup_preflight

    startup_started_at = time.monotonic()
    repo = _repo_root()
    try:
        boot_id = os.environ['SALESBUDDY_BOOT_ID']
        uuid.UUID(boot_id)
    except (KeyError, ValueError):
        boot_id = uuid.uuid4().hex
    os.environ['SALESBUDDY_BOOT_ID'] = boot_id
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=str(repo),
            capture_output=True, text=True, timeout=5, check=True,
        )
        expected_commit = result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        expected_commit = None
    status = StartupStatus(repo, boot_id, expected_commit)
    status.publish('starting', 'preflight', [])
    try:
        _run_boot(status, boot_id, expected_commit, startup_preflight, startup_started_at)
    except Exception as error:
        logger.exception('Supervisor boot failed')
        # Supervisor.run has already published child metadata on child failure.
        if status.data['state'] != 'failed':
            status.publish('failed', 'failed', [], error)
        try:
            from scripts.upgrade import load_state, report_update_failure
            pending = load_state(repo)
            if pending and pending.get('changes_started') and pending.get('phase') not in {
                'ready', 'restored',
            }:
                report_update_failure(repo, pending)
        except (OSError, ValueError, RuntimeError):
            logger.exception('Startup diagnostics could not be queued; recovery files are retained')
        raise SystemExit(1) from error


def _run_boot(
    status: StartupStatus, boot_id: str, expected_commit: Optional[str],
    startup_preflight: Callable[[Path], None],
    startup_started_at: Optional[float] = None,
) -> None:
    """Bootstrap location, rehearse schema, and then supervise both children."""
    # Bootstrap the isolated Azure CLI auth profile BEFORE spawning children so
    # they inherit the resolved AZURE_CONFIG_DIR and a broker-disabled config.
    from app.services.azure_profile import ensure_azure_profile
    ensure_azure_profile()

    port = _default_port()

    # Self-healing pre-flight: kill any STALE install-scoped backend and free the
    # web port BEFORE we touch the DB or fork children. A previous update whose
    # teardown didn't fully complete can leave a squatter on the port (or a stale
    # backend holding the DB open); clearing it here means a fresh supervisor
    # always starts clean and the app reliably comes back up.
    preflight_clear_stale_backend(port)

    # One-time move of the DB to its external location, done ONCE here in the
    # single supervisor process BEFORE forking web + worker (so they never race
    # to migrate). Children carry SALESBUDDY_SUPERVISED and skip their own copy.
    # Also publish the resolved DB path for the PowerShell scripts + installer.
    try:
        from app.db_paths import migrate_db_to_new_location, write_data_path_file
        migrate_db_to_new_location(logger)
        write_data_path_file()
    except Exception:
        logger.exception("DB path bootstrap failed (continuing on legacy location)")

    remaining = STARTUP_DEADLINE_SECONDS
    if startup_started_at is not None:
        remaining -= time.monotonic() - startup_started_at
    _run_preflight(_repo_root(), startup_preflight, remaining)
    if (
        startup_started_at is not None
        and time.monotonic() >= startup_started_at + STARTUP_DEADLINE_SECONDS
    ):
        raise TimeoutError('Supervisor startup timed out during preflight')
    status.publish('starting', 'starting', [])

    # Mark children as supervised so the web process defers the heavy schedulers
    # to the worker instead of running them inline.
    child_env = {
        **os.environ, "SALESBUDDY_SUPERVISED": "1", "SALESBUDDY_BOOT_ID": boot_id,
    }
    children = [
        ManagedChild("web", _web_argv(port), env=child_env,
                     health_check=lambda: web_health(
                         port, boot_id, expected_commit,
                         getattr(children[0].process, 'pid', None),
                         timeout_seconds=supervisor.probe_timeout(),
                     )),
        ManagedChild("worker", [sys.executable, "-m", "app.worker"], env=child_env,
                     health_check=lambda: worker_health(
                         port, boot_id, getattr(children[1].process, 'pid', None),
                         expected_commit,
                         timeout_seconds=supervisor.probe_timeout(),
                     ),
                     readiness_check=lambda: worker_health(
                         port, boot_id, getattr(children[1].process, 'pid', None),
                         expected_commit, require_ready=True,
                         timeout_seconds=supervisor.probe_timeout(),
                     )),
    ]
    supervisor = Supervisor(
        children, status=status, startup_started_at=startup_started_at,
    )

    def _handle_signal(signum, _frame):
        logger.info("Supervisor received signal %s", signum)
        supervisor.stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    supervisor.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [supervisor] %(levelname)s %(message)s")
    main()
