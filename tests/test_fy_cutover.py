"""Tests for Fiscal Year cutover feature."""

import json
import os
from datetime import datetime, timezone, date
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
            fake_db = tmp_path / 'salesbuddy.db'
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

            fake_db = tmp_path / 'salesbuddy.db'
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

    def test_fy_finalize_requires_sync_file(self, client, app):
        """POST /api/admin/fy/finalize should reject when no sync file exists."""
        # Ensure no leftover file from other tests
        tpid_file = Path(app.instance_path).parent / 'data' / 'last_sync_tpids.json'
        tpid_file.unlink(missing_ok=True)

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
        with patch('app.routes.main.date') as mock_date:
            mock_date.today.return_value = date(2026, 7, 15)
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            with patch('app.services.fy_cutover.datetime') as mock_fy_dt:
                mock_fy_dt.now.return_value = datetime(2026, 7, 15, tzinfo=timezone.utc)
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

        with patch('app.routes.main.date') as mock_date:
            mock_date.today.return_value = date(2026, 7, 15)
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            with patch('app.services.fy_cutover.datetime') as mock_fy_dt:
                mock_fy_dt.now.return_value = datetime(2026, 7, 15, tzinfo=timezone.utc)
                resp = client.get('/')
        assert b'time to transition' not in resp.data

    def test_changeover_reminder_hidden_outside_window(self, client):
        """Banner should NOT appear outside Jul-Aug."""
        with patch('app.routes.main.date') as mock_date:
            mock_date.today.return_value = date(2026, 9, 1)
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            resp = client.get('/')
        assert b'time to transition' not in resp.data

    def test_changeover_reminder_hidden_during_active_transition(self, client, app):
        """Banner should NOT appear if transition is already in progress."""
        with app.app_context():
            from app.services.fy_cutover import enter_transition_mode
            enter_transition_mode('FY27')

        with patch('app.routes.main.date') as mock_date:
            mock_date.today.return_value = date(2026, 7, 15)
            mock_date.side_effect = lambda *a, **k: date(*a, **k)
            resp = client.get('/')
        assert b'time to transition' not in resp.data

        # Clean up
        with app.app_context():
            from app.services.fy_cutover import exit_transition_mode
            exit_transition_mode()


class TestNavbarReorg:
    """Tests for navbar reorganization."""

    def test_notes_in_main_nav(self, client):
        """Notes should be accessible from the Browse dropdown."""
        resp = client.get('/')
        html = resp.data.decode()
        # Notes should be in the Browse dropdown
        assert '/notes' in html

    def test_customers_in_main_nav(self, client):
        """Customers should be accessible from the navbar."""
        resp = client.get('/')
        html = resp.data.decode()
        # Customers link should be in the navbar (Browse dropdown or top-level in DSS mode)
        assert '/customers' in html


