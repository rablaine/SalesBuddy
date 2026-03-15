"""
Tests for the Fill My Day feature.
Tests cover the page route, save API, process API with milestone matching,
and customer list API integration.
"""
import pytest
import json
from datetime import datetime
from unittest.mock import patch, MagicMock


class TestFillMyDayPage:
    """Tests for the Fill My Day page route."""

    def test_fill_my_day_page_loads(self, client):
        """Test that Fill My Day page loads without a date."""
        response = client.get('/fill-my-day')
        assert response.status_code == 200
        assert b'Fill My Day' in response.data
        assert b'Fetch Meetings' in response.data

    def test_fill_my_day_page_with_date(self, client):
        """Test that Fill My Day page loads with a prefilled date."""
        response = client.get('/fill-my-day?date=2026-02-24')
        assert response.status_code == 200
        assert b'2026-02-24' in response.data

    def test_fill_my_day_page_with_invalid_date(self, client):
        """Test that Fill My Day page handles invalid date gracefully."""
        response = client.get('/fill-my-day?date=not-a-date')
        assert response.status_code == 200
        # Should still load, just without prefilled date
        assert b'Fill My Day' in response.data

    def test_fill_my_day_page_has_lightning_icon(self, client):
        """Test that the page uses the lightning bolt icon."""
        response = client.get('/fill-my-day')
        assert b'bi-lightning-charge' in response.data


