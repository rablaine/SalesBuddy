"""Tests for the process supervisor (app/supervisor.py)."""
import time
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import should_run_schedulers
from app import supervisor as sup


@pytest.fixture(autouse=True)
def _quiet_lifecycle(monkeypatch):
    """Silence lifecycle event emission so tests don't write to the log."""
    monkeypatch.setattr(sup.lifecycle, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(sup.lifecycle, "set_role", lambda *a, **k: None)
    monkeypatch.setattr('scripts.upgrade.mark_startup_ready', lambda repo: None)

    def mark_ready(repo, timeout):
        """Keep schema-stamp spies in this process without launching test subprocesses."""
        from scripts.upgrade import mark_startup_ready
        mark_startup_ready(repo)

    monkeypatch.setattr(sup, '_run_ready_stamp', mark_ready)


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
        self.attempts += 1
        self.ready = False
        self.healthy = False
        self.healthy_since = None
        # Start well outside the grace window so health checks apply immediately.
        self.started_at = (time.monotonic() - 10_000) if now is None else now
        self.health_failures = 0
        self.startup_complete = False
        self.process = SimpleNamespace(returncode=None, pid=100 + self.starts)

    def poll(self):
        return self._poll_result

    def terminate(self):
        self.terminates += 1


@pytest.mark.skipif(os.name != 'nt' or sys.prefix == sys.base_prefix,
                    reason='Windows virtual-environment redirector regression')
def test_managed_windows_python_pid_is_actual_venv_process(tmp_path, monkeypatch):
    """The health PID must equal Popen.pid while keeping the installed venv and environment."""
    monkeypatch.setattr(sup, '_repo_root', lambda: tmp_path)
    output = tmp_path / 'child-identity.json'
    code = (
        'import ctypes, json, os, sys; from pathlib import Path; import flask, sqlalchemy; '
        'ctypes.windll.kernel32.GetCommandLineW.restype=ctypes.c_wchar_p; '
        'Path(sys.argv[1]).write_text(json.dumps(dict('
        'pid=os.getpid(), prefix=sys.prefix, executable=sys.executable, '
        'flask=flask.__file__, flag=os.environ.get("VM_PID_TEST"), '
        'command_line=ctypes.windll.kernel32.GetCommandLineW())))'
    )
    environment = {**os.environ, 'VM_PID_TEST': 'preserved'}
    before = dict(environment)
    child = sup.ManagedChild('worker', [sys.executable, '-c', code, str(output)], env=environment)
    child.start()
    assert child.process is not None
    assert child.process.wait(timeout=20) == 0
    identity = json.loads(output.read_text(encoding='utf-8'))
    assert identity['pid'] == child.process.pid
    assert identity['prefix'] == sys.prefix
    assert identity['executable'] == sys.executable
    assert Path(identity['flask']).is_relative_to(Path(sys.prefix))
    assert identity['flag'] == 'preserved'
    assert sys.executable in identity['command_line']
    assert environment == before


def test_managed_web_uses_python_module_not_another_executable_launcher():
    """Waitress console launchers must not insert another process between supervisor and web."""
    assert sup._web_argv(5151) == [
        sys.executable, '-m', 'waitress', '--host=0.0.0.0', '--port=5151',
        '--call', 'app:create_app',
    ]


@pytest.mark.parametrize(('elapsed', 'expired'), [
    (719.999, False), (720.0, True), (720.001, True),
])
def test_overall_startup_deadline_exact_threshold(elapsed, expired):
    """The overall budget expires at 720 seconds, not one tick or retry later."""
    supervisor = sup.Supervisor([], startup_started_at=1000)
    assert sup.STARTUP_DEADLINE_SECONDS == 720
    if expired:
        with pytest.raises(TimeoutError):
            supervisor._check_deadline(1000 + elapsed)
    else:
        supervisor._check_deadline(1000 + elapsed)


def test_probes_use_only_remaining_startup_budget(monkeypatch):
    """A probe at the end of startup does not add another five-second timeout."""
    clock = [0.0]
    monkeypatch.setattr(sup.time, 'monotonic', lambda: clock[0])
    supervisor = sup.Supervisor([])
    assert supervisor.probe_timeout() == sup.HEALTH_TIMEOUT_SECONDS
    clock[0] = 719.75
    assert supervisor.probe_timeout() == 0.25
    clock[0] = 720
    with pytest.raises(TimeoutError):
        supervisor.probe_timeout()


def test_startup_timeout_stops_both_children_and_persists_failure(monkeypatch, tmp_path):
    """Overall timeout is terminal even when each child's retry budget remains."""
    import json

    clock = [0.0]
    monkeypatch.setattr(sup.time, 'monotonic', lambda: clock[0])
    web = FakeChild('web')
    worker = FakeChild('worker')

    def timed_out_probe():
        """Consume the remaining startup window during one bounded probe."""
        clock[0] = 720.0
        return False

    web.health_check = timed_out_probe
    status = sup.StartupStatus(tmp_path, 'boot', None)
    supervisor = sup.Supervisor([web, worker], status=status, poll_interval=0)
    with pytest.raises(TimeoutError):
        supervisor.run()
    state = json.loads(status.path.read_text(encoding='utf-8'))
    assert state['state'] == state['phase'] == 'failed'
    assert state['activation_allowed'] is False
    assert state['error']['category'] == 'timeout'
    assert all(child['ready'] is False for child in state['children'])
    assert web.starts == worker.starts == 1
    assert web.terminates == worker.terminates == 1


def test_readiness_loss_starts_one_fresh_bounded_recovery_window(monkeypatch):
    """Healthy uptime is unlimited, but recovery retries cannot extend their deadline."""
    clock = [1000.0]
    monkeypatch.setattr(sup.time, 'monotonic', lambda: clock[0])
    web = FakeChild('web', health=True)
    worker = FakeChild('worker', health=True)
    web.start()
    worker.start()
    supervisor = sup.Supervisor([web, worker])
    supervisor._check(web, clock[0])
    supervisor._check(worker, clock[0])
    supervisor._publish()
    assert supervisor._readiness_deadline is None
    clock[0] = 10000
    supervisor._publish()
    worker.ready = False
    worker.healthy = False
    supervisor._publish()
    assert supervisor._readiness_deadline == 10720
    clock[0] = 10500
    supervisor._publish()
    assert supervisor._readiness_deadline == 10720
    supervisor._check_deadline(10719.999)
    with pytest.raises(TimeoutError):
        supervisor._check_deadline(10720)
    # Successful recovery clears this window; a subsequent loss gets a new one.
    clock[0] = 10600
    worker.ready = True
    worker.healthy = True
    supervisor._publish()
    assert supervisor._readiness_deadline is None
    clock[0] = 12000
    worker.ready = False
    supervisor._publish()
    assert supervisor._readiness_deadline == 12720


def test_late_ready_cannot_erase_expired_startup_window(monkeypatch):
    """A last-second health success cannot accept startup at or beyond its deadline."""
    clock = [0.0]
    monkeypatch.setattr(sup.time, 'monotonic', lambda: clock[0])
    child = FakeChild('worker', health=True)
    child.start()
    supervisor = sup.Supervisor([child])
    child.ready = child.healthy = True
    clock[0] = 720.0
    with pytest.raises(TimeoutError):
        supervisor._publish()
    assert supervisor._readiness_deadline == 720.0


def test_schema_stamp_cannot_extend_final_acceptance_deadline(monkeypatch, tmp_path):
    """Final stamp verification shares the original window instead of resetting it."""
    clock = [0.0]
    monkeypatch.setattr(sup.time, 'monotonic', lambda: clock[0])
    child = FakeChild('worker', health=True)
    child.start()
    status = sup.StartupStatus(tmp_path, 'boot', None)
    supervisor = sup.Supervisor([child], status=status)
    child.ready = child.healthy = True

    def late_stamp(repo, timeout):
        """Finish exactly at expiry to prove that final acceptance is rejected."""
        assert timeout == 720
        clock[0] = 720.0

    monkeypatch.setattr(sup, '_run_ready_stamp', late_stamp)
    with pytest.raises(TimeoutError):
        supervisor._publish()
    assert status.data['state'] == 'starting'
    assert supervisor._readiness_stamped is False


def test_preflight_timeout_terminates_its_process_without_waiting_for_core_timeout(
    monkeypatch, tmp_path,
):
    """Offline rehearsal cannot inherit the coordinator's longer command timeout."""
    calls = []
    alive = [True]
    receiving = SimpleNamespace(
        poll=lambda timeout: calls.append(('poll', timeout)) or False,
        close=lambda: None,
    )
    sending = SimpleNamespace(close=lambda: None)

    def terminate():
        """Record termination without creating any real process."""
        calls.append('terminate')
        alive[0] = False

    process = SimpleNamespace(
        pid=123,
        start=lambda: calls.append('start'),
        join=lambda timeout: calls.append(('join', timeout)),
        is_alive=lambda: alive[0],
        terminate=terminate,
        kill=lambda: pytest.fail('Terminated preflight should not need killing'),
        close=lambda: calls.append('close'),
    )
    context = SimpleNamespace(
        Pipe=lambda **kwargs: (receiving, sending),
        Process=lambda **kwargs: process,
    )
    monkeypatch.setattr(sup.multiprocessing, 'get_context', lambda method: context)
    monkeypatch.setattr(sup.time, 'monotonic', lambda: 1000)
    monkeypatch.setattr(sup.subprocess, 'run', lambda *args, **kwargs: None)
    with pytest.raises(TimeoutError):
        sup._run_preflight(tmp_path, lambda repo: None, 12)
    assert ('poll', 12) in calls
    assert 'terminate' in calls
    assert ('join', 5) in calls
    assert calls[-1] == 'close'


def test_preflight_process_preserves_safe_migration_details(tmp_path):
    """The bounded subprocess retains sanitized counts rather than wrapper frames."""
    details = {
        'type': 'IntegrityError', 'category': 'migration_foreign_keys',
        'violation_count': 2, 'parent_tables': ['customers'], 'frames': [],
    }
    messages = []
    connection = SimpleNamespace(send=messages.append, close=lambda: None)

    def fail(repo):
        """Simulate the safe exception returned by the coordinator."""
        error = RuntimeError('Private record data must not leave this process')
        error.upgrade_diagnostic = details
        raise error

    sup._preflight_process(tmp_path, fail, connection)
    assert messages == [{'success': False, 'error': details}]


def test_permanent_child_crash_is_terminal_and_stops_both(monkeypatch, tmp_path):
    """A permanent crash cannot be hidden by a sliding retry window."""
    crashed = FakeChild('web')
    crashed._poll_result = 1
    worker = FakeChild('worker', health=True)
    status = sup.StartupStatus(tmp_path, 'boot', 'a' * 40)
    supervisor = sup.Supervisor([crashed, worker], poll_interval=0, status=status)
    with pytest.raises(sup.StartupFailure):
        supervisor.run()
    import json
    state = json.loads(status.path.read_text(encoding='utf-8'))
    assert state['state'] == 'failed'
    assert state['phase'] == 'failed'
    assert state['boot_id'] == 'boot'
    assert state['expected_commit'] == 'a' * 40
    assert crashed.starts == sup.MAX_CHILD_ATTEMPTS
    assert crashed.terminates >= 1 and worker.terminates >= 1
    assert all(not child['ready'] for child in state['children'])
    assert 'error' in state


@pytest.mark.parametrize('health', [False, None])
def test_repeated_startup_hangs_exhaust_budget(health):
    """Expired deadlines are finite even when the worker probe is indeterminate."""
    child = FakeChild('worker', health=health)
    child.start(now=0)
    supervisor = sup.Supervisor([child], poll_interval=0)
    with pytest.raises(sup.StartupFailure):
        for tick in range(100):
            supervisor._check(child, time.monotonic() + 10000 + tick)
    assert child.starts == sup.MAX_CHILD_ATTEMPTS


def test_fleeting_health_does_not_reset_budget():
    """A single good response must not permit an endless crash/healthy cycle."""
    child = FakeChild('worker', health=True)
    child.start(now=1000)
    child.attempts = sup.MAX_CHILD_ATTEMPTS
    supervisor = sup.Supervisor([child])
    supervisor._check(child, 1001)
    assert child.attempts == sup.MAX_CHILD_ATTEMPTS
    child._poll_result = 1
    with pytest.raises(sup.StartupFailure):
        supervisor._check(child, 1002)


def test_only_sustained_health_resets_budget():
    """Meaningful continuous health admits a new recovery budget."""
    child = FakeChild('worker', health=True)
    child.start(now=1000)
    child.attempts = sup.MAX_CHILD_ATTEMPTS
    supervisor = sup.Supervisor([child])
    supervisor._check(child, 1001)
    supervisor._check(child, 1001 + sup.STABLE_HEALTH_SECONDS)
    assert child.attempts == 1


def test_partial_readiness_never_accepts_boot(tmp_path):
    """The durable ready flag requires both live current children."""
    web = FakeChild('web', health=True)
    worker = FakeChild('worker', health=False)
    web.start(now=1000)
    worker.start(now=1000)
    status = sup.StartupStatus(tmp_path, 'boot', None)
    supervisor = sup.Supervisor([web, worker], status=status)
    supervisor._check(web, 1001)
    supervisor._check(worker, 1001)
    supervisor._publish()
    assert status.data['state'] == 'starting'
    worker._health = True
    supervisor._check(worker, 1002)
    supervisor._publish()
    assert status.data['state'] == 'ready'
    worker._poll_result = 1
    supervisor._publish()
    assert status.data['state'] == 'starting'


def test_readiness_stamp_only_after_both_children_are_healthy(monkeypatch, tmp_path):
    """Schema/commit success is stamped once on acceptance, not at preflight."""
    stamped = []
    monkeypatch.setattr('scripts.upgrade.mark_startup_ready', stamped.append)
    web = FakeChild('web', health=True)
    worker = FakeChild('worker', health=False)
    web.start(now=1000)
    worker.start(now=1000)
    status = sup.StartupStatus(tmp_path, 'boot', None)
    supervisor = sup.Supervisor([web, worker], status=status)
    supervisor._check(web, 1001)
    supervisor._check(worker, 1001)
    supervisor._publish()
    assert stamped == []
    worker._health = True
    supervisor._check(worker, 1002)
    supervisor._publish()
    supervisor._publish()
    assert stamped == [tmp_path]
    assert status.data['state'] == 'ready'


def test_activation_precedes_final_acceptance_and_schema_stamp(monkeypatch, tmp_path):
    """A live worker cannot mark startup successful until services initialize."""
    stamps = []
    monkeypatch.setattr('scripts.upgrade.mark_startup_ready', stamps.append)
    web = FakeChild('web', health=True)
    worker = FakeChild('worker', health=True)
    service_ready = False
    worker.readiness_check = lambda: service_ready
    web.start(now=1000)
    worker.start(now=1000)
    status = sup.StartupStatus(tmp_path, 'boot', None)
    supervisor = sup.Supervisor([web, worker], status=status)
    supervisor._check(web, 1001)
    supervisor._check(worker, 1001)
    supervisor._publish()
    assert status.data['state'] == 'starting'
    assert status.data['phase'] == 'activating'
    assert status.data['activation_allowed'] is True
    assert status.data['children'][1]['ready'] is False
    assert stamps == []
    service_ready = True
    supervisor._check(worker, 1002)
    supervisor._publish()
    assert status.data['state'] == status.data['phase'] == 'ready'
    assert stamps == [tmp_path]


def test_service_initialization_crash_never_accepts_startup(monkeypatch, tmp_path):
    """A worker that crashes during activation exhausts retries without acceptance."""
    stamps = []
    monkeypatch.setattr('scripts.upgrade.mark_startup_ready', stamps.append)
    web = FakeChild('web', health=True)
    worker = FakeChild('worker', health=True)
    worker.readiness_check = lambda: False
    web.start(now=1000)
    worker.start(now=1000)
    status = sup.StartupStatus(tmp_path, 'boot', None)
    supervisor = sup.Supervisor([web, worker], status=status)
    supervisor._check(web, 1001)
    supervisor._check(worker, 1001)
    supervisor._publish()
    assert status.data['phase'] == 'activating'
    worker._poll_result = 1
    with pytest.raises(sup.StartupFailure):
        for tick in range(sup.MAX_CHILD_ATTEMPTS + 1):
            supervisor._check(worker, 1002 + tick)
            supervisor._publish()
    assert stamps == []
    assert status.data['state'] != 'ready'


@pytest.mark.parametrize('boot_id', ['0' * 32, '12345678-1234-4234-8234-123456789abc'])
@pytest.mark.parametrize('rehearsal_details', [
    None,
    {
        'type': 'IntegrityError', 'category': 'migration_foreign_keys',
        'violation_count': 2, 'parent_tables': ['customers'],
        'frames': [{'file': 'migrations.py', 'function': 'run_migrations', 'line': 42}],
    },
])
def test_main_preflight_failure_leaves_durable_failure_without_children(
    monkeypatch, tmp_path, rehearsal_details, boot_id,
):
    """Rehearsal failure is terminal, sanitized and happens before any child spawn."""
    import json

    monkeypatch.setenv('SALESBUDDY_BOOT_ID', boot_id)
    monkeypatch.setattr(sup, '_repo_root', lambda: tmp_path)
    monkeypatch.setattr(
        sup.subprocess, 'run',
        lambda *args, **kwargs: SimpleNamespace(stdout='a' * 40),
    )
    monkeypatch.setattr('app.services.azure_profile.ensure_azure_profile', lambda: None)
    monkeypatch.setattr(sup, 'preflight_clear_stale_backend', lambda port: None)
    monkeypatch.setattr(
        sup, '_run_preflight', lambda repo, preflight, timeout: preflight(repo),
    )
    order = []
    monkeypatch.setattr(
        'app.db_paths.migrate_db_to_new_location', lambda *args: order.append('bootstrap'),
    )
    monkeypatch.setattr('app.db_paths.write_data_path_file', lambda: None)

    def fail_preflight(repo):
        """Verify status is published before the safe rehearsal fails."""
        from scripts.upgrade import startup_path
        state = json.loads(startup_path(repo).read_text(encoding='utf-8'))
        assert state['phase'] == 'preflight'
        assert state['state'] == 'starting'
        assert state['boot_id'] == boot_id
        order.append('preflight')
        error = RuntimeError('PRIVATE-CUSTOMER-RECORD-MUST-NOT-LEAK')
        if rehearsal_details is not None:
            error.upgrade_diagnostic = rehearsal_details
        raise error

    monkeypatch.setattr('scripts.upgrade.startup_preflight', fail_preflight)
    monkeypatch.setattr(
        sup.ManagedChild, 'start',
        lambda *args: pytest.fail('Preflight must finish before children start'),
    )
    with pytest.raises(SystemExit) as caught:
        sup.main()
    assert caught.value.code != 0
    from scripts.upgrade import startup_path
    raw = startup_path(tmp_path).read_text(encoding='utf-8')
    state = json.loads(raw)
    assert order == ['bootstrap', 'preflight']
    assert state['state'] == state['phase'] == 'failed'
    assert state['children'] == []
    assert 'PRIVATE-CUSTOMER' not in raw
    if rehearsal_details is not None:
        assert state['error'] == rehearsal_details


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
