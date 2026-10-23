"""Isolated acceptance tests for one durable sync admission/execution/observation path."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import requests
from bs4 import BeautifulSoup

from app.models import Customer, Job, JobEvent, SyncStatus, Territory, UserPreference, db
from app.services import job_queue, sync_jobs


@pytest.mark.parametrize('initiator', ['manual', 'scheduled', 'followup'])
def test_meeting_aura_requires_customers_before_admission(app, monkeypatch, initiator):
    """Every launcher rejects a missing account prerequisite before creating a failed job."""
    from types import SimpleNamespace

    with app.app_context():
        monkeypatch.setattr(Customer, 'query', SimpleNamespace(first=lambda: None))
        before = Job.query.count()
        with pytest.raises(sync_jobs.SyncAdmissionError, match='Import accounts first'):
            sync_jobs.admit_sync('meeting-aura', initiator=initiator)
        assert Job.query.count() == before


@pytest.mark.parametrize('initiator', ['manual', 'scheduled', 'followup'])
@pytest.mark.parametrize('territory_name', [None, '   '])
def test_u2c_requires_valid_territories_before_admission(initiator, territory_name):
    """No launcher creates a failed U2C job while territory setup is incomplete."""
    if territory_name is not None:
        db.session.add(Territory(name=territory_name))
        db.session.commit()
    with pytest.raises(sync_jobs.SyncAdmissionError, match='No territories configured'):
        sync_jobs.admit_sync('u2c', initiator=initiator)
    assert Job.query.count() == 0
    assert SyncStatus.query.filter_by(sync_type='u2c_import').first() is None


def test_u2c_rechecks_territories_in_the_worker(executor):
    """Queued work cannot run against a scope removed after admission."""
    territory = Territory(name='East.SMECC.MAA.0101')
    db.session.add(territory)
    db.session.commit()
    job = sync_jobs.admit_sync('u2c')
    db.session.delete(territory)
    db.session.commit()
    job_queue.process_one('test-worker')
    db.session.refresh(job)
    assert job.status == 'failed'
    assert 'No territories configured' in job.last_error
    assert executor == []


@pytest.fixture
def executor(monkeypatch):
    """Replace remote work, not admission or persistence, with measurable test execution."""
    seen = []

    def run(operation, options):
        seen.append((operation, options))
        sync_jobs.publish_progress({
            'phase': 'fetching', 'current': 1, 'total': 2,
            'progress': 50, 'message': 'One of two test units stored.',
        })
        return {'success': True, 'items_synced': 2}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    return seen


def test_catalog_is_read_only_and_includes_all_regular_syncs(client):
    """Opening Sync Status cannot start a sync or fabricate a worker."""
    response = client.get('/api/jobs')
    assert response.status_code == 200
    data = response.get_json()
    assert len(data['operations']) == len(sync_jobs.OPERATIONS)
    assert {row['operation'] for row in data['operations']} == set(sync_jobs.OPERATIONS)
    assert data['worker']['alive'] is False
    assert Job.query.count() == 0
    assert client.get('/admin/sync-console').status_code == 200


def test_sync_status_name_matches_the_heading_browser_title_and_breadcrumb(client):
    """Use the app-facing name consistently across the page's navigation surfaces."""
    soup = BeautifulSoup(client.get('/admin/sync-console').data, 'html.parser')
    assert soup.title.get_text() == 'Sync Status - Sales Buddy'
    assert soup.h1.get_text() == 'Sync Status'
    assert soup.select_one('.sb-context-bar [aria-current="page"] > span[title]')[
        'title'] == 'Sync Status'


def test_sync_pages_update_automatically_without_manual_status_buttons(client):
    """The regular progress display and overview do not need redundant refresh controls."""
    tracker = BeautifulSoup(client.get('/reports/milestone-tracker').data, 'html.parser')
    assert not tracker.select_one('[data-sb-sync-progress="milestones"]').select('button')
    overview = BeautifulSoup(client.get('/admin/sync-console').data, 'html.parser')
    assert not overview.select_one('.sb-sync-header').select('button')


def test_operation_discovery_is_read_only_and_returns_null_before_first_run(client):
    """Opening a sync page never admits work just to discover its current status."""
    assert client.get('/api/jobs/operations/milestones').json == {
        'success': True, 'job': None,
    }
    assert Job.query.count() == 0
    assert client.get('/api/jobs/operations/not-a-sync').status_code == 404
    assert Job.query.count() == 0


def test_operation_discovery_reattaches_to_the_same_active_run_on_refresh(client):
    """Repeated page arrivals select the existing run without creating another."""
    db.session.add(Customer(name='Discovery customer', tpid=12345))
    db.session.commit()
    job = sync_jobs.admit_sync('milestones')
    job.status = Job.STATUS_RUNNING
    job.heartbeat_at = job_queue._now()
    job.progress_json = json.dumps({'percent': 37, 'message': 'Fetching tasks.'})
    db.session.commit()
    count = Job.query.count()
    for _ in range(3):
        snapshot = client.get('/api/jobs/operations/milestones').json['job']
        assert snapshot['id'] == job.id
        assert snapshot['status'] == 'running'
        assert snapshot['progress']['percent'] == 37
        assert Job.query.count() == count