class TestFillMyDaySaveAPI:
    """Tests for the Fill My Day save call log API."""

    def test_save_note_success(self, client, sample_data):
        """Test saving a call log from Fill My Day."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': '2026-02-24',
                'call_time': '14:00',
                'content': '<h2>Test Meeting</h2><p>Great discussion.</p>',
                'topic_ids': [sample_data['topic1_id']]
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'note_id' in data
        assert 'view_url' in data

    def test_save_note_without_time(self, client, sample_data):
        """Test saving a call log without a specific time."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': '2026-02-24',
                'content': '<p>Quick sync.</p>',
                'topic_ids': []
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

    def test_save_note_missing_customer(self, client, sample_data):
        """Test that missing customer returns error."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'call_date': '2026-02-24',
                'content': '<p>Test</p>',
                'topic_ids': []
            }),
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'Customer' in data['error']

    def test_save_note_missing_date(self, client, sample_data):
        """Test that missing date returns error."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'content': '<p>Test</p>',
                'topic_ids': []
            }),
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'Date' in data['error']

    def test_save_note_missing_content(self, client, sample_data):
        """Test that missing content returns error."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': '2026-02-24',
                'content': '',
                'topic_ids': []
            }),
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'Content' in data['error']

    def test_save_note_invalid_date(self, client, sample_data):
        """Test that invalid date format returns error."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': 'not-a-date',
                'content': '<p>Test</p>',
                'topic_ids': []
            }),
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    def test_save_note_nonexistent_customer(self, client, sample_data):
        """Test that nonexistent customer returns 404."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': 99999,
                'call_date': '2026-02-24',
                'content': '<p>Test</p>',
                'topic_ids': []
            }),
            content_type='application/json'
        )
        assert response.status_code == 404
        data = response.get_json()
        assert data['success'] is False

    def test_save_note_with_topics(self, client, sample_data):
        """Test that topics are correctly associated with saved call log."""
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': '2026-02-24',
                'call_time': '10:00',
                'content': '<h2>Azure VM Discussion</h2><p>Migration planning.</p>',
                'topic_ids': [sample_data['topic1_id'], sample_data['topic2_id']]
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

        # Verify topics were saved
        with client.application.app_context():
            from app.models import db, Note
            note = db.session.get(Note, data['note_id'])
            assert len(note.topics) == 2

    def test_save_note_with_milestone(self, client, sample_data):
        """Test saving a call log with milestone data from Fill My Day."""
        milestone_data = {
            'msx_milestone_id': 'test-guid-12345',
            'name': 'Azure Migration - Phase 1',
            'number': '7-123456',
            'status': 'On Track',
            'status_code': 1,
            'opportunity_name': 'Cloud Transformation',
            'url': 'https://example.com/milestone/test-guid-12345',
            'workload': 'Azure'
        }
        response = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': '2026-02-24',
                'call_time': '14:00',
                'content': '<h2>Migration Review</h2><p>Discussed timeline.</p>',
                'topic_ids': [sample_data['topic1_id']],
                'milestone': milestone_data,
                'task_subject': 'Follow up on migration timeline',
                'task_description': 'Schedule next review meeting'
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

        # Verify milestone was linked
        with client.application.app_context():
            from app.models import db, Note
            note = db.session.get(Note, data['note_id'])
            assert len(note.milestones) == 1
            assert note.milestones[0].msx_milestone_id == 'test-guid-12345'
            assert note.milestones[0].title == 'Azure Migration - Phase 1'

    def test_save_note_reuses_existing_milestone(self, client, sample_data):
        """Test that saving with an existing milestone MSX ID reuses it."""
        milestone_data = {
            'msx_milestone_id': 'reuse-guid-67890',
            'name': 'Initial Milestone',
            'url': 'https://example.com/milestone/reuse-guid-67890',
            'status': 'On Track'
        }
        # Save first call log with milestone
        resp1 = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': '2026-02-20',
                'content': '<p>First meeting</p>',
                'topic_ids': [],
                'milestone': milestone_data
            }),
            content_type='application/json'
        )
        assert resp1.get_json()['success'] is True

        # Save second call log with same milestone
        milestone_data['name'] = 'Updated Milestone Name'
        resp2 = client.post('/api/fill-my-day/save',
            data=json.dumps({
                'customer_id': sample_data['customer1_id'],
                'call_date': '2026-02-21',
                'content': '<p>Second meeting</p>',
                'topic_ids': [],
                'milestone': milestone_data
            }),
            content_type='application/json'
        )
        assert resp2.get_json()['success'] is True

        # Verify both call logs share the same milestone record
        with client.application.app_context():
            from app.models import Milestone
            milestones = Milestone.query.filter_by(
                msx_milestone_id='reuse-guid-67890'
            ).all()
            assert len(milestones) == 1
            # Should have updated name
            assert milestones[0].title == 'Updated Milestone Name'

    def test_save_note_no_json_body(self, client):
        """Test that missing JSON body returns error."""
        response = client.post('/api/fill-my-day/save',
            content_type='application/json'
        )
        assert response.status_code == 400


class TestFillMyDayProcessAPI:
    """Tests for the Fill My Day process (enrich) API."""

    def test_process_missing_data(self, client):
        """Test that missing data returns error."""
        response = client.post('/api/fill-my-day/process',
            content_type='application/json'
        )
        assert response.status_code == 400

    def test_process_missing_title(self, client):
        """Test that missing meeting title returns error."""
        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {},
                'date': '2026-02-24'
            }),
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_process_with_summary(self, mock_summary, client, sample_data):
        """Test processing a meeting with WorkIQ summary."""
        mock_summary.return_value = {
            'summary': 'Discussed Azure migration strategy.',
            'topics': ['Azure', 'Migration'],
            'action_items': ['Schedule follow-up', 'Send proposal']
        }

        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': 'Acme Corp - Migration Review'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'Migration' in data['content_html']
        assert 'Schedule follow-up' in data['content_html']

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_process_summary_failure(self, mock_summary, client, sample_data):
        """Test that summary failure still returns a result."""
        mock_summary.side_effect = Exception('WorkIQ unavailable')

        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': 'Some Meeting'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'Some Meeting' in data['content_html']

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_process_returns_milestone_field(self, mock_summary, client, sample_data):
        """Test that process API returns a milestone field (even if null)."""
        mock_summary.return_value = {
            'summary': 'Quick sync about Azure.',
            'topics': [],
            'action_items': []
        }

        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': 'Quick Sync'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert 'milestone' in data

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_process_no_milestone_on_summary_failure(self, mock_summary, client, sample_data):
        """Test that milestone matching is skipped when summary fails."""
        mock_summary.side_effect = Exception('WorkIQ unavailable')

        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': 'Some Meeting'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['milestone'] is None


class TestFillMyDayParagraphFormatting:
    """Tests that multi-paragraph summaries retain formatting in content_html."""

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_summary_paragraphs_become_separate_p_tags(self, mock_summary, client, sample_data):
        """Double-newlines in summary text should produce separate <p> tags."""
        mock_summary.return_value = {
            'summary': 'First paragraph about Azure.\n\nSecond paragraph about migration.',
            'topics': [],
            'action_items': []
        }
        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': 'Test Meeting'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        data = response.get_json()
        assert '<p>First paragraph about Azure.</p>' in data['content_html']
        assert '<p>Second paragraph about migration.</p>' in data['content_html']

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_single_newlines_become_br_tags(self, mock_summary, client, sample_data):
        """Single newlines within a paragraph should become <br> tags."""
        mock_summary.return_value = {
            'summary': 'Line one\nLine two\nLine three',
            'topics': [],
            'action_items': []
        }
        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': 'Test Meeting'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        data = response.get_json()
        assert 'Line one<br>Line two<br>Line three' in data['content_html']

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_html_in_summary_is_escaped(self, mock_summary, client, sample_data):
        """HTML characters in summary text should be escaped to prevent XSS."""
        mock_summary.return_value = {
            'summary': 'Check <script>alert("xss")</script> output',
            'topics': [],
            'action_items': []
        }
        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': 'Test Meeting'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        data = response.get_json()
        assert '<script>' not in data['content_html']
        assert '&lt;script&gt;' in data['content_html']

    @patch('app.services.workiq_service.get_meeting_summary')
    def test_title_is_escaped_in_content_html(self, mock_summary, client, sample_data):
        """Meeting title should be escaped in the generated HTML."""
        mock_summary.return_value = {
            'summary': 'Normal summary.',
            'topics': [],
            'action_items': []
        }
        response = client.post('/api/fill-my-day/process',
            data=json.dumps({
                'meeting': {'title': '<img onerror=alert(1) src=x>'},
                'date': '2026-02-24',
                'customer_id': sample_data['customer1_id']
            }),
            content_type='application/json'
        )
        data = response.get_json()
        assert '<img' not in data['content_html']
        assert '&lt;img' in data['content_html']


class TestCustomerListAPIForFillMyDay:
    """Tests that the customer list API returns fields needed for Fill My Day matching."""

    def test_customer_list_includes_nickname(self, client, sample_data):
        """Test that customer list API includes nickname field."""
        response = client.get('/api/customers')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) > 0
        # Check that nickname field exists (even if null)
        assert 'nickname' in data[0]

    def test_customer_list_includes_tpid_url(self, client, sample_data):
        """Test that customer list API includes tpid_url field."""
        response = client.get('/api/customers')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) > 0
        assert 'tpid_url' in data[0]

    def test_customer_list_returns_all_customers(self, client, sample_data):
        """Test that customer list returns all customers."""
        response = client.get('/api/customers')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) >= 3  # We have 3 customers in sample_data


class TestFillMyDayNavigation:
    """Tests that Fill My Day is accessible via direct URL."""

    def test_fill_my_day_page_accessible(self, client):
        """Test that the Fill My Day page loads via direct URL."""
        response = client.get('/fill-my-day')
        assert response.status_code == 200

    def test_calendar_has_fill_my_day_icon(self, client, sample_data):
        """Test that the calendar API data supports Fill My Day icon rendering."""
        # The icon is rendered client-side, so we verify the calendar API works
        response = client.get('/api/notes/calendar')
        assert response.status_code == 200
        data = response.get_json()
        assert 'year' in data
        assert 'month' in data
