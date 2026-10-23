"""Widget readiness, source admission, empty imports, and prerequisite ordering."""
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import Mock

from bs4 import BeautifulSoup
import pytest

from app.models import (
    Customer, Dashboard, Job, PToFImport, SyncStatus, UserPreference, db,
)
from app.services import dashboard_sync, dashboards
from tests.test_dashboards import BASE, add_widget, create_dashboard


def complete(operation: str, **result) -> Job:
    """Persist a successful source run, including a legitimate empty result."""
    job = Job(
        job_type=f'sync:{operation}', status=Job.STATUS_DONE, payload='{}',
        result=json.dumps({'success': True, **result}),
        finished_at=datetime.now(timezone.utc),
    )
    db.session.add(job)
    db.session.commit()
    return job


def start(client, key: str, *, retry: bool = False):
    """Request the widget's next dependency through its explicit POST."""
    return client.post(f'{BASE}/widgets/{key}/sync', json={'retry': retry})


def preview(client, key: str):
    """Render the shared saved/editor/library content fragment."""
    return client.get(f'{BASE}/widget-preview', query_string={
        'key': key, 'variant': 'metric', 'width': 6,
    })


def test_every_widget_source_uses_a_registered_central_sync_operation():
    """Widget dependencies cannot introduce imports outside the durable sync registry."""
    from app.services import job_queue
    from app.services.sync_jobs import OPERATIONS, register_handlers

    register_handlers()
    for sources in dashboard_sync.WIDGET_SOURCES.values():
        for source in sources:
            operation = dashboard_sync.SOURCE_OPERATIONS.get(source, source)
            assert operation in OPERATIONS
            assert callable(job_queue.get_handler(f'sync:{operation}'))


def test_fresh_install_loads_choices_before_prompting_without_get_writes(client, app):
    """An empty picker does not deadlock setup or start syncs during rendering."""
    page = preview(client, 'connect_goals.u2c')
    assert page.status_code == 200
    soup = BeautifulSoup(page.json['html'], 'html.parser')
    assert soup.select_one('[data-widget-sync]')
    assert soup.select_one('[data-widget-requirement]') is None
    with app.app_context():
        assert Job.query.count() == 0
    result = start(client, 'connect_goals.u2c')
    assert result.status_code == 200
    assert result.json['sync']['operation'] == 'accounts'
    with app.app_context():
        assert Job.query.one().initiator == 'dashboard'


@pytest.mark.parametrize('status', [None, Job.STATUS_PENDING, Job.STATUS_RUNNING, Job.STATUS_FAILED])
@pytest.mark.parametrize('group_by_seller', [False, True])
def test_sync_actions_only_show_when_the_widget_needs_help(client, status, group_by_seller):
    """Preparing, queued and running widgets need no extra click to follow their sync."""
    db.session.add(Customer(name='Widget account', tpid=12345))
    if status is not None:
        db.session.add(Job(
            job_type='sync:fabric', status=status, payload='{}',
            last_error='VPN unavailable' if status == Job.STATUS_FAILED else None,
        ))
    db.session.commit()
    response = client.get(f'{BASE}/widget-preview', query_string={
        'key': 'fabric.expansion', 'variant': 'metric', 'width': 6,
        'filter_context': json.dumps({
            'dashboard': {'live_filters': ['group_by_seller']},
            'live': {'group_by_seller': group_by_seller},
        }),
    })
    assert response.status_code == 200
    soup = BeautifulSoup(response.json['html'], 'html.parser')
    actions = soup.select_one('[data-widget-sync-actions]')
    failed = status == Job.STATUS_FAILED
    assert actions.has_attr('hidden') is not failed
    assert actions.select_one('[data-widget-sync-retry]').has_attr('hidden') is not failed
    assert actions.find('a', string='Sync Status')['href'] == '/admin/sync-console'
    assert soup.select_one('[data-sync-message]')['role'] == 'status'
    assert 'This widget will update automatically when its data is ready.' in soup.get_text()


def test_input_precedes_remaining_syncs_and_provider(client, monkeypatch):
    """Once choices exist, no widget source is admitted before confirmation."""
    setup = {'ready': False, 'available_buckets': ['Fabric'], 'fiscal_year': 'FY27'}
    monkeypatch.setitem(dashboards.WIDGET_REQUIREMENTS, 'compensation_buckets', lambda _: setup)
    spec = dashboards.WIDGET_DEFINITIONS['connect_goals.u2c']
    provider = Mock(side_effect=AssertionError('Not ready'))
    monkeypatch.setitem(dashboards.WIDGET_DEFINITIONS, spec.key, replace(spec, provider=provider))
    html = preview(client, spec.key).json['html']
    assert 'data-widget-requirement=' in html
    assert 'data-widget-sync=' not in html
    result = start(client, spec.key).json
    assert result['requirements'] and result['sync'] is None
    provider.assert_not_called()
    setup['ready'] = True
    assert 'data-widget-sync=' in preview(client, spec.key).json['html']
    assert start(client, spec.key).json['sync']['operation'] == 'accounts'


