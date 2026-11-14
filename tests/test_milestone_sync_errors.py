"""Regression coverage for sync exceptions and terminated progress streams."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import db, Customer, Job, Milestone, MsxTask, SyncStatus
from app.services import job_queue


def test_stream_database_error_reports_failure_and_rolls_back(client, app, sample_data):
    """A task autoflush failure becomes a terminal SSE error and failed status."""
    with app.app_context():
        milestone = Milestone(url='https://example.com/sync-error', title='Sync error')
        db.session.add(milestone)
        db.session.commit()
        milestone_id = milestone.id

    def failing_sync():
        """Reproduce the pending-task autoflush exception from the deployed logs."""
        SyncStatus.mark_started('milestones')
        yield 'event: start\ndata: {"total": 1}\n\n'
        db.session.add(MsxTask(
            msx_task_id='failed-sync-task', subject=None,
            task_category=861980002, milestone_id=milestone_id,
        ))
        SyncStatus.update_heartbeat('milestones')

    with patch(
        'app.services.milestone_sync.sync_all_customer_milestones_stream',
        side_effect=failing_sync,
    ):
        response = client.post(
            '/api/milestone-tracker/sync', headers={'Accept': 'text/event-stream'},
            buffered=False,
        )
        job_queue.process_one('test-worker', app=app, with_heartbeat=True)
        body = response.get_data(as_text=True)
    assert 'event: start' in body
    assert 'event: error' in body
    assert 'Some earlier changes may already be saved' in body
    assert 'event: complete' not in body
    assert 'NOT NULL' not in body
    with app.app_context():
        assert SyncStatus.get_status('milestones')['state'] == 'failed'
        assert MsxTask.query.filter_by(msx_task_id='failed-sync-task').first() is None


def test_legacy_failure_status_write_cannot_hide_job_failure(client, app, sample_data, caplog):
    """Durable failure does not depend on the old SyncStatus failure-write path."""
    with patch(
        'app.services.milestone_sync.sync_all_customer_milestones_stream',
        side_effect=RuntimeError('Internal sync error'),
    ), patch.object(
        SyncStatus, 'mark_completed', side_effect=IntegrityError('statement', {}, Exception()),
    ):
        response = client.post(
            '/api/milestone-tracker/sync', headers={'Accept': 'text/event-stream'},
            buffered=False,
        )
        job_queue.process_one('test-worker', app=app, with_heartbeat=True)
        body = response.get_data(as_text=True)
    assert 'event: error' in body
    assert SyncStatus.get_status('milestones')['state'] == 'failed'
    assert 'Internal sync error' in caplog.text


@pytest.mark.parametrize('failure', ['stale', 'team', 'tasks'])
def test_stage_failure_marks_the_durable_job_failed(client, app, sample_data, failure):
    """A later-stage error cannot finish as a green successful sync."""
    from app.services import milestone_sync

    def stage_result(stage):
        """Return a generator carrying its terminal stage result."""
        yield from ()
        return {
            'success': failure != stage,
            'error': f'Simulated {stage} failure' if failure == stage else '',
        }

    with patch.object(milestone_sync, 'test_connection', return_value={'success': True}), \
         patch.object(milestone_sync, 'extract_account_id_from_url',
                      return_value='aaaabbbb-1111-2222-3333-444455556666'), \
         patch.object(milestone_sync, 'batch_get_opportunities', return_value={
             'success': True, 'by_account': {},
         }), \
         patch.object(milestone_sync, '_sync_stale_milestones',
                      side_effect=lambda ids: stage_result('stale')), \
         patch.object(milestone_sync, '_sync_team_milestones', return_value={
             'success': failure != 'team', 'error': 'Simulated team failure',
         }), \
         patch.object(milestone_sync, '_sync_all_tasks',
                      side_effect=lambda: stage_result('tasks')), \
         patch.object(milestone_sync, '_sync_caip_activities',
                      return_value={'success': True}), \
         patch.object(milestone_sync, '_update_deal_team_memberships'), \
         patch.object(milestone_sync, '_sync_team_milestone_comments',
                      side_effect=lambda **kwargs: stage_result('comments')), \
         patch.object(milestone_sync, '_sync_milestone_audits',
                      side_effect=lambda: stage_result('audits')):
        response = client.post('/api/milestone-tracker/sync', headers={
            'Accept': 'application/json',
        })
        assert response.status_code == 202
        job_id = response.get_json()['job_id']
        job_queue.process_one('test-worker', app=app, with_heartbeat=True)

    with app.app_context():
        job = db.session.get(Job, job_id)
        assert job.status == 'failed'
        assert f'Simulated {failure} failure' in job.last_error


@pytest.mark.parametrize('failure', ['fetch', 'commit'])
def test_stale_failure_does_not_refresh_timestamps(app, sample_data, failure):
    """Unfetched or rolled-back records stay stale and report the failure."""
    from app.services import milestone_sync

    with app.app_context():
        customer = db.session.get(Customer, sample_data['customer1_id'])
        milestone = Milestone(
            url='https://example.test/stale-fetch-failure', msx_milestone_id='stale-failure',
            customer=customer, title='Unchanged',
        )
        db.session.add(milestone)
        db.session.commit()
        response = {
            'success': failure != 'fetch', 'error': 'Simulated stale fetch failure',
            'by_id': {'stale-failure': {'id': 'stale-failure', 'name': 'New title'}},
        }
        original_commit = db.session.commit

        def commit():
            """Fail the fallback write without interfering with fixture setup."""
            if failure == 'commit':
                raise SQLAlchemyError('Simulated stale commit failure')
            original_commit()

        with patch.object(milestone_sync, 'batch_get_milestones_by_id',
                          return_value=response), \
             patch.object(db.session, 'commit', side_effect=commit):
            generator = milestone_sync._sync_stale_milestones(set())
            try:
                while True:
                    next(generator)
            except StopIteration as stop:
                result = stop.value
        assert result['success'] is False
        assert 'stale' in result['error'].lower()
        assert result['milestones_updated'] == 0
        db.session.refresh(milestone)
        assert milestone.last_synced_at is None
        assert milestone.title == 'Unchanged'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required for JS tests')
def test_sync_error_ui():
    """Run the shared durable sync UI against completion, failure and reconnect paths."""
    test_file = Path(__file__).parent / 'js' / 'ui_sync_progress.test.cjs'
    result = subprocess.run(
        ['node', '--test', str(test_file)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