def test_console_groups_every_operation_once_in_workflow_order(client):
    """Core dependencies lead the list, followed by reports and targeted refreshes."""
    soup = BeautifulSoup(client.get('/admin/sync-console').data, 'html.parser')
    groups = soup.select('.sb-sync-group')
    assert [group.h2.get_text() for group in groups] == [
        'Core app syncs', 'Report syncs', 'Specific use case syncs',
    ]
    keys = [row['data-sync-operation'] for row in soup.select('[data-sync-operation]')]
    assert keys[:3] == ['accounts', 'milestones', 'revenue']
    assert keys[keys.index('meeting-aura') + 1] == 'meetings'
    assert groups[0].select_one('[data-sync-operation="meetings"]') is not None
    assert len(keys) == len(set(keys)) == len(sync_jobs.OPERATIONS)
    assert set(keys) == set(sync_jobs.OPERATIONS)
    assert [row.h3.get_text(' ', strip=True) for row in groups[0].select('article')][:3] == [
        'Account Sync', 'Milestone Sync (Milestone Tracker)', 'Revenue Sync (Revenue Analyzer)',
    ]
    assert [row['data-sync-operation'] for row in groups[1].select('article')][:4] == [
        'u2c', 'fabric', 'sql-motions', 'marketing',
    ]
    assert groups[1].select_one('[data-sync-operation="meeting-preparation"]') is not None
    assert groups[0].select_one('[data-sync-operation="meeting-preparation"]') is None
    meeting = groups[0].select_one('[data-sync-operation="meeting-aura"]')
    assert meeting.h3.get_text(' ', strip=True) == 'Meeting sync (Home)'
    assert meeting.select_one('.sb-sync-identity p').get_text() == (
        'Pulls meeting details for today and the next four business days.')
    assert groups[2].select_one('article')['data-sync-operation'] == 'accounts-selected'
    option_ids = [field['id'] for field in soup.select('[data-sync-options]')]
    assert len(option_ids) == len(set(option_ids)) == sum(
        bool(operation.defaults) for operation in sync_jobs.OPERATIONS.values())
    for label in soup.select('.sb-sync-row label'):
        assert soup.find(id=label['for']) is not None


def test_report_sync_titles_link_to_their_reports(client):
    """Each report sync links directly to the report it supplies."""
    soup = BeautifulSoup(client.get('/admin/sync-console').data, 'html.parser')
    expected = {
        'u2c': '/reports/u2c',
        'fabric': '/reports/fabric-consumption',
        'sql-motions': '/reports/sql-renewals',
        'marketing': '/reports/marketing-insights',
        'activity-reconciliation': '/reports/activity-coverage',
        'meeting-preparation': '/reports/activity-coverage',
        'milestone-dates': '/reports/one-on-one',
    }
    groups = soup.select('.sb-sync-group')
    for row in groups[1].select('[data-sync-operation]'):
        link = row.select_one('.sb-sync-identity h3 a')
        assert link['href'] == expected[row['data-sync-operation']]
        assert link.get_text(strip=True) == sync_jobs.OPERATIONS[
            row['data-sync-operation']].name
    for group in (groups[0], groups[2]):
        assert not group.select('.sb-sync-identity h3 a:not(.sb-sync-related-page)')


def test_core_syncs_have_separate_parenthetical_page_links(client):
    """Core sync titles remain plain text beside normal-weight related page links."""
    from flask import url_for

    soup = BeautifulSoup(client.get('/admin/sync-console').data, 'html.parser')
    for key, endpoint, label in (
        ('milestones', 'milestones.milestone_tracker', 'Milestone Tracker'),
        ('revenue', 'revenue.revenue_dashboard', 'Revenue Analyzer'),
        ('meeting-aura', 'main.index', 'Home'),
    ):
        heading = soup.select_one(f'[data-sync-operation="{key}"] h3')
        link = heading.select_one('a.sb-sync-related-page')
        assert link.get_text() == f'({label})'
        assert link['href'] == url_for(endpoint)
        assert len(heading.select('a')) == 1
    assert len(soup.select('.sb-sync-related-page')) == 3


def test_console_explains_every_supported_option(client):
    """Every editable field has help, defaults and a programmatic description."""
    from app.routes.jobs import CONSOLE_OPTION_HELP

    soup = BeautifulSoup(client.get('/admin/sync-console').data, 'html.parser')
    assert set(CONSOLE_OPTION_HELP) == {
        key for key, operation in sync_jobs.OPERATIONS.items() if operation.defaults
    }
    for key, operation in sync_jobs.OPERATIONS.items():
        row = soup.select_one(f'[data-sync-operation="{key}"]')
        editor = row.select_one('textarea')
        if operation.defaults:
            assert editor is not None
            help_text = row.find(id=editor['aria-describedby'])
            assert help_text is not None
            assert json.loads(editor.get_text()) == operation.defaults
            assert '\n  "' in editor.get_text()
            assert int(editor['rows']) >= len(editor.get_text().splitlines())
            layout = row.select_one('.sb-sync-options-layout')
            assert layout.select_one('.sb-sync-options-editor textarea') == editor
            assert layout.select_one('.sb-sync-option-help') == help_text
            assert set(CONSOLE_OPTION_HELP[key]) == set(operation.defaults)
            assert [term.code.get_text() for term in help_text.select('dt')] == list(
                operation.defaults)
            for term, (option, default) in zip(
                    help_text.select('dt'), operation.defaults.items()):
                assert f'Default: {json.dumps(default)}' in term.get_text()
                assert term.find_next_sibling('dd').get_text() == CONSOLE_OPTION_HELP[key][option]
        else:
            assert editor is None
            assert row.select_one('details') is None
    force_help = CONSOLE_OPTION_HELP['meeting-preparation']['force']
    assert 'clearing existing milestone selections' in force_help


