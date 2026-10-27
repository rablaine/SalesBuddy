"""Startup identity, worker acceptance and scheduler-gating regression coverage."""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import pytest
from flask import Flask

from app import start_background_services
from app import supervisor as sup
from app import worker


@pytest.mark.parametrize(('testing', 'supervised', 'expected_calls'), [
    (False, False, 1),
    (False, True, 0),
    (True, False, 0),
    (True, True, 0),
])
def test_app_factory_preflight_only_for_unsupervised_real_launches(
    monkeypatch, tmp_path, testing, supervised, expected_calls,
):
    """Direct launches rehearse before mutations; supervised and test boots skip it."""
    from app import create_app

    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1' if supervised else '0')
    if testing:
        monkeypatch.setenv('TESTING', 'true')
    else:
        monkeypatch.delenv('TESTING', raising=False)
    calls = []
    preflight = Mock(side_effect=lambda repo: calls.append('preflight'))
    monkeypatch.setattr('scripts.upgrade.startup_preflight', preflight)
    monkeypatch.setattr('app.db_paths.resolve_db_path', lambda: tmp_path / 'isolated.db')
    monkeypatch.setattr(
        'app.db_paths.migrate_db_to_new_location', lambda: calls.append('location'),
    )
    monkeypatch.setattr('app.db_paths.write_data_path_file', lambda: None)
    monkeypatch.setattr('app.services.azure_profile.ensure_azure_profile', lambda: None)

    def initialize(database) -> None:
        """Create only a real isolated schema while recording mutation ordering."""
        calls.append('schema')
        database.create_all()

    monkeypatch.setattr('app.services.database_startup.initialize_database', initialize)
    monkeypatch.setattr('app.services.shell_prefs.reconcile_shell_prefs', lambda *args: None)
    monkeypatch.setattr('app.services.telemetry.init_telemetry', lambda *args: None)
    monkeypatch.setattr('app.services.diagnostic_log.init_diagnostic_log', lambda *args: None)
    monkeypatch.setattr('app.services.lifecycle.init_lifecycle_logging', lambda *a, **k: None)
    monkeypatch.setattr('app.start_background_services', lambda *args: None)
    sha = '2abcc4507bed30c94061059fc6c4812668264229'
    git_run = Mock(side_effect=lambda args, **kwargs: SimpleNamespace(
        stdout=sha if args == ['git', 'rev-parse', 'HEAD'] else '',
    ))
    monkeypatch.setattr('subprocess.run', git_run)
    application = create_app()
    assert application.config['BOOT_COMMIT_FULL'] == sha
    assert application.config['BOOT_COMMIT'] == sha[:7]
    assert sum(
        call.args[0] == ['git', 'rev-parse', 'HEAD'] for call in git_run.call_args_list
    ) == 1
    assert preflight.call_count == expected_calls
    if expected_calls:
        preflight.assert_called_once_with(Path(__file__).resolve().parent.parent)
        assert calls.index('preflight') < calls.index('location') < calls.index('schema')
    with application.app_context():
        from app.models import db, UserPreference
        assert UserPreference.query.first().current_commit == sha
        db.session.remove()
        db.engine.dispose()


def test_direct_preflight_rejection_precedes_database_mutation(monkeypatch):
    """A prepared update fences a direct web launch before location or schema changes."""
    from app import create_app

    monkeypatch.delenv('TESTING', raising=False)
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '0')
    preflight = Mock(side_effect=RuntimeError('Active update blocks direct startup'))
    location = Mock()
    schema = Mock()
    monkeypatch.setattr('scripts.upgrade.startup_preflight', preflight)
    monkeypatch.setattr('app.db_paths.migrate_db_to_new_location', location)
    monkeypatch.setattr('app.services.database_startup.initialize_database', schema)
    with pytest.raises(RuntimeError, match='blocks direct startup'):
        create_app()
    preflight.assert_called_once()
    location.assert_not_called()
    schema.assert_not_called()


class HealthResponse:
    """Small bounded HTTP response stand-in."""

    status = 200

    def __init__(self, data: dict):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        """Serialize the health response."""
        return json.dumps(self.data).encode('utf-8')


