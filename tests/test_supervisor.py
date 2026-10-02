"""Tests for the process supervisor (app/supervisor.py)."""
import time
from types import SimpleNamespace

import pytest

from app import should_run_schedulers
from app import supervisor as sup


@pytest.fixture(autouse=True)
def _quiet_lifecycle(monkeypatch):
    """Silence lifecycle event emission so tests don't write to the log."""
    monkeypatch.setattr(sup.lifecycle, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(sup.lifecycle, "set_role", lambda *a, **k: None)


class FakeChild(sup.ManagedChild):
    """A ManagedChild that records lifecycle calls instead of spawning."""

    def __init__(self, name="c", health=None):
        super().__init__(name, ["noop"], health_check=lambda: self._health)
        self._poll_result = None
        self._health = health
        self.starts = 0
        self.terminates = 0

    def start(self, now=None):
        self.starts += 1
        # Start well outside the grace window so health checks apply immediately.
        self.started_at = (time.monotonic() - 10_000) if now is None else now
        self.health_failures = 0
        self.startup_complete = False
        self.process = SimpleNamespace(returncode=None)

    def poll(self):
        return self._poll_result

    def terminate(self):
        self.terminates += 1


# --- decide_action truth table ---------------------------------------------

def test_decide_action_crash():
    assert sup.decide_action(exited=True, in_grace=False,
                             health=None, health_failures=0) == "restart_crash"


def test_decide_action_grace_ignores_bad_health():
    assert sup.decide_action(exited=False, in_grace=True,
                             health=False, health_failures=99) == "ok"


def test_decide_action_health_failures_below_threshold():
    assert sup.decide_action(exited=False, in_grace=False,
                             health=False,
                             health_failures=sup.MAX_HEALTH_FAILURES - 1) == "ok"


def test_decide_action_health_failures_at_threshold():
    assert sup.decide_action(exited=False, in_grace=False,
                             health=False,
                             health_failures=sup.MAX_HEALTH_FAILURES) == "restart_hang"


def test_decide_action_healthy_and_indeterminate_are_ok():
    assert sup.decide_action(exited=False, in_grace=False,
                             health=True, health_failures=0) == "ok"
    assert sup.decide_action(exited=False, in_grace=False,
                             health=None, health_failures=0) == "ok"


# --- startup grace ----------------------------------------------------------

def test_in_startup_grace():
    child = sup.ManagedChild("c", ["noop"])
    now = 1000.0
    child.started_at = now
    assert child.in_startup_grace(now + 1) is True
    assert child.in_startup_grace(now + sup.STARTUP_GRACE_SECONDS + 1) is False


def test_slow_schema_startup_is_not_restarted():
    """A child waiting for schema initialization gets more than the old 60s window."""
    child = FakeChild('worker', health=False)
    child.start(now=1000)
    supervisor = sup.Supervisor([child])
    for now in (1030, 1060, 1090, 1120):
        supervisor._check(child, now)
    assert child.starts == 1
    assert child.terminates == 0
    supervisor._check(child, 1000 + sup.STARTUP_GRACE_SECONDS + 1)
    assert child.starts == 2


def test_healthy_child_exits_grace_and_detects_later_hang():
    """Long upgrade grace does not delay hang detection once a child is ready."""
    child = FakeChild('web', health=True)
    child.start(now=1000)
    supervisor = sup.Supervisor([child])
    supervisor._check(child, 1010)
    assert child.startup_complete
    assert not child.in_startup_grace(1011)
    child._health = False
    for now in (1020, 1030, 1040):
        supervisor._check(child, now)
    assert child.starts == 2


# --- crash-loop backoff -----------------------------------------------------

def test_backoff_zero_below_threshold():
    child = sup.ManagedChild("c", ["noop"])
    now = 1000.0
    for _ in range(sup.CRASH_LOOP_MAX):
        child.record_restart(now)
    assert child.backoff_seconds(now) == 0.0


def test_backoff_grows_and_caps_above_threshold():
    child = sup.ManagedChild("c", ["noop"])
    now = 1000.0
    for _ in range(sup.CRASH_LOOP_MAX + 1):
        child.record_restart(now)
    assert child.backoff_seconds(now) == 2.0  # 2 ** 1 excess

    for _ in range(20):
        child.record_restart(now)
    assert child.backoff_seconds(now) == float(sup.MAX_BACKOFF_SECONDS)


def test_restarts_outside_window_are_pruned():
    child = sup.ManagedChild("c", ["noop"])
    child.record_restart(1000.0)
    later = 1000.0 + sup.CRASH_LOOP_WINDOW_SECONDS + 10
    child.record_restart(later)
    assert child.restarts_in_window(later) == 1


# --- supervisor _check behavior --------------------------------------------

def test_check_restarts_on_crash():
    child = FakeChild("web")
    child.start()
    child._poll_result = 1  # process exited
    child.process.returncode = 1
    s = sup.Supervisor([child], poll_interval=0)
    s._check(child, time.monotonic())
    assert child.terminates == 1
    assert child.starts == 2  # initial start + restart


def test_check_restarts_after_repeated_health_failures():
    child = FakeChild("worker", health=False)
    child.start()
    s = sup.Supervisor([child], poll_interval=0)
    now = time.monotonic()

    # First failures below threshold: no restart yet.
    for _ in range(sup.MAX_HEALTH_FAILURES - 1):
        s._check(child, now)
    assert child.starts == 1
    assert child.health_failures == sup.MAX_HEALTH_FAILURES - 1

    # The failure that reaches the threshold triggers a restart.
    s._check(child, now)
    assert child.terminates == 1
    assert child.starts == 2


def test_check_healthy_child_is_left_alone():
    child = FakeChild("web", health=True)
    child.start()
    s = sup.Supervisor([child], poll_interval=0)
    s._check(child, time.monotonic())
    assert child.starts == 1
    assert child.terminates == 0
    assert child.health_failures == 0


# --- graceful-degradation scheduler gating ---------------------------------

def test_worker_always_runs_schedulers():
    assert should_run_schedulers("worker", supervised=True) is True
    assert should_run_schedulers("worker", supervised=False) is True


def test_unsupervised_web_runs_schedulers_inline():
    assert should_run_schedulers("web", supervised=False) is True


def test_supervised_web_defers_schedulers():
    assert should_run_schedulers("web", supervised=True) is False


def test_managed_child_stores_env():
    child = sup.ManagedChild("web", ["x"], env={"SALESBUDDY_SUPERVISED": "1"})
    assert child.env == {"SALESBUDDY_SUPERVISED": "1"}


# --- self-healing pre-flight ------------------------------------------------

def test_port_is_free_detects_busy():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))  # exclusive hold (no SO_REUSEADDR)
    port = s.getsockname()[1]
    s.listen()
    try:
        assert sup._port_is_free(port) is False
    finally:
        s.close()


def test_preflight_sweeps_then_returns_when_port_free(monkeypatch):
    """Pre-flight should attempt the sweep, then return once the port is free.
    subprocess.run + _repo_root are stubbed so the real sweep never runs (it would
    otherwise match this dev checkout's processes)."""
    from pathlib import Path
    calls = []
    monkeypatch.setattr(sup, "_repo_root", lambda: Path("C:/nonexistent-test-root"))
    monkeypatch.setattr(sup.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(sup, "_port_is_free", lambda p: True)
    # os.name is 'nt' on the dev box, so the (stubbed) sweep path runs.
    sup.preflight_clear_stale_backend(5151)
    if sup.os.name == "nt":
        assert len(calls) == 1  # sweep attempted exactly once


def test_preflight_noop_off_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(sup.os, "name", "posix")
    monkeypatch.setattr(sup.subprocess, "run", lambda *a, **k: calls.append(a))
    sup.preflight_clear_stale_backend(5151)
    assert calls == []  # never touches processes off Windows
