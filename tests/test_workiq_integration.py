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
from unittest.mock import patch, MagicMock
from app import db


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
    """Tests for scheduled milestone sync configuration."""

    def test_sync_disabled_when_env_var_not_set(self, app):
        """Scheduled sync should not start when MILESTONE_SYNC_HOUR is not set."""
        from app.services.scheduled_sync import _sync_running, stop_scheduled_sync
        import app.services.scheduled_sync as sync_mod

        # Ensure stopped state
        stop_scheduled_sync()

        with patch.dict(os.environ, {}, clear=False):
            # Remove the env var if it exists
            os.environ.pop('MILESTONE_SYNC_HOUR', None)
            sync_mod._sync_running = False
            sync_mod.start_scheduled_sync(app)
            assert sync_mod._sync_running is False

    def test_sync_rejects_invalid_hour(self, app):
        """Scheduled sync should reject invalid hour values."""
        import app.services.scheduled_sync as sync_mod
        sync_mod._sync_running = False

        with patch.dict(os.environ, {'MILESTONE_SYNC_HOUR': '25'}):
            sync_mod.start_scheduled_sync(app)
            assert sync_mod._sync_running is False

        with patch.dict(os.environ, {'MILESTONE_SYNC_HOUR': 'abc'}):
            sync_mod.start_scheduled_sync(app)
            assert sync_mod._sync_running is False

    def test_sync_starts_with_valid_hour(self, app):
        """Scheduled sync should start when a valid hour is configured."""
        import app.services.scheduled_sync as sync_mod
        sync_mod._sync_running = False

        with patch.dict(os.environ, {'MILESTONE_SYNC_HOUR': '3'}):
            sync_mod.start_scheduled_sync(app)
            assert sync_mod._sync_running is True

        # Clean up
        sync_mod.stop_scheduled_sync()
        import time
        time.sleep(0.1)  # Let the thread see the stop signal

    def test_stop_sync(self, app):
        """stop_scheduled_sync should set running flag to False."""
        import app.services.scheduled_sync as sync_mod
        sync_mod._sync_running = True
        sync_mod.stop_scheduled_sync()
        assert sync_mod._sync_running is False


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
        """New call log form should show the Import from Meeting button."""
        with app.app_context():
            from app.models import Customer
            customer = Customer.query.first()
            customer_id = customer.id

        response = client.get(f'/note/new?customer_id={customer_id}')
        html = response.data.decode()
        assert 'Import from Meeting' in html
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
        """Edit call log form hides auto-fill but keeps Import from Meeting."""
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