def test_console_has_aligned_schedule_status_and_action_columns(client):
    """Compact rows keep status, success, diagnostics and progress in distinct cells."""
    soup = BeautifulSoup(client.get('/admin/sync-console').data, 'html.parser')
    assert soup.select_one('.sb-sync-columns').get_text(' ', strip=True) == (
        'Sync Schedule Last status Last success Start sync')
    toggle = soup.select_one('[data-sync-show-options]')
    assert toggle['type'] == 'checkbox' and not toggle.has_attr('checked')
    assert all(options.has_attr('hidden') for options in soup.select('.sb-sync-options'))
    for row in soup.select('[data-sync-operation]'):
        assert row.select_one('[data-sync-schedule]').get_text(strip=True)
        assert row.select_one('.sb-sync-state [data-sync-state]')
        assert row.select_one('.sb-sync-success[data-sync-success]')
        schedule_inspect = row.select_one('.sb-sync-schedule [data-sync-inspect]')
        assert schedule_inspect['tabindex'] == '0'
        assert schedule_inspect.find(id=schedule_inspect['aria-describedby'])
        triggers = schedule_inspect.select_one('[data-sync-schedule-triggers]').get_text(strip=True)
        assert schedule_inspect.has_attr('hidden') == (not bool(triggers))
        assert 'Start sync in this console' not in triggers
        assert 'Manual' not in triggers
        inspect = row.select_one('.sb-sync-state [data-sync-inspect]')
        assert inspect.parent == row.select_one('.sb-sync-state')
        icon = inspect.select_one('svg.sb-sync-inspect-label')
        assert icon['viewbox'] == '0 0 16 16'
        assert icon['aria-hidden'] == 'true'
        assert inspect['tabindex'] == '0'
        assert inspect.find(id=inspect['aria-describedby'])
        assert inspect.select_one('[data-sync-run]')
        assert inspect.select_one('[data-sync-trigger]')
        assert inspect.select_one('[data-sync-timing]')
        assert inspect.select_one('[data-sync-details]')
        assert row.select_one('.sb-sync-actions [data-sync-start]')
        progress = row.select_one('[data-sync-live-progress]')
        assert progress.parent == row
        assert progress.select_one('[data-sync-progress]')
        heading = progress.select_one('.sb-sync-progress-heading')
        assert heading.select_one('[data-sync-progress-label]')
        assert heading.select_one('[data-sync-progress-message]')
        assert heading.find_next_sibling('progress').has_attr('data-sync-progress')
        assert [button.get_text() for button in row.select('.sb-sync-actions button')] == [
            'Start sync',
        ]


def test_schedules_reflect_real_cadences_without_assigning_a_time(client):
    """Status reads describe recurring and chained triggers without changing settings."""
    preference = UserPreference.query.first()
    assert preference.milestone_sync_hour is None
    catalog = client.get('/api/jobs').json
    schedules = {row['operation']: row['schedule'] for row in catalog['operations']}
    assert schedules['milestones']['label'] == 'Mon, Wed, Fri'
    assert schedules['meeting-aura']['label'] == 'Daily · 7:00 AM local'
    assert schedules['copilot-actions']['label'] == 'Daily · 6:00 AM local'
    assert schedules['revenue']['label'] == 'Every 7 days'
    assert schedules['u2c']['label'] == 'Every 20 hours'
    assert schedules['marketing']['label'] == 'After milestone sync'
    assert 'after Account Sync finishes successfully' in schedules['meeting-aura']['detail']
    assert any('Activity Coverage meeting preparation' in trigger
               for trigger in schedules['accounts']['triggers'])
    assert any('Activity Coverage meeting preparation' in trigger
               for trigger in schedules['milestones']['triggers'])
    assert any('TPID' in trigger for trigger in schedules['customer-data']['triggers'])
    assert any('After Account Sync finishes successfully' in trigger
               for trigger in schedules['meeting-aura']['triggers'])
    assert {key for key, schedule in schedules.items() if schedule['triggers']} == {
        'accounts', 'milestones', 'customer-data', 'meeting-aura', 'meetings',
    }
    assert schedules['meetings']['label'] == 'Startup if needed'
    assert schedules['favicons']['label'] == 'On demand'
    assert preference.milestone_sync_hour is None
    assert preference.milestone_sync_minute is None
    assert Job.query.count() == 0
    assert not db.session.dirty


