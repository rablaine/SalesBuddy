"""
Tests for WorkIQ integration features (Issue #11).

Covers:
- Custom meeting summary prompt in user preferences
- WorkIQ prompt API endpoint
- Meeting summary API with custom prompt passthrough
- Scheduled milestone sync configuration
- AI feature auto-hide when env vars missing
"""
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from app import db
from app.models import UserPreference


# =============================================================================
# WorkIQ Prompt Preferences
# =============================================================================

class TestWorkiqPromptPreferences:
    """Tests for custom WorkIQ summary prompt in user preferences."""

    def test_settings_page_shows_prompt_section(self, client, app):
        """Settings page should display the WorkIQ prompt textarea."""
        response = client.get('/preferences')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'WorkIQ' in html
        assert 'workiqPrompt' in html
        assert 'Meeting Summary Prompt' in html

    def test_settings_page_shows_default_prompt(self, client, app):
        """Settings page should display the default prompt when no custom prompt is set."""
        from app.services.workiq_service import DEFAULT_SUMMARY_PROMPT
        response = client.get('/preferences')
        html = response.data.decode()
        assert 'Summarize the meeting' in html

    def test_save_custom_prompt(self, client, app):
        """Saving a custom prompt via API should persist it."""
        custom_prompt = 'Give me a brief summary of {title} {date} focusing on action items only.'
        response = client.post('/api/preferences/workiq-prompt',
                               json={'workiq_summary_prompt': custom_prompt},
                               content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['workiq_summary_prompt'] == custom_prompt

        # Verify it persisted
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.workiq_summary_prompt == custom_prompt

    def test_reset_prompt_to_default(self, client, app):
        """Sending empty string should reset prompt to null (default)."""
        # First set a custom prompt
        client.post('/api/preferences/workiq-prompt',
                     json={'workiq_summary_prompt': 'custom prompt'},
                     content_type='application/json')

        # Reset by sending empty string
        response = client.post('/api/preferences/workiq-prompt',
                               json={'workiq_summary_prompt': ''},
                               content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['workiq_summary_prompt'] is None

        # Verify in database
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.workiq_summary_prompt is None

    def test_save_prompt_creates_preference_if_missing(self, client, app):
        """Saving prompt should create UserPreference record if one doesn't exist."""
        with app.app_context():
            from app.models import db, UserPreference
            # Delete all preferences to simulate fresh state
            UserPreference.query.delete()
            db.session.commit()

        response = client.post('/api/preferences/workiq-prompt',
                               json={'workiq_summary_prompt': 'test prompt'},
                               content_type='application/json')
        assert response.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref is not None
            assert pref.workiq_summary_prompt == 'test prompt'

        # Cleanup: restore default preference
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.workiq_summary_prompt = None
            db.session.commit()


# =============================================================================
# WorkIQ Service - Custom Prompt Support
# =============================================================================

class TestWorkiqServiceCustomPrompt:
    """Tests for custom prompt support in workiq_service.py."""

    def test_default_summary_prompt_constant_exists(self):
        """DEFAULT_SUMMARY_PROMPT should be defined and contain placeholders."""
        from app.services.workiq_service import DEFAULT_SUMMARY_PROMPT
        assert '{title}' in DEFAULT_SUMMARY_PROMPT
        assert '{date}' in DEFAULT_SUMMARY_PROMPT
        assert 'Summarize' in DEFAULT_SUMMARY_PROMPT

    @patch('app.services.workiq_service.query_workiq')
    def test_get_meeting_summary_uses_default_prompt(self, mock_query):
        """When no custom prompt is given, should use the default prompt."""
        mock_query.return_value = 'SUMMARY:\nTest summary text.\n\nTECHNOLOGIES:\nAzure\n\nACTION_ITEMS:\nFollow up'
        from app.services.workiq_service import get_meeting_summary

        get_meeting_summary('Team Standup', '2025-01-15')
        call_args = mock_query.call_args[0][0]
        assert 'Summarize' in call_args
        assert 'Team Standup' in call_args

    @patch('app.services.workiq_service.query_workiq')
    def test_get_meeting_summary_uses_custom_prompt(self, mock_query):
        """When a custom prompt is provided, should use it."""
        mock_query.return_value = 'Custom summary response.'
        from app.services.workiq_service import get_meeting_summary

        custom = 'Give me bullet points for {title} {date}'
        get_meeting_summary('Team Standup', '2025-01-15', custom_prompt=custom)
        call_args = mock_query.call_args[0][0]
        assert 'bullet points' in call_args
        assert 'Team Standup' in call_args
        assert 'on 2025-01-15' in call_args

    @patch('app.services.workiq_service.query_workiq')
    def test_get_meeting_summary_bad_placeholders_fallback(self, mock_query):
        """Custom prompt with invalid placeholders should fall back to default."""
        mock_query.return_value = 'Fallback summary.'
        from app.services.workiq_service import get_meeting_summary

        bad_prompt = 'Summarize {title} {date} {nonexistent_placeholder}'
        get_meeting_summary('Team Standup', '2025-01-15', custom_prompt=bad_prompt)
        call_args = mock_query.call_args[0][0]
        # Should have fallen back to default
        assert 'Summarize' in call_args
        assert 'Team Standup' in call_args

    @patch('app.services.workiq_service.query_workiq')
    def test_get_meeting_summary_empty_custom_prompt_uses_default(self, mock_query):
        """Empty custom prompt should use default."""
        mock_query.return_value = 'Default response.'
        from app.services.workiq_service import get_meeting_summary

        get_meeting_summary('Team Standup', '2025-01-15', custom_prompt='   ')
        call_args = mock_query.call_args[0][0]
        assert 'Summarize' in call_args


# =============================================================================
# Meeting Summary API - Custom Prompt Passthrough
# =============================================================================

class TestMeetingSummaryAPIPrompt:
    """Tests for custom prompt parameter on the meeting summary API."""

    @patch('app.services.workiq_service.query_workiq')
    def test_api_accepts_prompt_parameter(self, mock_query, client, app):
        """API should pass through custom prompt to get_meeting_summary."""
        mock_query.return_value = 'SUMMARY:\nTest summary.\n\nTECHNOLOGIES:\nAzure\n\nACTION_ITEMS:\nNone'

        response = client.get('/api/meetings/summary?title=Standup&date=2025-01-15&prompt=Custom+prompt+{title}+{date}')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        # Verify the custom prompt was used
        call_args = mock_query.call_args[0][0]
        assert 'Custom prompt' in call_args

    @patch('app.services.workiq_service.query_workiq')
    def test_api_works_without_prompt_parameter(self, mock_query, client, app):
        """API should work fine when no prompt parameter is provided."""
        mock_query.return_value = 'SUMMARY:\nTest summary.\n\nTECHNOLOGIES:\nAzure\n\nACTION_ITEMS:\nNone'

        response = client.get('/api/meetings/summary?title=Standup')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True


# =============================================================================
# Scheduled Milestone Sync
# =============================================================================

class TestScheduledSync:
    """Tests for in-process milestone sync scheduler."""

    def test_ensure_sync_time_assigns_random_time(self, app):
        """_ensure_sync_time should assign a 5-min slot between 9:30 and 16:25."""
        from app.services.scheduled_sync import _ensure_sync_time

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_sync_hour = None
            pref.milestone_sync_minute = None
            db.session.commit()

            hour, minute = _ensure_sync_time(pref)
            total = hour * 60 + minute
            assert 9 * 60 + 30 <= total <= 16 * 60 + 25
            assert minute % 5 == 0  # must be a 5-minute slot
            assert pref.milestone_sync_hour == hour
            assert pref.milestone_sync_minute == minute

    def test_ensure_sync_time_preserves_existing(self, app):
        """_ensure_sync_time should not change an already-assigned time."""
        from app.services.scheduled_sync import _ensure_sync_time

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_sync_hour = 14
            pref.milestone_sync_minute = 15
            db.session.commit()

            hour, minute = _ensure_sync_time(pref)
            assert hour == 14
            assert minute == 15

    def test_should_sync_true_when_never_synced_on_sync_day(self, app):
        """_should_sync returns True on MWF if past sync time and never synced."""
        from app.services.scheduled_sync import _should_sync

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_sync_hour = 0
            pref.milestone_sync_minute = 0
            pref.last_milestone_sync = None
            db.session.commit()

            # Mock today as a Monday (weekday 0)
            with patch('app.services.scheduled_sync.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 23, 12, 0)  # Monday
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                assert _should_sync(pref) is True

    def test_should_sync_false_on_non_sync_day(self, app):
        """_should_sync returns False on Tue/Thu/Sat/Sun."""
        from app.services.scheduled_sync import _should_sync

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_sync_hour = 0
            pref.milestone_sync_minute = 0
            pref.last_milestone_sync = None
            db.session.commit()

            # Mock today as a Tuesday (weekday 1)
            with patch('app.services.scheduled_sync.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 24, 12, 0)  # Tuesday
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                assert _should_sync(pref) is False

    def test_should_sync_false_before_sync_time(self, app):
        """_should_sync returns False when we haven't reached today's time."""
        from app.services.scheduled_sync import _should_sync

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_sync_hour = 16
            pref.milestone_sync_minute = 25
            pref.last_milestone_sync = None
            db.session.commit()

            # Monday at 9:00 AM, sync scheduled for 4:25 PM
            with patch('app.services.scheduled_sync.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 23, 9, 0)  # Monday 9 AM
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                assert _should_sync(pref) is False

    def test_startup_catchup_skips_when_disabled(self, app):
        """start_milestone_sync_background should skip when auto_sync is off."""
        from app.services.scheduled_sync import start_milestone_sync_background

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_auto_sync = False
            db.session.commit()

        # Should not raise or start any threads
        with patch('app.services.scheduled_sync._run_sync') as mock_run:
            start_milestone_sync_background(app)
            mock_run.assert_not_called()

    def test_missed_sync_catches_up_on_non_sync_day(self, app):
        """Startup on Tuesday should catch up Monday's missed sync."""
        from app.services.scheduled_sync import _missed_sync

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_sync_hour = 10
            pref.milestone_sync_minute = 0
            pref.last_milestone_sync = None  # never synced
            db.session.commit()

            # Tuesday noon - Monday's 10:00 AM sync was missed
            with patch('app.services.scheduled_sync.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 24, 12, 0)  # Tuesday
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                assert _missed_sync(pref) is True

    def test_missed_sync_false_when_already_synced(self, app):
        """No catchup needed if last sync was after the most recent sync day."""
        from app.services.scheduled_sync import _missed_sync

        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_sync_hour = 10
            pref.milestone_sync_minute = 0
            # Synced Monday at 10:05 AM UTC
            pref.last_milestone_sync = datetime(2026, 3, 23, 15, 5, 0,
                                                tzinfo=timezone.utc)
            db.session.commit()

            # Tuesday noon - Monday sync already ran
            with patch('app.services.scheduled_sync.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 3, 24, 12, 0)  # Tuesday
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                assert _missed_sync(pref) is False


# =============================================================================
# AI buttons always visible (AI is always enabled)
# =============================================================================

class TestAIAlwaysEnabled:
    """Tests that AI UI elements are always visible (AI is always on)."""

    def test_note_form_always_shows_ai_buttons(self, client, app, sample_data):
        """Call log form should always show AI buttons."""
        with app.app_context():
            from app.models import Customer
            customer = Customer.query.first()
            customer_id = customer.id

        response = client.get(f'/note/new?customer_id={customer_id}')
        html = response.data.decode()
        assert 'id="aiSuggestBtn"' in html
        assert 'id="aiMatchMilestoneBtn"' in html
        assert 'Auto-tag with AI' in html


# =============================================================================
# WorkIQ UI Elements - Un-hidden
# =============================================================================

class TestWorkiqUIElements:
    """Tests that WorkIQ UI elements are visible when expected."""

    def test_new_note_shows_autofill_button(self, client, app, sample_data):
        """New call log form should show the Auto-fill button."""
        with app.app_context():
            from app.models import Customer
            customer = Customer.query.first()
            customer_id = customer.id

        response = client.get(f'/note/new?customer_id={customer_id}')
        html = response.data.decode()
        assert 'Auto-fill' in html
        assert 'autoFillBtn' in html

    def test_new_note_shows_import_meeting_button(self, client, app, sample_data):
        """New call log form should show the Import Summary button."""
        with app.app_context():
            from app.models import Customer
            customer = Customer.query.first()
            customer_id = customer.id

        response = client.get(f'/note/new?customer_id={customer_id}')
        html = response.data.decode()
        assert 'Import Summary' in html
        assert 'importMeetingBtn' in html

    def test_new_note_shows_prompt_customization(self, client, app, sample_data):
        """New call log form should include the prompt customization section."""
        with app.app_context():
            from app.models import Customer
            customer = Customer.query.first()
            customer_id = customer.id

        response = client.get(f'/note/new?customer_id={customer_id}')
        html = response.data.decode()
        assert 'meetingCustomPrompt' in html
        assert 'Customize summary prompt' in html

    def test_edit_note_hides_autofill_but_shows_import(self, client, app, sample_data):
        """Edit call log form hides auto-fill but keeps Import Summary."""
        with app.app_context():
            from app.models import Note
            note = Note.query.first()
            if note:
                response = client.get(f'/note/{note.id}/edit')
                html = response.data.decode()
                # Auto-fill is only for new notes
                assert 'id="autoFillBtn"' not in html
                # Import from Meeting is available on both new and edit pages
                assert 'id="importMeetingBtn"' in html


# =============================================================================
# Migration
# =============================================================================

class TestWorkiqMigration:
    """Tests for the WorkIQ prompt migration."""

    def test_workiq_summary_prompt_column_exists(self, app):
        """UserPreference model should have workiq_summary_prompt field."""
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            # Field should exist and be None by default
            assert hasattr(pref, 'workiq_summary_prompt')