def test_shared_source_joins_active_run_with_different_options(client, app):
    """Widget admission follows an existing full source import, not a second copy."""
    with app.app_context():
        db.session.add(Customer(name='Customer', tpid=12345))
        job = dashboard_sync.admit_sync('revenue', {'run_analysis': False})
        job_id = job.id
    a = start(client, 'fabric.modernization').json
    b = start(client, 'connect_goals.whitespace').json
    assert a['sync']['job']['id'] == b['sync']['job']['id'] == job_id
    with app.app_context():
        assert Job.query.count() == 1


def test_rti_waits_for_revenue_and_fabric_then_uses_successful_empty_imports(client, app):
    """The two-source RTI widget participates in the normal sequential sync flow."""
    with app.app_context():
        db.session.add(Customer(name='RTI customer', tpid=12345))
        db.session.commit()
    assert start(client, 'fabric.rti').json['sync']['operation'] == 'revenue'
    with app.app_context():
        job = Job.query.filter_by(job_type='sync:revenue').one()
        job.status = Job.STATUS_DONE
        job.result = '{"success": true}'
        job.finished_at = datetime.now(timezone.utc)
        db.session.commit()
    assert start(client, 'fabric.rti').json['sync']['operation'] == 'fabric'
    with app.app_context():
        job = Job.query.filter_by(job_type='sync:fabric').one()
        job.status = Job.STATUS_DONE
        job.result = '{"success": true}'
        job.finished_at = datetime.now(timezone.utc)
        db.session.commit()
    assert start(client, 'fabric.rti').json['ready']
    assert 'data-widget-sync=' not in preview(client, 'fabric.rti').json['html']
    with app.app_context():
        assert Job.query.count() == 2


def test_accounts_then_source_then_empty_data_without_resync(client, app):
    """Account import is a real prerequisite; a successful empty source stays ready."""
    key = 'fabric.trial_conversion'
    first = start(client, key).json['sync']
    assert first['operation'] == 'accounts'
    with app.app_context():
        account = db.session.get(Job, first['job']['id'])
        account.status = Job.STATUS_DONE
        account.result = '{"success": true}'
        account.finished_at = datetime.now(timezone.utc)
        db.session.add(Customer(name='Imported account', tpid=12345))
        db.session.commit()
    second = start(client, key).json['sync']
    assert second['operation'] == 'fabric'
    with app.app_context():
        job = db.session.get(Job, second['job']['id'])
        job.status = Job.STATUS_DONE
        job.result = '{"success": true, "items_synced": 0}'
        job.finished_at = datetime.now(timezone.utc)
        db.session.commit()
    assert start(client, key).json['ready']
    assert 'data-widget-sync=' not in preview(client, key).json['html']
    with app.app_context():
        assert Job.query.count() == 2


def test_no_eligible_accounts_is_explicit_not_an_endless_import(client, app):
    """A valid empty account sync produces an actionable blocked source."""
    with app.app_context():
        complete('accounts', items_synced=0)
    result = start(client, 'fabric.expansion').json
    assert 'No eligible accounts' in result['sync']['error']
    with app.app_context():
        assert Job.query.count() == 1


def test_failed_jobs_require_explicit_retry(client, app):
    """Reloads preserve errors; the Retry button can admit one fresh attempt."""
    with app.app_context():
        db.session.add(Customer(name='Imported account', tpid=12345))
        db.session.add(Job(
            job_type='sync:fabric', status=Job.STATUS_FAILED, payload='{}',
            last_error='VPN unavailable',
        ))
        db.session.commit()
    for _ in range(2):
        result = start(client, 'fabric.expansion').json
        assert result['sync']['error'] == 'VPN unavailable'
    retry = start(client, 'fabric.expansion', retry=True).json
    assert retry['sync']['job']['status'] == Job.STATUS_PENDING
    assert retry['sync']['error'] is None
    with app.app_context():
        assert Job.query.count() == 2


def test_successful_manual_snapshot_never_resyncs_empty_results(client, app):
    """An imported snapshot is readiness evidence even with no renewal rows."""
    with app.app_context():
        db.session.add(PToFImport(customer_count=0, summary_count=0, detail_count=0))
        db.session.commit()
    assert start(client, 'ptof.upcoming_renewals').json['ready']
    with app.app_context():
        assert Job.query.count() == 0