@pytest.mark.parametrize(('role', 'specialty'), [
    ('se', 'data'), ('se', 'infra'), ('se', 'apps_ai'), ('se', 'other'),
    ('dss', 'data'), ('dss', 'infra'), ('dss', 'apps_ai'), ('dss', 'other'),
    (None, 'data'),
])
@pytest.mark.parametrize('enabled', [True, False])
def test_schedules_follow_user_settings(client, enabled, role, specialty):
    """Saved local times, disabled automatic syncs and specialty gates stay truthful."""
    preference = UserPreference.query.first()
    preference.milestone_auto_sync = enabled
    preference.copilot_actions_enabled = enabled
    preference.milestone_sync_hour = 14
    preference.milestone_sync_minute = 35
    preference.user_role, preference.se_specialty = role, specialty
    db.session.commit()
    schedules = sync_jobs.operation_schedules()
    assert schedules['milestones']['label'] == (
        'Mon, Wed, Fri · 2:35 PM local' if enabled else 'Disabled')
    assert schedules['copilot-actions']['label'] == (
        'Daily · 6:00 AM local' if enabled else 'Disabled')
    automatic = role == 'dss' or specialty == 'data'
    for operation in ('fabric', 'sql-motions'):
        assert schedules[operation]['label'] == ('Every 7 days' if automatic else 'On demand')
        assert 'Data SEs and sellers' in schedules[operation]['detail']
    assert Job.query.count() == 0


@pytest.mark.parametrize(('operation', 'widget_keys', 'scheduler_name'), [
    ('fabric', ('fabric.expansion', 'fabric.trial_conversion', 'ptof.upcoming_renewals'),
     'run_fabric_sync_if_due'),
    ('sql-motions', ('sql.renewals', 'sql.eos'), 'run_sql_motions_sync_if_due'),
])
def test_report_widgets_manual_and_scheduled_sync_share_one_worker_job(
    client, app, executor, operation, widget_keys, scheduler_name,
):
    """Every launcher joins one durable import and observes the worker's persisted result."""
    from app.services import scheduled_sync

    preference = UserPreference.query.first()
    preference.user_role, preference.se_specialty = 'se', 'data'
    db.session.add(Customer(name='Shared report sync customer', tpid=12345))
    db.session.commit()

    scheduled = getattr(scheduled_sync, scheduler_name)(app)
    job_id = scheduled['job_id']
    manual = client.post(f'/api/jobs/start/{operation}', json={})
    assert manual.status_code == 202
    assert manual.json['job_id'] == job_id
    for key in widget_keys:
        response = client.post(
            f'/api/reports/dashboards/widgets/{key}/sync', json={'retry': False},
        )
        assert response.status_code == 200
        assert response.json['sync']['operation'] == operation
        assert response.json['sync']['job']['id'] == job_id
    if operation == 'fabric':
        report = client.post('/api/reports/fabric-consumption/import', json={})
        assert report.status_code == 202
        assert report.json['job_id'] == job_id
    assert getattr(scheduled_sync, scheduler_name)(app) is None
    assert Job.query.count() == 1
    assert executor == []

    assert job_queue.process_one('shared-report-worker', app=app, with_heartbeat=False)
    observed = client.get(f'/api/jobs/{job_id}').json
    assert observed['job']['status'] == Job.STATUS_DONE
    assert observed['job']['initiator'] == 'scheduled'
    assert observed['job']['progress']['percent'] == 100
    assert observed['job']['result']['items_synced'] == 2
    assert any(json.loads(event['data']).get('progress') == 50 for event in observed['events'])
    assert executor == [(operation, {})]
    assert Job.query.count() == 1


def test_start_after_failure_uses_current_options_and_keeps_failed_history(client, monkeypatch):
    """Starting again admits a new run without replaying the prior run's captured options."""
    monkeypatch.setattr(sync_jobs, '_execute_operation', lambda operation, options: {
        'success': False, 'error': 'Source unavailable.',
    })
    first = sync_jobs.admit_sync('favicons', {'missing_only': True})
    job_queue.process_one('test-worker')
    assert first.status == Job.STATUS_FAILED
    recorded_error = first.last_error
    assert recorded_error.endswith('Source unavailable.')
    response = client.post('/api/jobs/start/favicons', json={'missing_only': False})
    assert response.status_code == 202
    assert response.json['job_id'] != first.id
    assert response.json['job']['options'] == {'missing_only': False}
    db.session.refresh(first)
    assert first.status == Job.STATUS_FAILED
    assert first.last_error == recorded_error
    assert json.loads(first.payload) == {'missing_only': True}


def test_start_close_reopen_observes_same_job(client, app, executor):
    """The accepted job exists before response consumption and survives a new browser client."""
    start = client.post('/api/jobs/start/favicons', json={})
    assert start.status_code == 202
    data = start.get_json()
    job_id = data['job_id']
    assert data['job']['state'] == 'waiting_for_worker'
    assert executor == []
    start.close()
    reopened = app.test_client()
    assert reopened.get(data['status_url']).get_json()['job']['id'] == job_id
    job_queue.process_one('test-worker', app=app, with_heartbeat=True)
    observed = reopened.get(data['status_url']).get_json()
    assert observed['job']['id'] == job_id
    assert observed['job']['status'] == 'done'
    assert observed['job']['progress']['percent'] == 100
    assert observed['job']['result']['items_synced'] == 2
    assert any(json.loads(event['data']).get('progress') == 50 for event in observed['events'])
    assert len(executor) == 1


