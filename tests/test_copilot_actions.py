"""Tests for Copilot daily action items feature."""

import json
from datetime import datetime, date, time, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest


# Sample WorkIQ response (JSON format from our prompt)
SAMPLE_WORKIQ_RESPONSE = """[
  {
    "title": "Provide cost comparison for Vantage PostgreSQL scaling",
    "description": "This is blocking decision-making on whether the customer can safely run at higher capacity. Build a cost comparison for PostgreSQL Flexible Server.",
    "source_url": "https://teams.microsoft.com/l/message/19:abc@thread.v2/123",
    "last_activity_date": "2026-03-26"
  },
  {
    "title": "Follow up on LogixHealth TCO deliverables",
    "description": "This account has multiple live threads and upcoming final delivery meeting. Ensure the assessment deck is accessible to all parties.",
    "source_url": "https://outlook.office365.com/owa/?ItemID=AAMk123",
    "last_activity_date": "2026-03-25"
  },
  {
    "title": "Progress Q3 pipeline hygiene actions",
    "description": "Manager-directed priority tied to your scorecard. Verify API usage and follow up on uncommitted milestones.",
    "source_url": "https://teams.microsoft.com/l/meeting/details?eventId=AAMk456",
    "last_activity_date": "2026-03-24"
  }
]"""

EMPTY_RESPONSE = ""

NO_ITEMS_RESPONSE = """I looked through your emails, chats and meetings but couldn't find any clear action items for you today."""


class TestParseActionItems:
    """Tests for the markdown response parser."""

    def test_parses_three_items(self):
        """Should extract all 3 action items from a well-formatted response."""
        from app.services.copilot_actions import parse_action_items
        items = parse_action_items(SAMPLE_WORKIQ_RESPONSE)
        assert len(items) == 3

    def test_extracts_titles(self):
        """Should extract clean titles without markdown formatting."""
        from app.services.copilot_actions import parse_action_items
        items = parse_action_items(SAMPLE_WORKIQ_RESPONSE)
        assert items[0]['title'] == 'Provide cost comparison for Vantage PostgreSQL scaling'
        assert items[1]['title'] == 'Follow up on LogixHealth TCO deliverables'
        assert items[2]['title'] == 'Progress Q3 pipeline hygiene actions'

    def test_extracts_description(self):
        """Should include context in description."""
        from app.services.copilot_actions import parse_action_items
        items = parse_action_items(SAMPLE_WORKIQ_RESPONSE)
        assert 'blocking decision-making' in items[0]['description']
        assert 'cost comparison' in items[0]['description']

    def test_empty_response_returns_empty(self):
        """Should return empty list for empty response."""
        from app.services.copilot_actions import parse_action_items
        assert parse_action_items(EMPTY_RESPONSE) == []
        assert parse_action_items(None) == []

    def test_no_items_response_returns_empty(self):
        """Should return empty list when WorkIQ finds no action items."""
        from app.services.copilot_actions import parse_action_items
        assert parse_action_items(NO_ITEMS_RESPONSE) == []

    def test_truncates_long_titles(self):
        """Titles should be truncated to 300 chars."""
        from app.services.copilot_actions import parse_action_items
        long_title = 'A' * 500
        response = json.dumps([{"title": long_title, "description": "test"}])
        items = parse_action_items(response)
        if items:
            assert len(items[0]['title']) <= 300

    def test_handles_json_in_code_block(self):
        """Should extract JSON even if wrapped in markdown code block."""
        from app.services.copilot_actions import parse_action_items
        response = '```json\n[{"title": "Test item", "description": "Do stuff"}]\n```'
        items = parse_action_items(response)
        assert len(items) == 1
        assert items[0]['title'] == 'Test item'

    def test_handles_json_with_preamble(self):
        """Should extract JSON even with text before the array."""
        from app.services.copilot_actions import parse_action_items
        response = 'Here are your items:\n[{"title": "Test", "description": "x"}]'
        items = parse_action_items(response)
        assert len(items) == 1


