"""
Tests for WorkIQ Connect Impact extraction feature (Issue #17).

Covers:
- _CONNECT_IMPACT_SUFFIX appended conditionally to prompt
- Parser extraction of CONNECT_IMPACT block
- connect_impact field in response dicts
- User preference for connect impact (default True)
- Preferences API endpoint for toggling
- Settings page UI toggle
- Call log form inline toggle
- Meeting summary API extract_impact param
- Fill My Day connects impact through to response
"""
import pytest
from unittest.mock import patch, MagicMock


# =============================================================================
# Parser Tests - CONNECT_IMPACT extraction
# =============================================================================

class TestConnectImpactParser:
    """Tests for parsing CONNECT_IMPACT block from WorkIQ responses."""

    def test_parse_impact_from_structured_response(self):
        """Parser should extract CONNECT_IMPACT signals from structured response."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "SUMMARY: We discussed Azure migration progress with Contoso.\n\n"
            "TECHNOLOGIES: Azure Kubernetes Service, Azure DevOps\n\n"
            "ACTION_ITEMS:\n"
            "1. Send AKS cost estimate\n"
            "2. Schedule follow-up\n\n"
            "TASK_TITLE: Follow up on AKS migration\n"
            "TASK_DESCRIPTION: Send cost estimate and schedule demo.\n\n"
            "CONNECT_IMPACT:\n"
            "- Customer committed to migrating 3 production workloads to AKS by Q2\n"
            "- Reduced deployment time from 4 hours to 15 minutes using Azure DevOps pipelines\n"
            "- Customer expanded Azure spend by $50K/month after successful POC\n"
        )
        result = _parse_summary_response(response)

        assert len(result['connect_impact']) == 3
        assert 'migrating 3 production workloads' in result['connect_impact'][0]
        assert 'Reduced deployment time' in result['connect_impact'][1]
        assert '$50K/month' in result['connect_impact'][2]

    def test_parse_impact_from_natural_response(self):
        """Parser should extract CONNECT_IMPACT from unstructured responses."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "The meeting covered cloud adoption strategy.\n\n"
            "TASK_TITLE: Cloud strategy review\n"
            "TASK_DESCRIPTION: Review cloud adoption plan.\n\n"
            "CONNECT_IMPACT:\n"
            "- Customer achieved 99.9% uptime after migrating to Azure\n"
            "- Reduced operational costs by 30% using serverless architecture\n"
        )
        result = _parse_summary_response(response)

        assert len(result['connect_impact']) == 2
        assert '99.9% uptime' in result['connect_impact'][0]
        assert '30%' in result['connect_impact'][1]

    def test_no_impact_block_returns_empty_list(self):
        """Parser should return empty list when no CONNECT_IMPACT block exists."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "SUMMARY: Quick sync about Azure.\n\n"
            "TASK_TITLE: Follow up\n"
            "TASK_DESCRIPTION: Send docs.\n"
        )
        result = _parse_summary_response(response)

        assert result['connect_impact'] == []

    def test_impact_block_stripped_from_summary(self):
        """CONNECT_IMPACT block should not appear in the summary text."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "The customer discussed their migration plans in detail.\n\n"
            "TASK_TITLE: Migration follow-up\n"
            "TASK_DESCRIPTION: Check on migration status.\n\n"
            "CONNECT_IMPACT:\n"
            "- Customer migrated 5 databases to Cosmos DB\n"
        )
        result = _parse_summary_response(response)

        assert 'CONNECT_IMPACT' not in result['summary']
        assert 'migrated 5 databases' not in result['summary']

    def test_impact_with_asterisk_bullets(self):
        """Parser should handle * bullet points in addition to dashes."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "Summary of the call.\n\n"
            "CONNECT_IMPACT:\n"
            "* Customer saved $200K annually using Azure Reserved Instances\n"
            "* Reduced time-to-market by 40% with CI/CD pipelines\n"
        )
        result = _parse_summary_response(response)

        assert len(result['connect_impact']) == 2
        assert '$200K annually' in result['connect_impact'][0]

    def test_impact_with_space_separator(self):
        """Parser should handle 'CONNECT IMPACT:' (space instead of underscore)."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "Summary text.\n\n"
            "CONNECT IMPACT:\n"
            "- Customer deployed 10 new microservices on AKS\n"
        )
        result = _parse_summary_response(response)

        assert len(result['connect_impact']) == 1
        assert '10 new microservices' in result['connect_impact'][0]

    def test_empty_impact_lines_filtered(self):
        """Parser should filter out empty lines within CONNECT_IMPACT block."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "Discussion notes.\n\n"
            "CONNECT_IMPACT:\n"
            "- Real impact signal\n"
            "-  \n"
            "- Another real signal\n"
        )
        result = _parse_summary_response(response)

        assert len(result['connect_impact']) == 2

    def test_task_fields_still_extracted_with_impact(self):
        """Task title and description should still work alongside impact extraction."""
        from app.services.workiq_service import _parse_summary_response
        response = (
            "Meeting summary.\n\n"
            "TASK_TITLE: Deploy new infrastructure\n"
            "TASK_DESCRIPTION: Set up AKS cluster for production workloads.\n\n"
            "CONNECT_IMPACT:\n"
            "- Customer approved production deployment\n"
        )
        result = _parse_summary_response(response)

        assert result['task_subject'] == 'Deploy new infrastructure'
        assert 'AKS cluster' in result['task_description']
        assert len(result['connect_impact']) == 1


# =============================================================================
# Prompt Suffix Tests
# =============================================================================

class TestConnectImpactSuffix:
    """Tests for _CONNECT_IMPACT_SUFFIX conditional appending."""

    @patch('app.services.workiq_service.query_workiq')
    def test_suffix_appended_when_extract_impact_true(self, mock_query):
        """Prompt should include Connect impact suffix when extract_impact=True."""
        from app.services.workiq_service import (
            get_meeting_summary, _CONNECT_IMPACT_SUFFIX, _TASK_PROMPT_SUFFIX
        )
        mock_query.return_value = "SUMMARY: Test summary."

        get_meeting_summary("Test Meeting", extract_impact=True)

        called_question = mock_query.call_args[0][0]
        assert 'CONNECT_IMPACT' in called_question
        assert 'impact signals' in called_question.lower()

    @patch('app.services.workiq_service.query_workiq')
    def test_suffix_not_appended_when_extract_impact_false(self, mock_query):
        """Prompt should NOT include Connect impact suffix when extract_impact=False."""
        from app.services.workiq_service import get_meeting_summary
        mock_query.return_value = "SUMMARY: Test summary."

        get_meeting_summary("Test Meeting", extract_impact=False)

        called_question = mock_query.call_args[0][0]
        assert 'CONNECT_IMPACT' not in called_question

    @patch('app.services.workiq_service.query_workiq')
    def test_suffix_not_appended_by_default(self, mock_query):
        """extract_impact defaults to False, so suffix should not be added by default."""
        from app.services.workiq_service import get_meeting_summary
        mock_query.return_value = "SUMMARY: Test summary."

        get_meeting_summary("Test Meeting")

        called_question = mock_query.call_args[0][0]
        assert 'CONNECT_IMPACT' not in called_question

    @patch('app.services.workiq_service.query_workiq')
    def test_task_suffix_always_present(self, mock_query):
        """Task suffix should always be present regardless of impact setting."""
        from app.services.workiq_service import get_meeting_summary
        mock_query.return_value = "SUMMARY: Test summary."

        get_meeting_summary("Test Meeting", extract_impact=False)
        question_without = mock_query.call_args[0][0]

        get_meeting_summary("Test Meeting", extract_impact=True)
        question_with = mock_query.call_args[0][0]

        assert 'TASK_TITLE' in question_without
        assert 'TASK_TITLE' in question_with

    @patch('app.services.workiq_service.query_workiq')
    def test_connect_impact_in_response_dict(self, mock_query):
        """Response dict should always include connect_impact key."""
        from app.services.workiq_service import get_meeting_summary
        mock_query.return_value = (
            "Summary of meeting.\n\n"
            "CONNECT_IMPACT:\n"
            "- Customer increased Azure usage by 200%\n"
        )

        result = get_meeting_summary("Test", extract_impact=True)
        assert 'connect_impact' in result
        assert len(result['connect_impact']) == 1

    @patch('app.services.workiq_service.query_workiq')
    def test_timeout_returns_empty_impact(self, mock_query):
        """Timeout should return empty connect_impact list."""
        from app.services.workiq_service import get_meeting_summary
        mock_query.side_effect = TimeoutError("timeout")

        result = get_meeting_summary("Test", extract_impact=True)
        assert result['connect_impact'] == []

    @patch('app.services.workiq_service.query_workiq')
    def test_error_returns_empty_impact(self, mock_query):
        """Error should return empty connect_impact list."""
        from app.services.workiq_service import get_meeting_summary
        mock_query.side_effect = RuntimeError("network error")

        result = get_meeting_summary("Test", extract_impact=True)
        assert result['connect_impact'] == []


# =============================================================================
# User Preference Tests
# =============================================================================

class TestConnectImpactPreference:
    """Tests for the workiq_connect_impact user preference."""

    def test_preference_defaults_to_true(self, app):
        """New UserPreference records should have connect_impact=True by default."""
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference(user_id=999)
            db.session.add(pref)
            db.session.flush()
            assert pref.workiq_connect_impact is True
            db.session.rollback()

    def test_save_connect_impact_setting(self, client, app):
        """API should save the connect impact preference."""
        response = client.post('/api/preferences/workiq-connect-impact',
                               json={'workiq_connect_impact': False},
                               content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['workiq_connect_impact'] is False

        # Verify persisted
        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.workiq_connect_impact is False

        # Cleanup: restore default
        client.post('/api/preferences/workiq-connect-impact',
                     json={'workiq_connect_impact': True},
                     content_type='application/json')

    def test_enable_connect_impact_setting(self, client, app):
        """API should enable the connect impact preference."""
        # First disable
        client.post('/api/preferences/workiq-connect-impact',
                     json={'workiq_connect_impact': False},
                     content_type='application/json')
        # Re-enable
        response = client.post('/api/preferences/workiq-connect-impact',
                               json={'workiq_connect_impact': True},
                               content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['workiq_connect_impact'] is True

    def test_preference_creates_record_if_missing(self, client, app):
        """Saving preference should create UserPreference if none exists."""
        with app.app_context():
            from app.models import db, UserPreference
            UserPreference.query.delete()
            db.session.commit()

        response = client.post('/api/preferences/workiq-connect-impact',
                               json={'workiq_connect_impact': True},
                               content_type='application/json')
        assert response.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref is not None
            assert pref.workiq_connect_impact is True

        # Cleanup: restore full preferences
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            if not pref:
                from app.models import User
                user = User.query.first()
                pref = UserPreference(user_id=user.id)
                db.session.add(pref)
                db.session.commit()


# =============================================================================
# UI Tests
# =============================================================================

class TestConnectImpactUI:
    """Tests for Connect impact UI elements."""

    def test_preferences_page_shows_toggle(self, client, app):
        """Settings page should display the Connect impact toggle."""
        response = client.get('/preferences')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'connectImpactSwitch' in html
        assert 'Extract Connect Impact Signals' in html

    def test_preferences_toggle_checked_by_default(self, client, app):
        """Toggle should be checked when preference is True (default)."""
        # Ensure preference is True
        client.post('/api/preferences/workiq-connect-impact',
                     json={'workiq_connect_impact': True},
                     content_type='application/json')

        response = client.get('/preferences')
        html = response.data.decode()
        # The checkbox should have 'checked' attribute
        assert 'connectImpactSwitch' in html
        # Find the relevant input and check it has 'checked'
        import re
        switch_match = re.search(
            r'<input[^>]*id="connectImpactSwitch"[^>]*>',
            html
        )
        assert switch_match is not None
        assert 'checked' in switch_match.group(0)

    def test_call_log_form_has_inline_toggle(self, client, app, sample_data):
        """Call log form should include the inline impact extraction checkbox."""
        response = client.get(f'/call-log/new?customer_id={sample_data["customer1_id"]}')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'meetingConnectImpact' in html
        assert 'Extract impact signals' in html

    def test_call_log_form_has_impact_js_variable(self, client, app, sample_data):
        """Call log form should have the connectImpactEnabled JS variable."""
        response = client.get(f'/call-log/new?customer_id={sample_data["customer1_id"]}')
        html = response.data.decode()
        assert 'connectImpactEnabled' in html


# =============================================================================
# Meeting Summary API Tests
# =============================================================================

class TestMeetingSummaryAPIImpact:
    """Tests for extract_impact parameter in meeting summary API."""

    @patch('app.services.workiq_service.query_workiq')
    def test_summary_api_passes_extract_impact(self, mock_query, client, app):
        """API should pass extract_impact=true to get_meeting_summary."""
        mock_query.return_value = (
            "Summary of meeting.\n\n"
            "CONNECT_IMPACT:\n"
            "- Customer saved $100K\n"
        )

        response = client.get(
            '/api/meetings/summary?title=Test+Meeting&extract_impact=true'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'connect_impact' in data
        assert len(data['connect_impact']) == 1
        assert '$100K' in data['connect_impact'][0]

    @patch('app.services.workiq_service.query_workiq')
    def test_summary_api_without_extract_impact(self, mock_query, client, app):
        """API should return empty connect_impact when flag not set."""
        mock_query.return_value = "Summary of meeting."

        response = client.get('/api/meetings/summary?title=Test+Meeting')
        assert response.status_code == 200
        data = response.get_json()
        assert 'connect_impact' in data
        assert data['connect_impact'] == []

    @patch('app.services.workiq_service.query_workiq')
    def test_summary_api_impact_with_custom_prompt(self, mock_query, client, app):
        """Impact extraction should work alongside custom prompts."""
        mock_query.return_value = (
            "Custom summary.\n\n"
            "TASK_TITLE: Follow up\n"
            "TASK_DESCRIPTION: Review.\n\n"
            "CONNECT_IMPACT:\n"
            "- Deployed 5 new Azure services\n"
        )

        response = client.get(
            '/api/meetings/summary?title=Test&prompt=Short+summary+of+{title}&extract_impact=true'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert len(data['connect_impact']) == 1
        assert data['task_subject'] == 'Follow up'


# =============================================================================
# Migration Tests
# =============================================================================

class TestConnectImpactMigration:
    """Tests for the workiq_connect_impact migration."""

    def test_migration_adds_column(self, app):
        """Migration should add workiq_connect_impact column to user_preferences."""
        with app.app_context():
            from app.models import db
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            columns = [c['name'] for c in inspector.get_columns('user_preferences')]
            assert 'workiq_connect_impact' in columns

    def test_migration_is_idempotent(self, app):
        """Running migration twice should not raise errors."""
        with app.app_context():
            from app.models import db
            from app.migrations import _migrate_workiq_connect_impact
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            # Run migration twice - should not error
            _migrate_workiq_connect_impact(db, inspector)
            _migrate_workiq_connect_impact(db, inspector)