@pytest.mark.parametrize(('url', 'operation', 'payload'), [
    ('/api/msx/accounts/sync', 'accounts', {}),
    ('/api/msx/import-accounts', 'accounts-selected', {'accounts': [{'tpid': 12345}]}),
    ('/api/milestone-tracker/sync', 'milestones', {}),
    ('/api/milestone-tracker/sync-customer/{id}', 'customer-milestones', {}),
    ('/api/revenue/sync', 'revenue', {}),
    ('/api/msx/sync-marketing', 'marketing', {}),
    ('/api/reports/u2c/import-official', 'u2c', {}),
    ('/api/reports/fabric-consumption/import', 'fabric', {}),
    ('/api/copilot-actions/sync', 'copilot-actions', {}),
    ('/api/meetings/refresh', 'meetings', {'date': '2026-10-14'}),
    ('/api/meetings/sync-aura', 'meeting-aura', {}),
    ('/api/admin/fetch-favicons', 'favicons', {}),
    ('/api/admin/refresh-favicons', 'favicons', {}),
    ('/api/reports/sync-milestone-dates', 'milestone-dates', {}),
    ('/api/reports/activity-coverage/reconcile', 'activity-reconciliation', {}),
    ('/api/reports/activity-coverage/populate', 'calendar-population', {}),
    ('/api/reports/activity-coverage/match-milestones', 'meeting-preparation', {}),
])
def test_original_sync_routes_admit_and_observe_same_job(
    client, app, executor, url, operation, payload,
):
    """Every original JSON launcher admits one durable job without executing inline."""
    customer = Customer(name='Adapter customer', tpid=12345, tpid_url='https://example.test/msx')
    db.session.add(customer)
    if operation == 'u2c':
        db.session.add(Territory(name='East.SMECC.MAA.0101'))
    db.session.commit()
    response = client.post(url.format(id=customer.id), json=payload)
    assert response.status_code == 202
    receipt = response.json
    assert receipt['accepted'] is True
    assert receipt['job']['operation'] == operation
    assert receipt['job']['status'] == Job.STATUS_PENDING
    assert executor == []
    job_queue.process_one('adapter-test-worker', app=app, with_heartbeat=True)
    observed = client.get(receipt['status_url']).json['job']
    assert observed['id'] == receipt['job_id']
    assert observed['status'] == Job.STATUS_DONE
    assert observed['result']['items_synced'] == 2
    assert len(executor) == 1


def test_revenue_analysis_form_redirects_to_durable_progress(client, executor):
    """The form deliberately lands on the console instead of claiming inline completion."""
    response = client.post('/revenue/analyze')
    assert response.status_code == 302
    assert response.location.endswith('/admin/sync-console')
    job = Job.query.one()
    assert job.job_type == 'sync:revenue-analysis'
    assert job.status == Job.STATUS_PENDING
    assert executor == []


@pytest.mark.parametrize(('operation', 'url'), [
    ('revenue', '/api/revenue/sync/status'),
    ('fabric', '/api/reports/fabric-consumption/status'),
])
def test_legacy_status_pages_observe_queued_and_running_durable_jobs(client, operation, url):
    """Existing import pages see queued work and live progress rather than old success."""
    db.session.add(Customer(name='Sync status customer', tpid=12345))
    db.session.commit()
    job = sync_jobs.admit_sync(operation)
    response = client.get(url)
    assert response.status_code == 200
    status = response.json['status']
    assert status['state'] == 'in_progress'
    assert status['job_id'] == job.id
    assert status['job']['status'] == 'pending'
    job.status = Job.STATUS_RUNNING
    job.heartbeat_at = job_queue._now()
    job.progress_json = json.dumps({
        'phase': 'fetching', 'percent': 42, 'progress': 42, 'message': 'Pulling source rows.',
    })
    db.session.commit()
    status = client.get(url).json['status']
    assert status['state'] == 'in_progress'
    assert status['job']['progress']['percent'] == 42
    assert json.loads(status['details'])['message'] == 'Pulling source rows.'
    job.status = Job.STATUS_FAILED
    job.last_error = 'Source unavailable.'
    job.finished_at = job_queue._now()
    db.session.commit()
    status = client.get(url).json['status']
    assert status['state'] == 'failed'
    assert json.loads(status['details'])['error'] == 'Source unavailable.'


def test_calendar_population_status_uses_durable_progress_not_legacy_dates(client):
    """Canonical jobs carry a through date; measured counts come from runtime progress."""
    from datetime import date
    from app.services.activity_coverage import get_population_status

    job = sync_jobs.admit_sync('calendar-population', {'through': '2026-07-07'})
    job.status = Job.STATUS_RUNNING
    job.heartbeat_at = job_queue._now()
    job.progress_json = json.dumps({
        'phase': 'calendar', 'processed': 2, 'total': 5, 'percent': 40,
        'message': 'Stored calendar through 2026-07-02.',
    })
    db.session.commit()
    status = get_population_status(date(2026, 7, 7))
    assert status['running'] is True
    assert status['completed_count'] == 2
    assert status['total_dates'] == 5
    assert status['detail'] == 'Stored calendar through 2026-07-02.'


def test_failed_meeting_preparation_is_visible_after_returning_to_report(client):
    """Prerequisite failures are not hidden just because no individual meeting failed."""
    job = sync_jobs.admit_sync('meeting-preparation')
    job.status = Job.STATUS_FAILED
    job.last_error = 'Account prerequisite failed.'
    job.finished_at = job_queue._now()
    db.session.commit()
    status = client.get('/api/reports/activity-coverage/match-status').json
    assert status['running_job'] is False
    assert status['error'] == 'Account prerequisite failed.'
    page = BeautifulSoup(client.get('/reports/activity-coverage').data, 'html.parser')
    assert page.select_one('[data-sb-sync-progress="meeting-preparation"] progress')
    snapshot = client.get('/api/jobs/operations/meeting-preparation').json['job']
    assert snapshot['status'] == 'failed'
    assert snapshot['error'] == 'Account prerequisite failed.'


