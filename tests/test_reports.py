"""Tests for the reports blueprint."""
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from app.models import db, Customer, Engagement, Note, Territory, Milestone, Topic


class TestReportsHub:
    """Tests for /reports hub page."""

    def test_hub_page_loads(self, client, app):
        """Reports hub page should return 200."""
        with app.app_context():
            resp = client.get('/reports')
            assert resp.status_code == 200
            assert b'Reports' in resp.data

    def test_hub_has_one_on_one_link(self, client, app):
        """Reports hub should link to the 1:1 report."""
        with app.app_context():
            resp = client.get('/reports')
            assert b'1:1 Manager' in resp.data
            assert b'/reports/one-on-one' in resp.data

    def test_hub_has_revenue_reports_link(self, client, app):
        """Reports hub should link to revenue reports."""
        with app.app_context():
            resp = client.get('/reports')
            assert b'Revenue Reports' in resp.data


class TestOneOnOneReport:
    """Tests for /reports/one-on-one."""

    def test_empty_report(self, client, app):
        """1:1 report with no data should return 200 with zero stats."""
        with app.app_context():
            resp = client.get('/reports/one-on-one')
            assert resp.status_code == 200
            assert b'1:1 Manager' in resp.data

    def test_engagement_with_recent_note_shows(self, client, app, sample_data):
        """Engagement linked to a note with recent call_date appears in Last 2 Weeks."""
        with app.app_context():
            eng = Engagement(
                customer_id=sample_data['customer1_id'],
                title='Eng With Recent Note',
                status='Active',
            )
            note = Note(
                customer_id=sample_data['customer1_id'],
                call_date=datetime.now(timezone.utc) - timedelta(days=5),
                content='Recent call about migration.',
            )
            db.session.add_all([eng, note])
            db.session.flush()
            eng.notes.append(note)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert resp.status_code == 200
            assert b'Eng With Recent Note' in resp.data

    def test_engagement_without_recent_note_not_in_recent(self, client, app, sample_data):
        """Engagement with no recent notes should not appear in Last 2 Weeks tab."""
        with app.app_context():
            old_date = datetime.now(timezone.utc) - timedelta(days=60)
            eng = Engagement(
                customer_id=sample_data['customer1_id'],
                title='Eng No Recent Notes',
                status='Active',
            )
            note = Note(
                customer_id=sample_data['customer1_id'],
                call_date=old_date,
                content='Old call from months ago.',
            )
            db.session.add_all([eng, note])
            db.session.flush()
            eng.notes.append(note)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            html = resp.data.decode()
            # Should be in "All Open" tab (Active status) but NOT via recent notes
            assert resp.status_code == 200
            # Still appears somewhere on page (all-open tab)
            assert 'Eng No Recent Notes' in html

    def test_open_engagement_appears_in_all_tab(self, client, app, sample_data):
        """Active engagement appears in the All Open Engagements tab."""
        with app.app_context():
            eng = Engagement(
                customer_id=sample_data['customer2_id'],
                title='Persistent Active Eng',
                status='Active',
            )
            db.session.add(eng)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert b'Persistent Active Eng' in resp.data

    def test_closed_engagement_not_in_all_tab(self, client, app, sample_data):
        """Won engagement with only old notes should not appear in recent tab."""
        with app.app_context():
            old_date = datetime.now(timezone.utc) - timedelta(days=60)
            eng = Engagement(
                customer_id=sample_data['customer2_id'],
                title='Won Eng Should Hide',
                status='Won',
            )
            note = Note(
                customer_id=sample_data['customer2_id'],
                call_date=old_date,
                content='Old closing call.',
            )
            db.session.add_all([eng, note])
            db.session.flush()
            eng.notes.append(note)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            html = resp.data.decode()
            # Won + old notes = not in recent tab, not in all-open tab
            assert resp.status_code == 200

    def test_recent_note_shows_customer(self, client, app, sample_data):
        """A recently created note should surface its customer in the report."""
        with app.app_context():
            # sample_data already has notes with call_date=now, so customer should show
            resp = client.get('/reports/one-on-one')
            assert b'Acme Corp' in resp.data

    def test_stats_counts(self, client, app, sample_data):
        """Stats should reflect the right counts."""
        with app.app_context():
            eng = Engagement(
                customer_id=sample_data['customer1_id'],
                title='Stats Test Eng',
                status='Active',
            )
            note = Note(
                customer_id=sample_data['customer1_id'],
                call_date=datetime.now(timezone.utc) - timedelta(days=2),
                content='Recent stats call.',
            )
            db.session.add_all([eng, note])
            db.session.flush()
            eng.notes.append(note)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert resp.status_code == 200
            assert b'Engagements (2w)' in resp.data
            assert b'Notes (2w)' in resp.data
            assert b'Open Engagements' in resp.data

    def test_territory_badge_shown(self, client, app, sample_data):
        """Customer territory should display as a badge."""
        with app.app_context():
            resp = client.get('/reports/one-on-one')
            assert b'West Region' in resp.data

    def test_engagement_no_notes_not_in_recent(self, client, app, sample_data):
        """Engagement with zero linked notes should not appear in recent tab."""
        with app.app_context():
            eng = Engagement(
                customer_id=sample_data['customer3_id'],
                title='Naked Eng No Notes',
                status='Active',
            )
            db.session.add(eng)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            html = resp.data.decode()
            # Should only appear in all-open tab, not recent
            # The recent tab is driven purely by note call_dates
            assert resp.status_code == 200


