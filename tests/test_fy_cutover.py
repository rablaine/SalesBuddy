"""Tests for Fiscal Year cutover feature."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


class TestFYCutoverService:
    """Tests for app/services/fy_cutover.py."""

    def test_get_transition_state_default(self, app):
        """Default state should be not in transition."""
        with app.app_context():
            from app.services.fy_cutover import get_transition_state
            state = get_transition_state()
            assert state['in_transition'] is False
            assert state['fy_label'] is None
            assert state['started_at'] is None

    def test_enter_and_exit_transition_mode(self, app):
        """Should toggle transition state on and off."""
        with app.app_context():
            from app.services.fy_cutover import (
                enter_transition_mode,
                exit_transition_mode,
                get_transition_state,
            )

            enter_transition_mode('FY25')
            state = get_transition_state()
            assert state['in_transition'] is True
            assert state['fy_label'] == 'FY25'
            assert state['started_at'] is not None

            exit_transition_mode()
            state = get_transition_state()
            assert state['in_transition'] is False
            assert state['fy_label'] is None

    def test_list_archives_empty(self, app):
        """Should return empty list when no archives exist."""
        with app.app_context():
            from app.services.fy_cutover import list_archives
            archives = list_archives()
            assert archives == []

    def test_start_new_fiscal_year(self, app, tmp_path):
        """Should create archive and enter transition mode."""
        with app.app_context():
            from app.services.fy_cutover import (
                get_fiscal_year_labels,
                get_transition_state,
                start_new_fiscal_year,
            )

            labels = get_fiscal_year_labels()
            current_fy = labels['current_fy']
            next_fy = labels['next_fy']

            # Create a fake DB file in tmp
            fake_db = tmp_path / 'notehelper.db'
            fake_db.write_text('test database content')

            with patch('app.services.fy_cutover._get_data_dir', return_value=tmp_path):
                with patch('app.services.fy_cutover._get_onedrive_backup_root', return_value=None):
                    result = start_new_fiscal_year()

            assert result['archive_path'] == str(tmp_path / f'{current_fy}.db')
            assert result['onedrive_path'] is None
            assert result['stats']['customers'] >= 0
            assert (tmp_path / f'{current_fy}.db').exists()

            state = get_transition_state()
            assert state['in_transition'] is True
            assert state['fy_label'] == next_fy

    def test_start_new_fy_with_onedrive(self, app, tmp_path):
        """Should copy archive to OneDrive when configured."""
        with app.app_context():
            from app.services.fy_cutover import (
                get_fiscal_year_labels,
                start_new_fiscal_year,
            )

            current_fy = get_fiscal_year_labels()['current_fy']

            fake_db = tmp_path / 'notehelper.db'
            fake_db.write_text('test db')
            onedrive_root = tmp_path / 'OneDrive'
            onedrive_root.mkdir()

            with patch('app.services.fy_cutover._get_data_dir', return_value=tmp_path):
                with patch('app.services.fy_cutover._get_onedrive_backup_root',
                           return_value=str(onedrive_root)):
                    result = start_new_fiscal_year()

            assert result['onedrive_path'] is not None
            assert (onedrive_root / 'previous_years' / f'{current_fy}.db').exists()

    def test_preview_purge(self, app, sample_data):
        """Should correctly identify customers to keep vs purge."""
        with app.app_context():
            from app.services.fy_cutover import preview_purge

            # sample_data has TPIDs 1001, 1002, 1003
            # Keep only 1001, purge 1002 and 1003
            preview = preview_purge([1001])
            assert preview['kept_customers'] >= 1
            assert preview['purge_customers'] >= 2

    def test_finalize_alignments(self, app, sample_data):
        """Should purge orphaned customers and exit transition."""
        with app.app_context():
            from app.models import Customer, db
            from app.services.fy_cutover import (
                enter_transition_mode,
                finalize_alignments,
                get_transition_state,
            )

            enter_transition_mode('FY25')

            # Keep only TPID 1001 (Acme Corp)
            initial_count = Customer.query.count()
            assert initial_count >= 3

            summary = finalize_alignments([1001])
            assert summary['purged_customers'] >= 2
            assert summary['kept_customers'] >= 1

            # Verify transition ended
            state = get_transition_state()
            assert state['in_transition'] is False

            # Verify Acme still exists
            acme = Customer.query.filter_by(tpid=1001).first()
            assert acme is not None
            assert acme.name == 'Acme Corp'

    def test_finalize_keeps_customers_in_synced_list(self, app, sample_data):
        """Customers whose TPID is in synced_tpids should be kept."""
        with app.app_context():
            from app.models import Customer, db
            from app.services.fy_cutover import enter_transition_mode, finalize_alignments

            enter_transition_mode('FY25')

            # Keep all three TPIDs — nothing should be purged
            summary = finalize_alignments([1001, 1002, 1003])
            assert summary['purged_customers'] == 0
            assert summary['kept_customers'] >= 3


class TestFYAdminRoutes:
    """Tests for FY cutover admin API routes."""

    def test_fy_status(self, client):
        """GET /api/admin/fy/status should return state and archives."""
        resp = client.get('/api/admin/fy/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'transition' in data
        assert 'archives' in data
        assert data['transition']['in_transition'] is False

    def test_fy_start_no_body_required(self, client):
        """POST /api/admin/fy/start should work without any JSON body."""
        with patch('app.services.fy_cutover.start_new_fiscal_year') as mock_start:
            mock_start.return_value = {
                'archive_path': '/tmp/FY26.db',
                'onedrive_path': None,
                'stats': {'customers': 5, 'notes': 10, 'sellers': 2},
                'current_fy': 'FY26',
                'next_fy': 'FY27',
            }
            resp = client.post('/api/admin/fy/start')
            assert resp.status_code == 200
            mock_start.assert_called_once()

    def test_fy_preview_purge_requires_tpids(self, client):
        """POST /api/admin/fy/preview-purge should reject empty list."""
        resp = client.post('/api/admin/fy/preview-purge',
                           data=json.dumps({}),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_fy_finalize_requires_sync_file(self, client):
        """POST /api/admin/fy/finalize should reject when no sync file exists."""
        resp = client.post('/api/admin/fy/finalize',
                           content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'Run the Final Sync first' in data['error']

    def test_fy_finalize_reads_tpid_file(self, client, app, sample_data):
        """POST /api/admin/fy/finalize should read TPIDs from the JSON file."""
        with app.app_context():
            from app.services.fy_cutover import enter_transition_mode
            enter_transition_mode('FY26')

        # Write a TPID file with known values — use same path as the route
        tpid_file = Path(app.instance_path).parent / 'data' / 'last_sync_tpids.json'
        tpid_file.parent.mkdir(parents=True, exist_ok=True)
        tpid_file.write_text(json.dumps([1001, 1002, 1003]))

        try:
            resp = client.post('/api/admin/fy/finalize',
                               content_type='application/json')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['success'] is True
            assert data['kept_customers'] >= 3
            assert data['purged_customers'] == 0
            # TPID file should be cleaned up after finalization
            assert not tpid_file.exists()
        finally:
            tpid_file.unlink(missing_ok=True)

    def test_clear_backup_notes(self, client):
        """POST /api/backup/clear-notes should succeed even without backup configured."""
        resp = client.post('/api/backup/clear-notes')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_fy_exit_transition(self, client, app):
        """POST /api/admin/fy/exit-transition should clear transition."""
        # First enter transition manually
        with app.app_context():
            from app.services.fy_cutover import enter_transition_mode
            enter_transition_mode('FY25')

        resp = client.post('/api/admin/fy/exit-transition')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

        # Verify state cleared
        resp2 = client.get('/api/admin/fy/status')
        data2 = resp2.get_json()
        assert data2['transition']['in_transition'] is False

    def test_exit_transition_records_last_completed(self, app):
        """exit_transition_mode should store fy_last_completed from label."""
        with app.app_context():
            from app.models import UserPreference
            from app.services.fy_cutover import enter_transition_mode, exit_transition_mode
            enter_transition_mode('FY27')
            exit_transition_mode()
            pref = UserPreference.query.first()
            assert pref.fy_last_completed == 'FY27'

    def test_exit_transition_without_label_preserves_last_completed(self, app):
        """Cancelling (no label set) should not overwrite fy_last_completed."""
        with app.app_context():
            from app.models import UserPreference, db
            from app.services.fy_cutover import exit_transition_mode
            pref = UserPreference.query.first()
            pref.fy_last_completed = 'FY26'
            pref.fy_transition_active = True
            pref.fy_transition_label = None  # already cleared or never set
            db.session.commit()

            exit_transition_mode()
            pref = UserPreference.query.first()
            assert pref.fy_last_completed == 'FY26'


class TestFYTransitionBanner:
    """Tests for the FY transition banner in base template."""

    def test_banner_hidden_when_no_transition(self, client):
        """Banner should not appear when not in transition."""
        resp = client.get('/')
        assert b'Transition in Progress' not in resp.data

    def test_banner_visible_during_transition(self, client, app):
        """Banner should appear when in transition mode."""
        with app.app_context():
            from app.services.fy_cutover import enter_transition_mode
            enter_transition_mode('FY26')

        resp = client.get('/')
        assert b'FY26 Transition in Progress' in resp.data

        # Clean up
        with app.app_context():
            from app.services.fy_cutover import exit_transition_mode
            exit_transition_mode()

    def test_changeover_reminder_banner_in_july(self, client, app):
        """Banner should remind user to start FY transition in July."""
        with patch('app.routes.main.datetime') as mock_dt:
            mock_dt.today.return_value = datetime(2026, 7, 15)
            with patch('app.services.fy_cutover.datetime') as mock_fy_dt:
                mock_fy_dt.now.return_value = datetime(2026, 7, 15)
                resp = client.get('/')
        assert b'time to transition to FY27' in resp.data
        assert b'Get started in the Admin Panel' in resp.data

    def test_changeover_reminder_hidden_after_completion(self, client, app):
        """Banner should NOT appear if FY transition was already completed."""
        with app.app_context():
            from app.models import UserPreference, db
            pref = UserPreference.query.first()
            pref.fy_last_completed = 'FY27'
            db.session.commit()

        with patch('app.routes.main.datetime') as mock_dt:
            mock_dt.today.return_value = datetime(2026, 7, 15)
            with patch('app.services.fy_cutover.datetime') as mock_fy_dt:
                mock_fy_dt.now.return_value = datetime(2026, 7, 15)
                resp = client.get('/')
        assert b'time to transition' not in resp.data

    def test_changeover_reminder_hidden_outside_window(self, client):
        """Banner should NOT appear outside Jul-Aug."""
        with patch('app.routes.main.datetime') as mock_dt:
            mock_dt.today.return_value = datetime(2026, 9, 1)
            resp = client.get('/')
        assert b'time to transition' not in resp.data

    def test_changeover_reminder_hidden_during_active_transition(self, client, app):
        """Banner should NOT appear if transition is already in progress."""
        with app.app_context():
            from app.services.fy_cutover import enter_transition_mode
            enter_transition_mode('FY27')

        with patch('app.routes.main.datetime') as mock_dt:
            mock_dt.today.return_value = datetime(2026, 7, 15)
            resp = client.get('/')
        assert b'time to transition' not in resp.data

        # Clean up
        with app.app_context():
            from app.services.fy_cutover import exit_transition_mode
            exit_transition_mode()


class TestNavbarReorg:
    """Tests for navbar reorganization."""

    def test_notes_in_main_nav(self, client):
        """Notes should be a top-level nav item, not in More menu."""
        resp = client.get('/')
        html = resp.data.decode()
        # Notes should be in the main nav area with id="navNotes"
        assert 'id="navNotes"' in html

    def test_customers_in_more_menu(self, client):
        """Customers should be inside the More dropdown."""
        resp = client.get('/')
        html = resp.data.decode()
        # Customers should NOT have a top-level nav item anymore
        assert 'id="navCustomers"' not in html

    def test_pods_in_more_menu(self, client):
        """PODs should be inside the More dropdown."""
        resp = client.get('/')
        html = resp.data.decode()
        # PODs should NOT have a top-level nav item anymore
        assert 'id="navPods"' not in html

    def test_partners_in_main_nav(self, client):
        """Partners should be a top-level nav item."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'id="navPartners"' in html

    def test_topics_in_main_nav(self, client):
        """Topics should be a top-level nav item."""
        resp = client.get('/')
        html = resp.data.decode()
        assert 'id="navTopics"' in html


class TestCallsToNotesRename:
    """Tests verifying 'Calls' was renamed to 'Notes' in the UI."""

    def test_analytics_says_notes(self, client, sample_data):
        """Analytics page should say 'Notes' not 'Calls'."""
        resp = client.get('/analytics')
        html = resp.data.decode()
        assert 'Notes This Week' in html
        assert 'Total Notes' in html
        assert 'Calls This Week' not in html
        assert 'Total Calls' not in html

    def test_topics_list_says_notes(self, client, sample_data):
        """Topics list should say 'notes' not 'calls'."""
        resp = client.get('/topics')
        html = resp.data.decode()
        assert 'By Notes' in html
        assert 'By Calls' not in html