def test_calendar_failure_before_checkpoint_is_visible_after_returning(client):
    """A worker failure remains visible even if no population checkpoint was written."""
    job = sync_jobs.admit_sync('calendar-population')
    job.status = Job.STATUS_FAILED
    job.active_key = None
    job.last_error = 'Calendar source unavailable.'
    job.finished_at = job_queue._now()
    db.session.commit()
    status = client.get('/api/reports/activity-coverage/import-status').json
    assert status['running'] is False
    assert status['error'] == 'Calendar source unavailable.'
    assert status['detail'] == status['error']
    page = BeautifulSoup(client.get('/reports/activity-coverage').data, 'html.parser')
    assert page.select_one('[data-sb-sync-progress="calendar-population"] progress')
    snapshot = client.get('/api/jobs/operations/calendar-population').json['job']
    assert snapshot['status'] == 'failed'
    assert snapshot['error'] == status['error']
    sync_jobs.admit_sync('calendar-population')
    status = client.get('/api/reports/activity-coverage/import-status').json
    assert status['running'] is True
    assert status['error'] is None


def test_meeting_status_card_observes_running_job_before_newer_queued_window(client):
    """The global status card follows the actual source fetch instead of its queued successor."""
    customer = Customer(name='Meeting Status Test Customer', tpid=999002)
    db.session.add(customer)
    db.session.commit()
    try:
        running = sync_jobs.admit_sync('meeting-aura', {'anchor': '2026-10-14'})
        running.status = Job.STATUS_RUNNING
        running.progress_json = json.dumps({'message': 'Fetching five meeting dates.'})
        running.heartbeat_at = job_queue._now()
        db.session.commit()
        queued = sync_jobs.admit_sync('meeting-aura', {'anchor': '2026-10-15'})
        assert queued.id > running.id
        status = client.get('/api/meetings/sync-status').json
        assert status['running'] is True
        assert status['job_id'] == running.id
        assert status['job']['progress']['message'] == 'Fetching five meeting dates.'
    finally:
        db.session.delete(customer)
        db.session.commit()


def test_manual_and_headless_duplicate_share_identity(client):
    """Both launchers use identical options and the same atomically protected resource."""
    response = client.post('/api/jobs/start/favicons', json={})
    manual_id = response.get_json()['job_id']
    headless = sync_jobs.admit_sync('favicons', initiator='scheduled')
    assert headless.id == manual_id
    assert Job.query.count() == 1


def test_different_active_options_queue_separately(client):
    """Different captured requests wait for their group instead of failing admission."""
    first = client.post('/api/jobs/start/favicons', json={'missing_only': True})
    assert first.status_code == 202
    second = client.post('/api/jobs/start/favicons', json={'missing_only': False})
    assert second.status_code == 202
    assert second.json['job_id'] != first.json['job_id']
    assert second.json['job']['state'] == 'waiting_for_group'
    assert second.json['job']['blocker']['id'] == first.json['job_id']
    assert Job.query.count() == 2


def test_idempotency_key_recovers_completed_run(client, executor):
    """A lost POST response cannot duplicate an already completed operation."""
    headers = {'Idempotency-Key': 'lost-response'}
    first = client.post('/api/jobs/start/favicons', json={}, headers=headers)
    job_queue.process_one('test-worker')
    repeated = client.post('/api/jobs/start/favicons', json={}, headers=headers)
    assert repeated.get_json()['job_id'] == first.get_json()['job_id']
    assert Job.query.count() == 1


@pytest.mark.parametrize('payload', [[], 'bad', {'arbitrary_handler': 'danger'}, {'missing_only': 1}])
def test_invalid_options_do_not_admit_work(client, payload):
    """The allowlist cannot be bypassed with client-selected handler names or bad types."""
    response = client.post('/api/jobs/start/favicons', json=payload)
    assert response.status_code == 400
    assert Job.query.count() == 0


def test_simultaneous_admission_is_atomic_across_sessions(app):
    """Separate callers must not race through a check-then-insert dedupe."""
    barrier = threading.Barrier(4)

    def start():
        with app.app_context():
            barrier.wait(timeout=10)
            return sync_jobs.admit_sync('favicons').id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: start(), range(4)))
    assert len(set(ids)) == 1
    assert Job.query.count() == 1


def test_domain_failure_is_terminal_not_success_or_blind_retry(monkeypatch):
    """A returned success:false must be failed even for a repeatable operation."""
    monkeypatch.setattr(sync_jobs, '_execute_operation',
                        lambda operation, options: {'success': False, 'error': 'Sign in first.'})
    job = sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    db.session.refresh(job)
    assert job.status == 'failed'
    assert job.attempts == 1
    assert 'Sign in first' in job.last_error
    assert job.active_key is None
    assert sync_jobs.job_snapshot(job)['progress'].get('percent') != 100