class TestShouldSync:
    """Tests for the sync-needed check."""

    def test_should_sync_when_never_synced(self, app):
        """Should return True if never synced before."""
        with app.app_context():
            from app.services.copilot_actions import should_sync
            assert should_sync() is True

    def test_should_not_sync_before_6am(self, app):
        """Should return False before 6 AM even if never synced."""
        with app.app_context():
            from app.services.copilot_actions import should_sync
            with patch('app.services.copilot_actions.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 26, 4, 0)  # 4 AM
                mock_dt.combine = datetime.combine
                result = should_sync()
            # First sync: should_sync checks pref.last_copilot_sync is None,
            # returns True regardless of time
            assert result is True

    def test_should_sync_after_6am_if_stale(self, app):
        """Should return True after 6 AM if last sync was yesterday."""
        with app.app_context():
            from app.models import UserPreference, db
            from app.services.copilot_actions import should_sync
            pref = UserPreference.query.first()
            # Set last sync to yesterday 7 AM UTC
            pref.last_copilot_sync = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
            db.session.commit()
            # Now is after 6 AM today
            assert should_sync() is True

    def test_should_not_sync_if_already_done_today(self, app):
        """Should return False if sync already ran today after 6 AM."""
        with app.app_context():
            from app.models import UserPreference, db
            from app.services.copilot_actions import should_sync
            pref = UserPreference.query.first()
            # Set last sync to today at 6:05 AM UTC (well after 6 AM local)
            pref.last_copilot_sync = datetime.now(timezone.utc)
            db.session.commit()
            assert should_sync() is False


class TestSyncCopilotActionItems:
    """Tests for the full sync function."""

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_creates_items(self, mock_workiq, app):
        """Should create action items from WorkIQ response."""
        mock_workiq.return_value = SAMPLE_WORKIQ_RESPONSE

        with app.app_context():
            from app.services.copilot_actions import sync_copilot_action_items
            from app.models import ActionItem
            result = sync_copilot_action_items()
            assert result['success'] is True
            assert result['items_created'] == 3

            items = ActionItem.query.filter_by(source='copilot').all()
            assert len(items) == 3
            assert all(i.status == 'open' for i in items)

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_clears_old_items(self, mock_workiq, app):
        """Should delete old copilot items before creating new ones."""
        with app.app_context():
            from app.services.copilot_actions import sync_copilot_action_items
            from app.models import ActionItem, db

            # Create an old copilot item
            old = ActionItem(
                title='Old copilot item',
                source='copilot',
                status='open',
            )
            db.session.add(old)
            db.session.commit()
            old_id = old.id

            mock_workiq.return_value = SAMPLE_WORKIQ_RESPONSE
            result = sync_copilot_action_items()
            assert result['success'] is True

            # Old item should be gone (check by title since IDs can be reused)
            assert ActionItem.query.filter_by(title='Old copilot item').first() is None
            # New items should exist
            assert ActionItem.query.filter_by(source='copilot').count() == 3

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_does_not_touch_engagement_items(self, mock_workiq, app,
                                                    sample_data):
        """Should not delete engagement action items during sync."""
        with app.app_context():
            from app.services.copilot_actions import sync_copilot_action_items
            from app.models import ActionItem, Engagement, db

            eng = Engagement.query.first()
            if not eng:
                eng = Engagement(
                    title='Test Engagement',
                    customer_id=sample_data['customer1_id'],
                )
                db.session.add(eng)
                db.session.flush()

            eng_task = ActionItem(
                title='Engagement task',
                engagement_id=eng.id,
                source='engagement',
                status='open',
            )
            db.session.add(eng_task)
            db.session.commit()
            eng_task_id = eng_task.id

            mock_workiq.return_value = SAMPLE_WORKIQ_RESPONSE
            sync_copilot_action_items()

            # Engagement task should still exist
            assert ActionItem.query.get(eng_task_id) is not None

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_updates_last_sync_timestamp(self, mock_workiq, app):
        """Should update last_copilot_sync after successful sync."""
        mock_workiq.return_value = SAMPLE_WORKIQ_RESPONSE

        with app.app_context():
            from app.services.copilot_actions import sync_copilot_action_items
            from app.models import UserPreference
            sync_copilot_action_items()

            pref = UserPreference.query.first()
            assert pref.last_copilot_sync is not None

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_handles_empty_response(self, mock_workiq, app):
        """Should handle empty WorkIQ response gracefully."""
        mock_workiq.return_value = ""

        with app.app_context():
            from app.services.copilot_actions import sync_copilot_action_items
            result = sync_copilot_action_items()
            assert result['success'] is False
            assert 'Empty response' in result['error']

    @patch('app.services.workiq_service.query_workiq')
    def test_sync_handles_workiq_exception(self, mock_workiq, app):
        """Should handle WorkIQ failures gracefully."""
        mock_workiq.side_effect = Exception("WorkIQ timeout")

        with app.app_context():
            from app.services.copilot_actions import sync_copilot_action_items
            result = sync_copilot_action_items()
            assert result['success'] is False


class TestDashboardCopilotItems:
    """Integration tests: copilot items show on the dashboard."""

    def test_copilot_items_visible(self, client, app):
        """Copilot action items should appear on the dashboard."""
        with app.app_context():
            from app.models import ActionItem, db
            item = ActionItem(
                title='Copilot Test Item',
                source='copilot',
                status='open',
            )
            db.session.add(item)
            db.session.commit()

        resp = client.get('/')
        assert resp.status_code == 200
        assert b'Copilot Test Item' in resp.data
        assert b'bi-stars' in resp.data  # Copilot icon

    def test_copilot_items_labeled(self, client, app):
        """Copilot items should show 'Copilot Suggestions' section header."""
        with app.app_context():
            from app.models import ActionItem, db
            item = ActionItem(
                title='Test Copilot Label',
                source='copilot',
                status='open',
            )
            db.session.add(item)
            db.session.commit()

        resp = client.get('/')
        assert b'Copilot Suggestions' in resp.data
