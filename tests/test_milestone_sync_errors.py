"""Regression coverage for sync exceptions and terminated progress streams."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import db, Milestone, MsxTask, SyncStatus


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
        )
        body = response.get_data(as_text=True)
    assert 'event: start' in body
    assert 'event: error' in body
    assert 'Some earlier changes may already be saved' in body
    assert 'event: complete' not in body
    assert 'NOT NULL' not in body
    with app.app_context():
        assert SyncStatus.get_status('milestones')['state'] == 'failed'
        assert MsxTask.query.filter_by(msx_task_id='failed-sync-task').first() is None


def test_failure_status_write_error_still_emits_terminal_event(client, sample_data, caplog):
    """Even a database unable to persist failure status must end the UI stream clearly."""
    with patch(
        'app.services.milestone_sync.sync_all_customer_milestones_stream',
        side_effect=RuntimeError('Internal sync error'),
    ), patch.object(
        SyncStatus, 'mark_completed', side_effect=IntegrityError('statement', {}, Exception()),
    ):
        response = client.post(
            '/api/milestone-tracker/sync', headers={'Accept': 'text/event-stream'},
        )
        body = response.get_data(as_text=True)
    assert 'event: error' in body
    assert 'Could not record milestone sync failure' in caplog.text


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required for JS tests')
def test_sync_error_ui():
    """Run the actual streaming UI against success, explicit error, and early EOF."""
    test_file = Path(__file__).parent / 'js' / 'milestone_sync_errors.test.cjs'
    result = subprocess.run(
        ['node', '--test', str(test_file)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