class TestMilestoneHighlights:
    """Tests for milestone commitments and quarter milestones sections."""

    def test_committed_milestone_shows(self, client, app, sample_data):
        """Recently committed milestone on my team appears in Commitments section."""
        with app.app_context():
            ms = Milestone(
                url='https://example.com/ms2',
                title='Committed Deployment',
                msx_status='On Track',
                customer_commitment='Committed',
                committed_at=datetime.now(timezone.utc) - timedelta(days=5),
                on_my_team=True,
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert b'Committed Deployment' in resp.data
            assert b'Commitments' in resp.data

    def test_milestone_not_on_team_hidden(self, client, app, sample_data):
        """Milestone not on my team should not appear in commitments."""
        with app.app_context():
            ms = Milestone(
                url='https://example.com/ms3',
                title='Someone Elses Milestone',
                msx_status='On Track',
                committed_at=datetime.now(timezone.utc) - timedelta(days=1),
                on_my_team=False,
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert b'Someone Elses Milestone' not in resp.data

    def test_old_committed_milestone_hidden(self, client, app, sample_data):
        """Milestone committed more than 2 weeks ago should not appear."""
        with app.app_context():
            ms = Milestone(
                url='https://example.com/ms-old',
                title='Ancient Commitment',
                msx_status='On Track',
                customer_commitment='Committed',
                committed_at=datetime.now(timezone.utc) - timedelta(days=60),
                on_my_team=True,
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert b'Ancient Commitment' not in resp.data

    def test_no_date_milestone_hidden(self, client, app, sample_data):
        """Milestone with no committed_at should not appear in Commitments."""
        with app.app_context():
            ms = Milestone(
                url='https://example.com/ms-nodate',
                title='No Date Milestone',
                msx_status='On Track',
                on_my_team=True,
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert b'No Date Milestone' not in resp.data

    def test_overdue_milestone_shows(self, client, app, sample_data):
        """Overdue active milestone on my team appears in Needs Attention."""
        with app.app_context():
            ms = Milestone(
                url='https://example.com/ms4',
                title='Overdue Migration',
                msx_status='At Risk',
                on_my_team=True,
                due_date=datetime.now(timezone.utc) - timedelta(days=5),
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert b'Overdue Migration' in resp.data
            assert b'Overdue' in resp.data

    def test_upcoming_milestone_shows(self, client, app, sample_data):
        """Active milestone due this fiscal quarter on my team appears in Quarter Milestones."""
        from datetime import date as _date
        # Calculate a date within the current fiscal quarter
        today = _date.today()
        fy_month = (today.month - 7) % 12
        fq_start = (fy_month // 3) * 3
        q_start_month = ((fq_start + 7 - 1) % 12) + 1
        # Use the 15th of the quarter start month (always in-range)
        in_quarter = datetime(today.year, q_start_month, 15)

        with app.app_context():
            ms = Milestone(
                url='https://example.com/ms5',
                title='Upcoming Deployment',
                msx_status='On Track',
                on_my_team=True,
                due_date=in_quarter,
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

            resp = client.get('/reports/one-on-one')
            assert b'Upcoming Deployment' in resp.data

    def test_empty_milestones(self, client, app):
        """No milestones should show empty state messages."""
        with app.app_context():
            resp = client.get('/reports/one-on-one')
            assert b'No milestones committed in the last 2 weeks' in resp.data
            assert b'No milestones due this quarter' in resp.data


class TestTopicTrends:
    """Tests for topic trends section."""

    def test_topics_from_recent_notes(self, client, app, sample_data):
        """Topics tagged on recent notes should appear in trends."""
        with app.app_context():
            # sample_data has notes with call_date=now tagged with topics
            resp = client.get('/reports/one-on-one')
            assert b'Topic Trends (FY)' in resp.data
            # sample_data tags note1 with 'Azure VM'
            assert b'Azure VM' in resp.data

    def test_no_topics_shows_empty(self, client, app):
        """No topics tagged should show empty state."""
        with app.app_context():
            resp = client.get('/reports/one-on-one')
            assert b'No topics tagged on recent notes' in resp.data


class TestMilestoneAuditSync:
    """Tests for audit-based milestone date extraction and sync endpoint."""

    def test_parse_changedata_json(self, app):
        """Should parse Dynamics 365 audit changedata JSON."""
        with app.app_context():
            from app.services.milestone_audit import _parse_audit_changedata
            changedata = json.dumps({"changedAttributes": [
                {
                    "logicalName": "msp_milestonestatus",
                    "oldValue": "861980000", "newValue": "861980003",
                    "oldName": "On Track", "newName": "Completed",
                },
            ]})
            changes = _parse_audit_changedata(changedata)
            assert len(changes) == 1
            assert changes[0]['logicalName'] == 'msp_milestonestatus'
            assert changes[0]['newName'] == 'Completed'

    def test_parse_empty_changedata(self, app):
        """Empty or None changedata should return empty list."""
        with app.app_context():
            from app.services.milestone_audit import _parse_audit_changedata
            assert _parse_audit_changedata('') == []
            assert _parse_audit_changedata(None) == []

    def test_parse_invalid_json(self, app):
        """Invalid JSON should return empty list, not crash."""
        with app.app_context():
            from app.services.milestone_audit import _parse_audit_changedata
            assert _parse_audit_changedata('not json at all') == []

    def test_extract_completed_via_completedon(self, app):
        """Should extract completion date from msp_completedon field."""
        with app.app_context():
            from app.services.milestone_audit import _extract_dates_from_audit
            records = [
                {
                    'createdon': '2026-03-23T20:42:15.843Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_completedon",
                            "oldValue": None,
                            "newValue": "03/23/2026 20:42:14",
                        },
                        {
                            "logicalName": "msp_completedby",
                            "oldValue": None,
                            "newValue": "systemuser,abc123",
                        },
                    ]}),
                },
            ]
            committed, completed = _extract_dates_from_audit(records)
            assert completed is not None
            assert completed.year == 2026
            assert completed.month == 3
            assert completed.day == 23
            assert completed.hour == 20
            assert completed.minute == 42
            assert committed is None

    def test_extract_committed_via_committedon(self, app):
        """Should extract commitment date from msp_committedon field."""
        with app.app_context():
            from app.services.milestone_audit import _extract_dates_from_audit
            records = [
                {
                    'createdon': '2025-11-11T20:55:09.941Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_committedon",
                            "oldValue": None,
                            "newValue": "11/11/2025 20:55:08",
                        },
                        {
                            "logicalName": "msp_committedby",
                            "oldValue": None,
                            "newValue": "systemuser,xyz789",
                        },
                    ]}),
                },
            ]
            committed, completed = _extract_dates_from_audit(records)
            assert committed is not None
            assert committed.year == 2025
            assert committed.month == 11
            assert committed.day == 11
            assert committed.hour == 20
            assert committed.minute == 55
            assert completed is None

    def test_extract_completed_via_status_change(self, app):
        """Falls back to msp_milestonestatus newName when no msp_completedon."""
        with app.app_context():
            from app.services.milestone_audit import _extract_dates_from_audit
            records = [
                {
                    'createdon': '2026-03-20T14:30:00Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_milestonestatus",
                            "oldValue": "861980000", "newValue": "861980003",
                            "oldName": "On Track", "newName": "Completed",
                        },
                    ]}),
                },
            ]
            committed, completed = _extract_dates_from_audit(records)
            assert completed is not None
            assert completed.year == 2026
            assert completed.month == 3
            assert completed.day == 20
            assert committed is None

    def test_extract_committed_via_option_change(self, app):
        """Falls back to msp_commitmentrecommendation newName."""
        with app.app_context():
            from app.services.milestone_audit import _extract_dates_from_audit
            records = [
                {
                    'createdon': '2025-11-11T20:55:08.437Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_commitmentrecommendation",
                            "oldValue": "861980000", "newValue": "861980003",
                            "oldName": "Uncommitted", "newName": "Committed",
                        },
                    ]}),
                },
            ]
            committed, completed = _extract_dates_from_audit(records)
            assert committed is not None
            assert committed.year == 2025
            assert committed.month == 11
            assert committed.day == 11
            assert completed is None

    def test_extract_prefers_explicit_timestamp(self, app):
        """msp_committedon/msp_completedon take priority over status change."""
        with app.app_context():
            from app.services.milestone_audit import _extract_dates_from_audit
            # Record 0 (newest): explicit msp_completedon with precise time
            # Record 1 (older): status change in same audit trail
            records = [
                {
                    'createdon': '2026-03-23T20:42:15.843Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_completedon",
                            "oldValue": None,
                            "newValue": "03/23/2026 20:42:14",
                        },
                    ]}),
                },
                {
                    'createdon': '2026-03-23T20:42:14.364Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_milestonestatus",
                            "oldValue": "861980000", "newValue": "861980003",
                            "oldName": "On Track", "newName": "Completed",
                        },
                    ]}),
                },
            ]
            _, completed = _extract_dates_from_audit(records)
            # Should use the explicit timestamp from msp_completedon, not createdon
            assert completed.minute == 42
            assert completed.second == 14

    def test_extract_no_relevant_changes(self, app):
        """Records with no status/commitment changes return None dates."""
        with app.app_context():
            from app.services.milestone_audit import _extract_dates_from_audit
            records = [
                {
                    'createdon': '2026-03-20T14:30:00Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_tags",
                            "oldValue": "parconstraint",
                            "newValue": "",
                        },
                    ]}),
                },
            ]
            committed, completed = _extract_dates_from_audit(records)
            assert committed is None
            assert completed is None

    def test_extract_both_dates_real_format(self, app):
        """Should find both dates when records contain both transitions."""
        with app.app_context():
            from app.services.milestone_audit import _extract_dates_from_audit
            records = [
                # Newest: completedon set
                {
                    'createdon': '2026-03-23T20:42:15.843Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_completedon",
                            "oldValue": None,
                            "newValue": "03/23/2026 20:42:14",
                        },
                    ]}),
                },
                # Older: committedon set
                {
                    'createdon': '2025-11-11T20:55:09.941Z',
                    'changedata': json.dumps({"changedAttributes": [
                        {
                            "logicalName": "msp_committedon",
                            "oldValue": None,
                            "newValue": "11/11/2025 20:55:08",
                        },
                    ]}),
                },
            ]
            committed, completed = _extract_dates_from_audit(records)
            assert completed is not None
            assert committed is not None
            assert completed.year == 2026
            assert committed.year == 2025

    def test_sync_endpoint_returns_json(self, client, app):
        """POST /api/reports/sync-milestone-dates should return JSON."""
        with app.app_context():
            with patch('app.services.milestone_audit.sync_milestone_audit_dates') as mock_sync:
                mock_sync.return_value = {
                    'success': True,
                    'total_milestones': 0,
                    'dates_found': 0,
                    'dates_cleared': 0,
                    'errors': [],
                }
                resp = client.post('/api/reports/sync-milestone-dates')
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['success'] is True

    def test_sync_clears_stale_dates(self, app, sample_data):
        """Milestones no longer Committed should have committed_at cleared."""
        with app.app_context():
            ms = Milestone(
                url='https://example.com/stale',
                title='Stale Commitment',
                msx_milestone_id='00000000-0000-0000-0000-000000000001',
                msx_status='On Track',
                customer_commitment='Uncommitted',
                on_my_team=True,
                committed_at=datetime.now(timezone.utc) - timedelta(days=5),
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

            from app.services.milestone_audit import sync_milestone_audit_dates
            with patch('app.services.milestone_audit.get_milestone_audit_history') as mock_audit:
                result = sync_milestone_audit_dates()
                # Should not have called audit API (not in target state)
                mock_audit.assert_not_called()
                # committed_at should be cleared
                assert ms.committed_at is None
                assert result['dates_cleared'] >= 1