def test_safe_transient_failure_has_bounded_backoff(monkeypatch):
    """Only explicitly safe, typed transient failures qualify for automatic retry."""
    def fail(operation, options):
        raise requests.Timeout('temporary network failure')

    monkeypatch.setattr(sync_jobs, '_execute_operation', fail)
    job = sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    db.session.refresh(job)
    assert job.status == 'pending'
    assert job.attempts == 1
    assert job.available_at > job_queue._now()
    assert job.active_key.startswith('sync:favicons:')


def test_last_success_survives_a_failed_new_attempt(monkeypatch, executor):
    """The active/latest attempt cannot erase the last successful refresh."""
    first = sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    success_at = next(row['last_success_at']
                      for row in sync_jobs.operation_snapshots()['operations']
                      if row['operation'] == 'favicons')
    monkeypatch.setattr(sync_jobs, '_execute_operation',
                        lambda operation, options: {'success': False, 'error': 'Failed.'})
    second = sync_jobs.admit_sync('favicons')
    assert second.id != first.id
    job_queue.process_one('test-worker')
    row = next(row for row in sync_jobs.operation_snapshots()['operations']
               if row['operation'] == 'favicons')
    assert row['latest']['status'] == 'failed'
    assert row['last_success_at'] == success_at


def test_stream_error_without_terminal_success_fails_job(monkeypatch):
    """A VPN/error event cannot be reported as a completed sync."""
    db.session.add(Customer(name='ACME', tpid=123))
    db.session.commit()

    def fail(operation, options):
        return sync_jobs.consume_stream(iter([
            'event: vpn_blocked\ndata: {"message": "Connect to VPN"}\n\n',
        ]))

    monkeypatch.setattr(sync_jobs, '_execute_operation', fail)
    job = sync_jobs.admit_sync('marketing')
    job_queue.process_one('test-worker')
    db.session.refresh(job)
    assert job.status == 'failed'
    assert 'Connect to VPN' in job.last_error


def test_legacy_sse_disconnect_does_not_remove_admitted_work(client, executor):
    """Legacy SSE readers observe the same job; closing the response is not cancellation."""
    response = client.post('/api/admin/refresh-favicons',
                           headers={'Accept': 'text/event-stream'}, buffered=False)
    assert response.status_code == 200
    assert b'accepted' in next(iter(response.response))
    response.close()
    job = Job.query.one()
    assert job.status == 'pending'
    job_queue.process_one('test-worker')
    assert db.session.get(Job, job.id).status == 'done'


def test_progress_events_are_bounded_and_measured(monkeypatch):
    """Progress must not reach 100 before the terminal job commit."""
    def run(operation, options):
        for count in range(205):
            sync_jobs.publish_progress({'progress': count, 'current': count, 'total': 205})
        assert Job.query.one().status == 'running'
        assert sync_jobs.job_snapshot(Job.query.one())['progress']['percent'] == 99
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    assert JobEvent.query.count() == sync_jobs.EVENT_LIMIT


def test_two_workers_can_claim_independent_syncs():
    """Independent fetches are not blocked behind an unrelated run."""
    sync_jobs.admit_sync('favicons')
    sync_jobs.admit_sync('copilot-actions')
    assert job_queue.claim_next('worker-one') is not None
    assert job_queue.claim_next('worker-two') is not None


def test_progress_never_commits_pending_domain_writes(monkeypatch):
    """Live progress never commits staged domain data or changes its rollback boundary."""
    def run(operation, options):
        db.session.add(Customer(name='Must roll back', tpid=987))
        db.session.flush()
        sync_jobs.publish_progress({'progress': 50, 'message': 'Atomic save underway.'})
        assert JobEvent.query.filter_by(event_type='progress').count() == 0
        raise RuntimeError('Save failed')

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    first = sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    assert Customer.query.filter_by(tpid=987).first() is None
    assert db.session.get(Job, first.id).status == 'failed'
    monkeypatch.setattr(sync_jobs, '_execute_operation', lambda *args: {'success': True})
    second = sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    assert db.session.get(Job, second.id).status == 'done'


def test_committed_atomic_save_flushes_latest_progress(monkeypatch):
    """After the save commits, the measured unit becomes durable without a lock conflict."""
    def run(operation, options):
        db.session.add(Customer(name='Saved', tpid=988))
        db.session.flush()
        sync_jobs.publish_progress({'progress': 80, 'current': 8, 'total': 10})
        db.session.commit()
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    job = sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    assert db.session.get(Job, job.id).status == 'done'
    assert json.loads(JobEvent.query.filter_by(event_type='progress').one().data_json)['current'] == 8


def test_nested_dependency_uses_same_worker_and_retains_child_identity(monkeypatch, app):
    """An admitted dependency cannot deadlock behind its parent in a serial queue."""
    seen = []

    def run(operation, options):
        seen.append(operation)
        if operation == 'copilot-actions':
            sync_jobs.run_dependency('favicons')
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    parent = sync_jobs.admit_sync('copilot-actions')
    job_queue.process_one('test-worker', app=app, with_heartbeat=True)
    child = Job.query.filter_by(job_type='sync:favicons').one()
    assert seen == ['copilot-actions', 'favicons']
    assert child.initiator == f'job:{parent.id}'
    assert child.claimed_by == parent.claimed_by
    assert child.status == parent.status == 'done'