def health_payload() -> dict:
    """Return a healthy current backend and worker identity."""
    return {
        'worker': 'alive', 'boot_id': 'current', 'worker_boot_id': 'current',
        'worker_pid': 42, 'commit_full': 'a' * 40,
        'web_pid': 43,
        'worker_ready': True,
        'worker_heartbeat_at': datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.parametrize('overrides', [
    {'boot_id': 'prior'},
    {'worker_boot_id': 'prior'},
    {'worker_pid': 41},
    {'commit_full': 'b' * 40},
    {'worker_heartbeat_at': (
        datetime.now(timezone.utc) - timedelta(seconds=1000)
    ).isoformat()},
    {'worker_heartbeat_at': 'invalid'},
    {'worker_heartbeat_at': None},
])
def test_supervised_worker_rejects_stale_identity(monkeypatch, overrides):
    """An apparently alive heartbeat cannot authorize the wrong child."""
    data = {**health_payload(), **overrides}
    monkeypatch.setattr(sup.urllib.request, 'urlopen', lambda *a, **k: HealthResponse(data))
    assert sup.worker_health(5151, 'current', 42, 'a' * 40) is False


def test_health_matches_expected_commit_and_boot(monkeypatch):
    """Both strict probes verify current identity and use a finite HTTP timeout."""
    request = Mock(side_effect=lambda *a, **k: HealthResponse(health_payload()))
    monkeypatch.setattr(sup.urllib.request, 'urlopen', request)
    assert sup.web_health(5151, 'current', 'a' * 40) is True
    assert sup.web_health(5151, 'current', 'a' * 40, 43) is True
    assert sup.web_health(5151, 'current', 'a' * 40, 44) is False
    assert sup.worker_health(5151, 'current', 42, 'a' * 40) is True
    assert sup.worker_health(5151, 'current', 42, 'a' * 40, require_ready=True) is True
    assert sup.web_health(5151, 'old', 'a' * 40) is False
    assert all(call.kwargs['timeout'] == sup.HEALTH_TIMEOUT_SECONDS
               for call in request.call_args_list)
    assert all(call.args[0].endswith('/health?startup=1')
               for call in request.call_args_list)


def test_live_worker_requires_explicit_service_readiness_for_final_health(monkeypatch):
    """Preliminary liveness does not imply successful service initialization."""
    data = {**health_payload(), 'worker_ready': False}
    monkeypatch.setattr(sup.urllib.request, 'urlopen', lambda *a, **k: HealthResponse(data))
    assert sup.worker_health(5151, 'current', 42, 'a' * 40) is True
    assert sup.worker_health(5151, 'current', 42, 'a' * 40, require_ready=True) is False


def test_legacy_worker_health_remains_available(monkeypatch):
    """Unsupervised consumers can continue probing the original worker field."""
    monkeypatch.setattr(
        sup.urllib.request, 'urlopen',
        lambda *a, **k: HealthResponse({'worker': 'alive'}),
    )
    assert sup.worker_health(5151) is True


@pytest.mark.parametrize('supervised', ['', '0', 'false'])
def test_direct_worker_does_not_wait(monkeypatch, supervised):
    """Direct worker launches do not depend on any startup-state file."""
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', supervised)
    monkeypatch.delenv('SALESBUDDY_BOOT_ID', raising=False)
    assert worker.wait_for_startup_acceptance(threading.Event(), timeout_seconds=0)


def test_explicit_unmanaged_dev_stack_does_not_wait_for_supervisor(monkeypatch):
    """The supported split Flask debug stack has no supervisor acceptance file."""
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1')
    monkeypatch.setenv('SALESBUDDY_DEV_STACK', '1')
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.delenv('SALESBUDDY_BOOT_ID', raising=False)
    assert worker.wait_for_startup_acceptance(threading.Event(), timeout_seconds=0)


@pytest.mark.parametrize(('environment', 'marker'), [
    ('production', '1'), ('development', '0'), ('development', ''),
])
def test_dev_override_cannot_release_production_or_unmarked_workers(
    monkeypatch, environment, marker,
):
    """A managed worker without a boot identity remains fail closed outside dev opt-in."""
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1')
    monkeypatch.setenv('SALESBUDDY_DEV_STACK', marker)
    monkeypatch.setenv('FLASK_ENV', environment)
    monkeypatch.delenv('SALESBUDDY_BOOT_ID', raising=False)
    with pytest.raises(RuntimeError, match='no boot identity'):
        worker.wait_for_startup_acceptance(threading.Event(), timeout_seconds=0)


def test_dev_marker_cannot_bypass_real_managed_boot(monkeypatch, tmp_path):
    """An actual boot identity always requires activation, even in a marked dev stack."""
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1')
    monkeypatch.setenv('SALESBUDDY_DEV_STACK', '1')
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SALESBUDDY_BOOT_ID', 'current')
    path = tmp_path / 'startup-state.json'
    path.write_text(json.dumps({
        'boot_id': 'current', 'state': 'starting', 'phase': 'starting', 'children': [],
    }), encoding='utf-8')
    monkeypatch.setattr('scripts.upgrade.startup_path', lambda repo: path)
    with pytest.raises(TimeoutError):
        worker.wait_for_startup_acceptance(threading.Event(), timeout_seconds=0)


@pytest.mark.parametrize('state', [
    {'state': 'ready', 'boot_id': 'old', 'children': []},
    {'state': 'starting', 'boot_id': 'current', 'children': []},
    {'state': 'ready', 'boot_id': 'current', 'children': [
        {'name': 'worker', 'pid': -1, 'ready': True},
    ]},
    {'state': 'starting', 'phase': 'activating', 'activation_allowed': True,
     'boot_id': 'old', 'children': [
         {'name': 'worker', 'pid': os.getpid(), 'ready': False},
     ]},
    {'state': 'starting', 'phase': 'activating', 'activation_allowed': True,
     'boot_id': 'current', 'children': [
         {'name': 'worker', 'pid': -1, 'ready': False},
     ]},
    {'state': 'starting', 'phase': 'activating', 'activation_allowed': False,
     'boot_id': 'current', 'children': [
         {'name': 'worker', 'pid': os.getpid(), 'ready': False},
     ]},
])
def test_worker_wait_is_bounded_and_rejects_old_acceptance(monkeypatch, tmp_path, state):
    """Readiness from a prior boot or worker never starts remote schedulers."""
    path = tmp_path / 'startup-state.json'
    path.write_text(json.dumps(state), encoding='utf-8')
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1')
    monkeypatch.setenv('SALESBUDDY_BOOT_ID', 'current')
    monkeypatch.setattr('scripts.upgrade.startup_path', lambda repo: path)
    with pytest.raises(TimeoutError):
        worker.wait_for_startup_acceptance(threading.Event(), timeout_seconds=0)


def test_worker_accepts_only_its_current_activation_record(monkeypatch, tmp_path):
    """Activation admission is tied to the live worker PID, not final readiness."""
    path = tmp_path / 'startup-state.json'
    path.write_text(json.dumps({
        'state': 'starting', 'phase': 'activating', 'activation_allowed': True,
        'boot_id': 'current',
        'children': [{'name': 'worker', 'pid': os.getpid(), 'ready': False}],
    }), encoding='utf-8')
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1')
    monkeypatch.setenv('SALESBUDDY_BOOT_ID', 'current')
    monkeypatch.setattr('scripts.upgrade.startup_path', lambda repo: path)
    assert worker.wait_for_startup_acceptance(threading.Event(), timeout_seconds=0)


def test_worker_rejected_boot_is_terminal(monkeypatch, tmp_path):
    """A preflight or child failure stops the waiting worker immediately."""
    path = tmp_path / 'startup-state.json'
    path.write_text(json.dumps({'state': 'failed', 'boot_id': 'current'}), encoding='utf-8')
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1')
    monkeypatch.setenv('SALESBUDDY_BOOT_ID', 'current')
    monkeypatch.setattr('scripts.upgrade.startup_path', lambda repo: path)
    with pytest.raises(RuntimeError, match='rejected'):
        worker.wait_for_startup_acceptance(threading.Event())


@pytest.mark.parametrize(('supervised', 'dev_stack'), [
    (False, False), (True, False), (True, True),
])
def test_worker_starts_services_only_after_activation(monkeypatch, supervised, dev_stack):
    """Only initialized workers publish ready; direct launches need no admission."""
    calls = []
    app = SimpleNamespace()
    monkeypatch.setattr(
        'app.create_app',
        lambda **kwargs: calls.append(('create', kwargs)) or app,
    )
    monkeypatch.setattr(
        worker, '_start_worker_heartbeat',
        lambda app, stop, ready: calls.append(('heartbeat', ready.is_set())),
    )
    monkeypatch.setattr(
        worker, '_publish_worker_heartbeat',
        lambda app, ready: calls.append(('publish', ready.is_set())),
    )
    monkeypatch.setattr(
        worker, 'wait_for_startup_acceptance', lambda *args: calls.append('accept') or True,
    )
    monkeypatch.setattr('app.start_background_services', lambda *args: calls.append('services'))
    monkeypatch.setattr(
        'app.services.job_queue.run_worker_loop', lambda *args, **kwargs: calls.append('jobs'),
    )
    monkeypatch.setattr(worker.signal, 'signal', lambda *args: None)
    monkeypatch.setenv('SALESBUDDY_ROLE', 'worker')
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1' if supervised else '0')
    monkeypatch.setenv('SALESBUDDY_DEV_STACK', '1' if dev_stack else '0')
    monkeypatch.setenv('FLASK_ENV', 'development' if dev_stack else 'production')
    if dev_stack:
        monkeypatch.delenv('SALESBUDDY_BOOT_ID', raising=False)
    managed = supervised and not dev_stack
    worker.main()
    expected = [('create', {'defer_background_services': True})]
    if managed:
        expected.append(('heartbeat', False))
    expected.extend(['accept', 'services'])
    expected.append(('publish', True) if managed else ('heartbeat', True))
    expected.append('jobs')
    assert calls == expected


def test_worker_service_start_failure_never_publishes_ready_or_runs_jobs(monkeypatch):
    """Synchronous initialization failure cannot produce a successful worker boot."""
    app = SimpleNamespace()
    beats = []
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '1')
    monkeypatch.setenv('SALESBUDDY_ROLE', 'worker')
    monkeypatch.setattr('app.create_app', lambda **kwargs: app)
    monkeypatch.setattr(worker.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(worker, 'wait_for_startup_acceptance', lambda *args: True)
    monkeypatch.setattr(
        worker, '_start_worker_heartbeat',
        lambda app, stop, ready: beats.append(ready.is_set()),
    )
    publish = Mock()
    jobs = Mock()
    monkeypatch.setattr(worker, '_publish_worker_heartbeat', publish)
    monkeypatch.setattr('app.services.job_queue.run_worker_loop', jobs)
    monkeypatch.setattr(
        'app.start_background_services', Mock(side_effect=RuntimeError('Service startup failed')),
    )
    with pytest.raises(RuntimeError, match='Service startup failed'):
        worker.main()
    assert beats == [False]
    publish.assert_not_called()
    jobs.assert_not_called()


def test_testing_config_blocks_all_background_services(monkeypatch):
    """Testing continues to suppress all background service side effects."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    monkeypatch.delenv('TESTING', raising=False)
    start = Mock()
    monkeypatch.setattr('app.services.msx_auth.start_token_refresh_job', start)
    start_background_services(app)
    start.assert_not_called()
    assert 'salesbuddy_background_services' not in app.extensions


@pytest.mark.parametrize(('role', 'supervised', 'heavy'), [
    ('worker', '1', True),
    ('worker', '0', True),
    ('web', '1', False),
    ('web', '0', True),
])
def test_extracted_services_keep_role_gates_and_start_once(
    monkeypatch, role, supervised, heavy,
):
    """Extraction retains each scheduler, including specialty-aware sync owners."""
    monkeypatch.delenv('TESTING', raising=False)
    monkeypatch.setenv('SALESBUDDY_ROLE', role)
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', supervised)
    paths = [
        'app.services.msx_auth.start_token_refresh_job',
        'app.services.update_checker.start_update_checker',
        'app.services.telemetry_shipper.start_flush_thread',
        'app.services.copilot_actions.start_copilot_sync_background',
        'app.services.copilot_actions.start_daily_scheduler',
        'app.services.scheduled_sync.start_milestone_sync_background',
        'app.services.scheduled_sync.start_daily_milestone_scheduler',
        'app.services.scheduled_sync.start_fabric_sync_scheduler',
        'app.services.scheduled_sync.start_sql_motions_sync_scheduler',
        'app.services.meeting_sync.start_meeting_sync_background',
        'app.services.meeting_sync.start_daily_meeting_scheduler',
        'app.services.lifecycle.init_lifecycle_logging',
    ]
    mocks = {}
    for path in paths:
        mocks[path] = Mock()
        monkeypatch.setattr(path, mocks[path])
    app = Flask(__name__)
    app.config['DEBUG'] = False
    start_background_services(app)
    start_background_services(app)
    for path, mock in mocks.items():
        expected = heavy if path in paths[3:-1] else True
        assert mock.call_count == int(expected), path


def test_reloader_parent_does_not_start_background_services(monkeypatch):
    """Debug-mode web parents stay idle regardless of the ambient dev environment."""
    monkeypatch.delenv('TESTING', raising=False)
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setenv('SALESBUDDY_ROLE', 'web')
    monkeypatch.setenv('SALESBUDDY_SUPERVISED', '0')
    app = Flask(__name__)
    app.config['DEBUG'] = True
    start = Mock()
    monkeypatch.setattr('app.services.msx_auth.start_token_refresh_job', start)
    start_background_services(app)
    start.assert_not_called()
    assert 'salesbuddy_background_services' not in app.extensions


def test_health_endpoint_preserves_commit_and_reports_worker_identity(client, app, monkeypatch):
    """Legacy health consumers keep their fields while strict probes get identity."""
    monkeypatch.setitem(app.config, 'BOOT_COMMIT', 'shortsha')
    monkeypatch.setitem(app.config, 'BOOT_COMMIT_FULL', 'a' * 40)
    monkeypatch.setitem(app.config, 'BOOT_ID', 'current')
    heartbeat = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        'app.services.sync_runtime.worker_state',
        lambda: {'boot_id': 'current', 'pid': 42, 'heartbeat_at': heartbeat, 'ready': True},
    )
    monkeypatch.setattr(
        'app.services.sync_jobs.worker_health',
        lambda: {'alive': True, 'heartbeat_at': heartbeat},
    )
    response = client.get('/health?startup=1')
    assert response.status_code == 200
    data = response.get_json()
    assert data['commit'] == 'shortsha'
    assert data['commit_full'] == 'a' * 40
    assert data['worker'] == 'alive'
    assert data['boot_id'] == data['worker_boot_id'] == 'current'
    assert data['worker_pid'] == 42
    assert data['web_pid'] == os.getpid()
    assert data['worker_heartbeat_at'] == heartbeat
    assert data['worker_ready'] is True


@pytest.fixture
def managed_health(monkeypatch, app, tmp_path):
    """Supply isolated durable state and heartbeat for managed health acceptance."""
    monkeypatch.setitem(app.config, 'BOOT_ID', 'current')
    monkeypatch.setitem(app.config, 'BOOT_COMMIT_FULL', 'a' * 40)
    now = datetime.now(timezone.utc)
    beat = {
        'boot_id': 'current', 'pid': 42, 'ready': True,
        'heartbeat_at': (now - timedelta(seconds=5)).isoformat(),
    }
    state = {
        'schema_version': 1, 'state': 'ready', 'phase': 'ready',
        'activation_allowed': True, 'boot_id': 'current', 'expected_commit': 'a' * 40,
        'started_at': (now - timedelta(seconds=60)).isoformat(),
        'updated_at': now.isoformat(),
        'children': [
            {'name': 'web', 'pid': os.getpid(), 'attempts': 1, 'ready': True},
            {'name': 'worker', 'pid': 42, 'attempts': 1, 'ready': True},
        ],
    }
    path = tmp_path / 'startup-state.json'
    monkeypatch.setattr('scripts.upgrade.startup_path', lambda repo: path)
    monkeypatch.setattr('app.services.sync_runtime.worker_state', lambda: beat)
    monkeypatch.setattr(
        'app.services.sync_jobs.worker_health',
        lambda: {'alive': True, 'heartbeat_at': beat['heartbeat_at']},
    )
    return state, beat, path


def test_default_managed_health_accepts_only_final_matching_state(client, managed_health):
    """Old HTTP-200 update pollers cannot accept the intermediate activation phase."""
    state, beat, path = managed_health
    state.update(state='starting', phase='activating')
    path.write_text(json.dumps(state), encoding='utf-8')
    response = client.get('/health')
    assert response.status_code == 503
    assert response.get_json()['database'] == 'connected'
    assert response.get_json()['worker_ready'] is True
    assert client.get('/health?startup=1').status_code == 200
    state.update(state='ready', phase='ready')
    path.write_text(json.dumps(state), encoding='utf-8')
    assert client.get('/health').status_code == 200


def test_managed_health_waits_for_initial_durable_state(client, managed_health):
    """A missing startup-state file never means accepted startup."""
    assert client.get('/health').status_code == 503
    assert client.get('/health?startup=1').status_code == 200


@pytest.mark.parametrize(('field', 'value'), [
    ('boot_id', 'previous'),
    ('expected_commit', 'b' * 40),
    ('updated_at', (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()),
    ('schema_version', 2),
    ('children', [
        {'name': 'web', 'pid': -1, 'ready': True},
        {'name': 'worker', 'pid': 42, 'ready': True},
    ]),
    ('children', [
        {'name': 'web', 'pid': os.getpid(), 'ready': True},
        {'name': 'worker', 'pid': 43, 'ready': True},
    ]),
    ('children', [
        {'name': 'web', 'pid': os.getpid(), 'ready': True},
        {'name': 'worker', 'pid': 42, 'ready': False},
    ]),
])
def test_default_managed_health_rejects_stale_or_wrong_status(
    client, managed_health, field, value,
):
    """A ready label cannot hide an old boot, commit, child PID or stale status."""
    state, beat, path = managed_health
    state[field] = value
    path.write_text(json.dumps(state), encoding='utf-8')
    assert client.get('/health').status_code == 503


@pytest.mark.parametrize(('field', 'value'), [
    ('ready', False),
    ('boot_id', 'previous'),
    ('pid', 43),
    ('heartbeat_at', (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()),
])
def test_default_managed_health_requires_current_initialized_worker(
    client, managed_health, field, value,
):
    """Final durable state alone cannot accept an uninitialized or stale worker."""
    state, beat, path = managed_health
    beat[field] = value
    path.write_text(json.dumps(state), encoding='utf-8')
    assert client.get('/health').status_code == 503


@pytest.mark.parametrize('contents', ['{invalid json', '{}', '[]'])
def test_malformed_startup_state_logs_error_and_keeps_diagnostic_payload(
    client, managed_health, caplog, contents,
):
    """Unreadable state fails closed instead of masquerading as a DB outage."""
    state, beat, path = managed_health
    path.write_text(contents, encoding='utf-8')
    with caplog.at_level('ERROR', logger='app.routes.main'):
        response = client.get('/health')
    assert response.status_code == 503
    assert response.get_json()['database'] == 'connected'
    assert response.get_json()['boot_id'] == 'current'
    assert 'Startup readiness state could not be read' in caplog.text
    assert client.get('/health?startup=1').status_code == 200


def test_direct_health_without_boot_id_does_not_read_managed_status(
    client, managed_health, app, monkeypatch,
):
    """Direct and ordinary testing launches retain their original health behavior."""
    state, beat, path = managed_health
    monkeypatch.setitem(app.config, 'BOOT_ID', None)
    path.write_text('{invalid json', encoding='utf-8')
    assert client.get('/health').status_code == 200


def test_worker_heartbeat_carries_boot_identity(monkeypatch, tmp_path):
    """The file read by health is stamped with this worker's current boot."""
    from app.services import sync_runtime

    monkeypatch.setenv('SALESBUDDY_BOOT_ID', 'current')
    monkeypatch.setattr(sync_runtime, 'state_directory', lambda: tmp_path)
    sync_runtime.worker_heartbeat(ready=True)
    state = sync_runtime.worker_state()
    assert state['boot_id'] == 'current'
    assert state['pid'] == os.getpid()
    assert state['ready'] is True
    assert datetime.fromisoformat(state['heartbeat_at']).tzinfo is not None