def test_fabric_revenue_or_telemetry_does_not_satisfy_renewals(app):
    """Partial Fabric operations are not evidence of a full P-to-F import."""
    with app.app_context():
        db.session.add(Customer(name='Customer', tpid=12345))
        complete('fabric-revenue')
        db.session.add(SyncStatus(
            sync_type='fabric_consumption', success=True,
            completed_at=datetime.now(timezone.utc),
        ))
        db.session.commit()
        assert dashboard_sync.get_source_sync(('ptof',))['operation'] == 'fabric'


def test_calendar_day_import_does_not_satisfy_fiscal_calendar(app):
    """Today's meetings cannot make FY-to-date meeting coverage complete."""
    with app.app_context():
        complete('meetings')
        assert dashboard_sync.get_source_sync(
            ('calendar-population',), date(2026, 10, 20),
        )['operation'] == 'calendar-population'


def test_quarter_rollover_does_not_reuse_old_u2c_completion(app):
    """U2C is quarter-specific even when an old import completed successfully."""
    with app.app_context():
        job = complete('u2c')
        job.finished_at = datetime(2026, 9, 30, tzinfo=timezone.utc)
        db.session.commit()
        state = dashboard_sync.get_source_sync(('u2c',), date(2026, 10, 20))
        assert state['operation'] == 'u2c'
        assert state['error']
        job.finished_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
        db.session.commit()
        assert dashboard_sync.get_source_sync(('u2c',), date(2026, 10, 20)) is None


def test_skipped_import_is_not_readiness_evidence(app):
    """A no-op is not a successful empty source import."""
    with app.app_context():
        complete('fabric', skipped=True)
        state = dashboard_sync.get_source_sync(('fabric',))
        assert state
        assert state['operation'] == 'accounts'


def test_shared_missing_source_is_read_once_per_dashboard(app, monkeypatch):
    """Multiple widgets sharing a source use one request-local readiness check."""
    with app.app_context():
        dashboard = Dashboard(name='Same source')
        db.session.add(dashboard)
        for key in ('fabric.expansion', 'fabric.trial_conversion', 'fabric.expansion'):
            dashboards.add_dashboard_widget(dashboard, key)
        db.session.commit()
        provider = Mock(return_value={'operation': 'fabric'})
        monkeypatch.setattr(dashboard_sync, 'get_source_sync', provider)
        result = dashboards.get_dashboard_data(dashboard)
        assert len(result['widgets']) == 3
        provider.assert_called_once_with(('fabric',), None)


def test_local_initiatives_do_not_trigger_external_work(client, app):
    """Locally authored widgets do not require an unrelated import."""
    assert start(client, 'initiative_tracker.overview').json['ready']
    with app.app_context():
        assert Job.query.count() == 0


@pytest.mark.parametrize('payload', [None, [], {'retry': 'yes'}, {'operation': 'accounts'}])
def test_invalid_admission_is_rejected(client, payload):
    """Only a typed retry flag is accepted; callers cannot select arbitrary work."""
    response = client.post(f'{BASE}/widgets/fabric.expansion/sync', json=payload)
    assert response.status_code == 400


def test_specialty_access_and_unknown_widgets_cannot_start_jobs(client, app):
    """Source admission enforces the same specialty eligibility as the catalog."""
    assert start(client, 'not-a-widget').status_code == 404
    with app.app_context():
        pref = UserPreference.query.first()
        pref.user_role = 'se'
        pref.se_specialty = 'apps'
        db.session.commit()
    assert start(client, 'fabric.acquisition').status_code == 403
    with app.app_context():
        assert Job.query.count() == 0
        UserPreference.query.first().user_role = 'dss'
        db.session.commit()
    assert start(client, 'fabric.acquisition').status_code == 200


def test_source_refresh_preserves_saved_dashboard_geometry(client, app):
    """Readiness transitions never publish or mutate a dashboard arrangement."""
    dashboard_id = create_dashboard(client)
    add_widget(client, dashboard_id, 'fabric.expansion')
    before = client.get(f'{BASE}/{dashboard_id}').json['dashboard']
    with app.app_context():
        complete('fabric')
    after = client.get(f'{BASE}/{dashboard_id}').json['dashboard']
    assert before['widgets'][0]['sync']
    assert after['widgets'][0]['sync'] is None
    assert after['widgets'][0]['data']
    for field in ('layout_revision', 'updated_at'):
        assert before[field] == after[field]
    for field in ('id', 'layout_column', 'layout_row', 'column_span', 'row_span', 'filters'):
        assert before['widgets'][0][field] == after['widgets'][0][field]
    with app.app_context():
        assert db.session.get(Dashboard, dashboard_id).layout_revision == before['layout_revision']
