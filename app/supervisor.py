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
- web: HTTP GET /health returns 200.
- worker: the web /health 'worker' field reads 'alive' (backed by the worker's
  DB heartbeat). Indeterminate (web unreachable) never triggers a worker restart;
  the web restart path handles that case.

Restart safety:
- A per-child startup grace period avoids restarting a child that is still
  booting.
- A child must fail MAX_HEALTH_FAILURES probes in a row before it's treated as
  hung, so a single blip doesn't cause a restart.
- Crash-loop protection: too many restarts within a window triggers exponential
  backoff (capped) so a permanently broken child doesn't spin the CPU.
"""
from __future__ import annotations

import json
import logging
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
STARTUP_GRACE_SECONDS = 360       # five-minute schema lock wait plus startup overhead
MAX_HEALTH_FAILURES = 3           # consecutive failed probes => hung => restart
CRASH_LOOP_MAX = 5                # restarts within the window before backoff kicks in
CRASH_LOOP_WINDOW_SECONDS = 120
MAX_BACKOFF_SECONDS = 60
HEALTH_TIMEOUT_SECONDS = 5


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

def web_health(port: int) -> bool:
    """Return True if the web /health endpoint responds 200."""
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/health", timeout=HEALTH_TIMEOUT_SECONDS
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def worker_health(port: int) -> Optional[bool]:
    """Return worker liveness via the web /health 'worker' field.

    Returns True if alive, False if stale/not started, or None if it cannot be
    determined (web unreachable) - in which case the worker is left alone.
    """
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/health", timeout=HEALTH_TIMEOUT_SECONDS
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    worker = data.get("worker")
    if worker == "alive":
        return True
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
                 env: Optional[dict] = None):
        self.name = name
        self.argv = argv
        self.health_check = health_check
        self.env = env
        self.process: Optional[subprocess.Popen] = None
        self.started_at = 0.0
        self.health_failures = 0
        self.startup_complete = False
        self._restart_times: List[float] = []

    def start(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self.process = subprocess.Popen(self.argv, cwd=str(_repo_root()), env=self.env)
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
                 stop_event: Optional[threading.Event] = None):
        self.children = children
        self.poll_interval = poll_interval
        self.stop_event = stop_event or threading.Event()

    def _restart(self, child: ManagedChild, reason: str) -> None:
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

    def _check(self, child: ManagedChild, now: float) -> None:
        exited = child.poll() is not None
        in_grace = child.in_startup_grace(now)

        health: Optional[bool] = None
        if not exited and child.health_check is not None:
            health = child.health_check()
            if health is True:
                child.startup_complete = True
                in_grace = False
                child.health_failures = 0
            elif health is False:
                child.health_failures += 1

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
        lifecycle.set_role("supervisor")
        lifecycle.emit_event("supervisor_start",
                             children=[c.name for c in self.children])
        logger.info("Supervisor starting: %s", [c.name for c in self.children])
        for child in self.children:
            child.start()
            lifecycle.emit_event("child_started", child=child.name)
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                for child in self.children:
                    if self.stop_event.is_set():
                        break
                    try:
                        self._check(child, now)
                    except Exception:
                        logger.exception("Supervisor check failed for %s", child.name)
                self.stop_event.wait(self.poll_interval)
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
    exe = Path(sys.executable).parent / (
        "waitress-serve.exe" if os.name == "nt" else "waitress-serve"
    )
    if exe.exists():
        return [str(exe), "--host=0.0.0.0", f"--port={port}",
                "--call", "app:create_app"]
    return [sys.executable, "-m", "waitress", "--host=0.0.0.0",
            f"--port={port}", "--call", "app:create_app"]


def main() -> None:
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

    # Mark children as supervised so the web process defers the heavy schedulers
    # to the worker instead of running them inline.
    child_env = {**os.environ, "SALESBUDDY_SUPERVISED": "1"}
    children = [
        ManagedChild("web", _web_argv(port), env=child_env,
                     health_check=lambda: web_health(port)),
        ManagedChild("worker", [sys.executable, "-m", "app.worker"], env=child_env,
                     health_check=lambda: worker_health(port)),
    ]
    supervisor = Supervisor(children)

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