def test_legacy_status_projects_pending_owner_not_previous_completion(client):
    """Old UI polling cannot mistake a newly queued job for yesterday's completed run."""
    SyncStatus.mark_started('favicons')
    SyncStatus.mark_completed('favicons', success=True, items_synced=4)
    client.post('/api/jobs/start/favicons', json={})
    projected = SyncStatus.get_status('favicons')
    assert projected['state'] == 'in_progress'
    assert projected['completed_at'] is None


def test_marketing_worker_exception_propagates_without_waiting_for_done(monkeypatch):
    """A crashed source future cannot leave the stream blocked on a missing sentinel."""
    from app.services import marketing_sync

    db.session.add(Customer(name='Marketing', tpid=989))
    db.session.commit()

    def fail(*args):
        raise RuntimeError('Remote fetch crashed')

    monkeypatch.setattr(marketing_sync, '_fetch_worker', fail)
    with pytest.raises(RuntimeError, match='Remote fetch crashed'):
        list(marketing_sync.sync_marketing_stream())


def test_safe_retry_stops_after_three_attempts(monkeypatch):
    """Retryable work cannot loop forever or release its ownership between attempts."""
    def fail(*args):
        raise requests.Timeout('Temporary source outage')

    monkeypatch.setattr(sync_jobs, '_execute_operation', fail)
    job = sync_jobs.admit_sync('favicons')
    for attempt in range(1, 4):
        job_queue.process_one('test-worker')
        db.session.refresh(job)
        assert job.attempts == attempt
        if attempt < 3:
            assert job.status == 'pending'
            assert job.active_key.startswith('sync:favicons:')
            job.available_at = job_queue._now() - timedelta(seconds=1)
            db.session.commit()
    assert job.status == 'failed'
    assert job.active_key is None


@pytest.mark.parametrize('operation,status', [('favicons', 'pending'),
                                             ('copilot-actions', 'failed')])
def test_interrupted_recovery_obeys_operation_safety(operation, status):
    """Stale ownership is recovered only for a safe operation within its attempt bound."""
    job = sync_jobs.admit_sync(operation)
    job_queue.claim_next('dead-worker')
    job.heartbeat_at = job_queue._now() - timedelta(
        seconds=Job.HEARTBEAT_STALE_SECONDS + 1)
    db.session.commit()
    assert sync_jobs.job_snapshot(job)['state'] == 'interrupted'
    assert job_queue.reclaim_stale() == 1
    db.session.refresh(job)
    assert job.status == status
    assert bool(job.active_key) is (status == 'pending')


def test_malformed_payload_fails_without_abandoning_running_ownership(executor):
    """Invalid persisted inputs become terminal failure, not an unreclaimable running job."""
    job = sync_jobs.admit_sync('favicons')
    job.payload = '{'
    db.session.commit()
    job_queue.process_one('test-worker')
    db.session.refresh(job)
    assert job.status == 'failed'
    assert job.attempts == 1
    assert job.active_key is None
    assert executor == []


def test_generic_enqueue_cannot_bypass_canonical_sync_admission():
    """Headless callers cannot bypass validation, ownership or retry policies."""
    with pytest.raises(ValueError, match='admit_sync'):
        job_queue.enqueue('sync:favicons')
    assert Job.query.count() == 0


@pytest.mark.parametrize('operation,legacy', [('u2c', 'u2c_import'),
                                            ('fabric-revenue', 'fabric_consumption')])
def test_domain_guard_does_not_confuse_its_own_job_with_another_sync(
    monkeypatch, operation, legacy,
):
    """A source's cadence/full-import guards must not reject their current queue owner."""
    db.session.add(Customer(name='Guard scope', tpid=991))
    if operation == 'u2c':
        db.session.add(Territory(name='East.SMECC.MAA.0101'))
    db.session.commit()
    SyncStatus.mark_started(legacy)
    SyncStatus.mark_completed(legacy, success=True)

    def run(*args):
        assert SyncStatus.get_status(legacy)['state'] == 'complete'
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    job = sync_jobs.admit_sync(operation)
    assert SyncStatus.get_status(legacy)['state'] == 'in_progress'
    job_queue.process_one('test-worker')
    db.session.refresh(job)
    assert job.status == 'done'


def test_failed_domain_result_retains_partial_counts_and_outcome(monkeypatch):
    """A real failed outcome keeps its structured evidence, not just an error string."""
    result = {'success': False, 'error': 'Partial import', 'outcome': 'broken', 'saved': 3}
    monkeypatch.setattr(sync_jobs, '_execute_operation', lambda *args: result)
    job = sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    db.session.refresh(job)
    assert job.status == 'failed'
    assert json.loads(job.result) == result


def test_skipped_refresh_does_not_advance_the_source_success_clock(monkeypatch, executor):
    """A due-check/no-op cannot masquerade as newly refreshed source data."""
    sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    first = next(row for row in sync_jobs.operation_snapshots()['operations']
                 if row['operation'] == 'favicons')['last_success_at']
    monkeypatch.setattr(sync_jobs, '_execute_operation',
                        lambda *args: {'success': True, 'skipped': True})
    sync_jobs.admit_sync('favicons')
    job_queue.process_one('test-worker')
    row = next(row for row in sync_jobs.operation_snapshots()['operations']
               if row['operation'] == 'favicons')
    assert row['latest']['status'] == 'done'
    assert row['last_success_at'] == first
    third = sync_jobs.admit_sync('favicons')
    assert sync_jobs._iso(third.last_success_at) == first