class TestArchiveExplorer:
    """Tests for FY Archive Explorer API endpoints."""

    def _create_archive(self, app, label='FY25'):
        """Helper: create a minimal archive .db with test data."""
        import sqlite3
        from app.services.fy_cutover import _get_data_dir

        with app.app_context():
            data_dir = _get_data_dir()
            archive_path = data_dir / f'{label}.db'

            conn = sqlite3.connect(str(archive_path))
            c = conn.cursor()

            # Create minimal schema matching the app's models
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sellers (
                    id INTEGER PRIMARY KEY, name TEXT, alias TEXT, seller_type TEXT, territory_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS territories (
                    id INTEGER PRIMARY KEY, name TEXT, pod_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY, name TEXT, tpid INTEGER, nickname TEXT,
                    territory_id INTEGER, seller_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY, customer_id INTEGER, content TEXT,
                    call_date TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS engagements (
                    id INTEGER PRIMARY KEY, customer_id INTEGER, title TEXT,
                    status TEXT, technical_problem TEXT, business_impact TEXT,
                    solution_resources TEXT, estimated_acr REAL, key_individuals TEXT, target_date TEXT
                );
                CREATE TABLE IF NOT EXISTS milestones (
                    id INTEGER PRIMARY KEY, customer_id INTEGER, title TEXT,
                    msx_status TEXT, dollar_value REAL, due_date TEXT,
                    workload TEXT, monthly_usage REAL,
                    msx_milestone_id TEXT, milestone_number TEXT, url TEXT,
                    msx_status_code INTEGER, opportunity_name TEXT,
                    opportunity_id INTEGER, on_my_team INTEGER
                );
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY, customer_id INTEGER, name TEXT
                );
                CREATE TABLE IF NOT EXISTS msx_tasks (
                    id INTEGER PRIMARY KEY, milestone_id INTEGER, subject TEXT,
                    task_category TEXT, duration_minutes INTEGER, is_hok INTEGER,
                    msx_task_id TEXT, msx_task_url TEXT, description TEXT, due_date TEXT, note_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS topics (
                    id INTEGER PRIMARY KEY, name TEXT, description TEXT
                );
                CREATE TABLE IF NOT EXISTS notes_topics (
                    note_id INTEGER, topic_id INTEGER, PRIMARY KEY (note_id, topic_id)
                );
                CREATE TABLE IF NOT EXISTS notes_engagements (
                    note_id INTEGER, engagement_id INTEGER, PRIMARY KEY (note_id, engagement_id)
                );
                CREATE TABLE IF NOT EXISTS notes_milestones (
                    note_id INTEGER, milestone_id INTEGER, PRIMARY KEY (note_id, milestone_id)
                );
                CREATE TABLE IF NOT EXISTS partners (
                    id INTEGER PRIMARY KEY, name TEXT
                );
                CREATE TABLE IF NOT EXISTS notes_partners (
                    note_id INTEGER, partner_id INTEGER, PRIMARY KEY (note_id, partner_id)
                );
                CREATE TABLE IF NOT EXISTS verticals (
                    id INTEGER PRIMARY KEY, name TEXT
                );
                CREATE TABLE IF NOT EXISTS customers_verticals (
                    customer_id INTEGER, vertical_id INTEGER, PRIMARY KEY (customer_id, vertical_id)
                );

                -- Insert test data
                INSERT INTO sellers (id, name, alias) VALUES (1, 'Alice Seller', 'alice@ms.com');
                INSERT INTO territories (id, name) VALUES (1, 'West Region');
                INSERT INTO customers (id, name, tpid, seller_id, territory_id) VALUES (1, 'Acme Corp', 1001, 1, 1);
                INSERT INTO customers (id, name, tpid, seller_id, territory_id) VALUES (2, 'Globex Inc', 1002, 1, 1);
                INSERT INTO customers (id, name, tpid, seller_id) VALUES (3, 'No-Seller Co', 1003, NULL);
                INSERT INTO notes (id, customer_id, content, call_date) VALUES (1, 1, '<p>Discussed Azure migration</p>', '2025-01-15');
                INSERT INTO notes (id, customer_id, content, call_date) VALUES (2, 1, '<p>Follow-up on Cosmos DB</p>', '2025-02-10');
                INSERT INTO engagements (id, customer_id, title, status) VALUES (1, 1, 'Cloud Migration', 'Active');
                INSERT INTO milestones (id, customer_id, title, msx_status) VALUES (1, 1, 'POC Complete', 'Approved');
                INSERT INTO topics (id, name) VALUES (1, 'Azure');
                INSERT INTO notes_topics (note_id, topic_id) VALUES (1, 1);
                INSERT INTO notes_engagements (note_id, engagement_id) VALUES (1, 1);
                INSERT INTO notes_milestones (note_id, milestone_id) VALUES (1, 1);
                INSERT INTO msx_tasks (id, milestone_id, subject, task_category, duration_minutes) VALUES (1, 1, 'Review POC', 'Technical', 60);
                INSERT INTO verticals (id, name) VALUES (1, 'Healthcare');
                INSERT INTO customers_verticals (customer_id, vertical_id) VALUES (1, 1);
            """)
            conn.commit()
            conn.close()
            return archive_path

    def test_archive_tree_endpoint(self, client, app):
        """GET /api/admin/fy/archive/<label>/tree returns tree data."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/tree')
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'summary' in data
            assert 'sellers' in data
            assert 'unassigned' in data
            assert data['summary']['customers'] == 3
            assert data['summary']['sellers'] == 1
            assert data['summary']['notes'] == 2
            assert len(data['sellers']) == 1
            assert data['sellers'][0]['name'] == 'Alice Seller'
            assert len(data['sellers'][0]['customers']) == 2
            assert len(data['unassigned']) == 1
            assert data['unassigned'][0]['name'] == 'No-Seller Co'
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_tree_not_found(self, client):
        """GET /api/admin/fy/archive/<label>/tree returns 404 for missing archive."""
        resp = client.get('/api/admin/fy/archive/FY99/tree')
        assert resp.status_code == 404

    def test_archive_customer_endpoint(self, client, app):
        """GET /api/admin/fy/archive/<label>/customer/<id> returns customer detail."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/customer/1')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['name'] == 'Acme Corp'
            assert data['tpid'] == 1001
            assert data['territory'] == 'West Region'
            assert data['seller'] == 'Alice Seller'
            assert len(data['notes']) == 2
            assert len(data['engagements']) == 1
            assert data['engagements'][0]['title'] == 'Cloud Migration'
            assert len(data['milestones']) == 1
            assert data['milestones'][0]['title'] == 'POC Complete'
            assert 'Healthcare' in data['verticals']
            # Check note topics (notes ordered by call_date DESC, note 1 is second)
            note_with_topics = [n for n in data['notes'] if n['topics']]
            assert len(note_with_topics) == 1
            assert 'Azure' in note_with_topics[0]['topics']
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_customer_not_found(self, client, app):
        """GET customer endpoint returns 404 for missing customer."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/customer/999')
            assert resp.status_code == 404
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_detail_note(self, client, app):
        """GET /api/admin/fy/archive/<label>/detail/note/<id> returns note detail."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/detail/note/1')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['type'] == 'note'
            assert 'Azure migration' in data['content']
            assert data['customer_name'] == 'Acme Corp'
            assert 'Azure' in data['topics']
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_detail_engagement(self, client, app):
        """GET engagement detail returns engagement with linked notes."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/detail/engagement/1')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['type'] == 'engagement'
            assert data['title'] == 'Cloud Migration'
            assert len(data['linked_notes']) == 1
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_detail_milestone(self, client, app):
        """GET milestone detail returns milestone with tasks."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/detail/milestone/1')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['type'] == 'milestone'
            assert data['title'] == 'POC Complete'
            assert len(data['tasks']) == 1
            assert data['tasks'][0]['subject'] == 'Review POC'
            assert len(data['linked_notes']) == 1
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_detail_invalid_type(self, client, app):
        """GET detail with invalid type returns 400."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/detail/badtype/1')
            assert resp.status_code == 400
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_detail_not_found(self, client, app):
        """GET detail for missing item returns 404."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/api/admin/fy/archive/FY25/detail/note/999')
            assert resp.status_code == 404
        finally:
            archive_path.unlink(missing_ok=True)

    def test_archive_browse_button_in_admin_panel(self, client, app):
        """Admin panel archive list should include Browse button."""
        archive_path = self._create_archive(app, 'FY25')
        try:
            resp = client.get('/admin')
            html = resp.data.decode()
            assert 'openArchiveExplorer' in html
            assert 'archiveExplorerModal' in html
        finally:
            archive_path.unlink(missing_ok=True)


class TestNavbarReorgExtended:
    """Additional navbar reorganization tests."""

    def test_pods_in_more_menu(self, client):
        """PODs should be inside the More dropdown."""
        resp = client.get('/')
        html = resp.data.decode()
        # PODs should NOT have a top-level nav item anymore
        assert 'id="navPods"' not in html

    def test_partners_in_main_nav(self, client):
        """Partners should be accessible from the Browse dropdown."""
        resp = client.get('/')
        html = resp.data.decode()
        assert '/partners' in html

    def test_topics_in_more_dropdown(self, client):
        """Topics should be in the More dropdown menu."""
        resp = client.get('/')
        html = resp.data.decode()
        assert '/topics' in html


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
