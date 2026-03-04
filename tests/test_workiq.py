"""
Tests for WorkIQ integration (meeting import functionality).
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, date
from app.services.workiq_service import (
    fuzzy_match_score,
    _parse_meetings_response,
    _parse_summary_response,
    _clean_ai_preamble,
    find_best_customer_match
)


class TestFuzzyMatching:
    """Test fuzzy matching algorithms."""
    
    def test_exact_match(self):
        """Test exact string match."""
        score = fuzzy_match_score("Contoso", "Contoso")
        assert score >= 0.9
        
    def test_substring_match(self):
        """Test substring matching."""
        score = fuzzy_match_score("Contoso", "Contoso Inc.")
        assert score >= 0.9
        
    def test_partial_match(self):
        """Test partial fuzzy match."""
        score = fuzzy_match_score("Contoso Bank", "Contoso Banking Corp")
        assert score > 0.5
        
    def test_no_match(self):
        """Test non-matching strings."""
        score = fuzzy_match_score("Apple", "Microsoft")
        assert score < 0.3
        
    def test_empty_strings(self):
        """Test empty string handling."""
        assert fuzzy_match_score("", "Test") == 0.0
        assert fuzzy_match_score("Test", "") == 0.0
        assert fuzzy_match_score("", "") == 0.0
        
    def test_case_insensitive(self):
        """Test case insensitive matching."""
        score1 = fuzzy_match_score("Contoso", "CONTOSO")
        score2 = fuzzy_match_score("CONTOSO", "Contoso")
        assert score1 >= 0.9
        assert score2 >= 0.9


class TestMeetingsResponseParsing:
    """Test parsing of WorkIQ meeting list responses (markdown table format)."""
    
    def test_parse_single_meeting(self):
        """Test parsing a single meeting from table format."""
        response = """
| Time | Meeting Title | External Company |
|------|---------------|------------------|
| 10:00 AM | **Weekly Sync with Customer A** | Customer A Inc |
        """
        meetings = _parse_meetings_response(response, "2026-02-24")
        
        assert len(meetings) == 1
        assert meetings[0]['title'] == "Weekly Sync with Customer A"
        assert meetings[0]['customer'] == "Customer A Inc"
        assert meetings[0]['start_time'].hour == 10
        assert meetings[0]['start_time'].minute == 0
        
    def test_parse_multiple_meetings(self):
        """Test parsing multiple meetings from table format."""
        response = """
| Time | Meeting Title | External Company |
|------|---------------|------------------|
| 9:00 AM | **Morning Standup** | Acme Corp |
| 2:30 PM | **Afternoon Review** | Beta LLC |
        """
        meetings = _parse_meetings_response(response, "2026-02-24")
        
        assert len(meetings) == 2
        assert meetings[0]['title'] == "Morning Standup"
        assert meetings[1]['title'] == "Afternoon Review"
        assert meetings[1]['start_time'].hour == 14
        assert meetings[1]['start_time'].minute == 30
        
    def test_parse_no_meetings(self):
        """Test parsing response with no meetings."""
        response = "There are no meetings scheduled for this date."
        meetings = _parse_meetings_response(response, "2026-02-24")
        
        assert len(meetings) == 0
        
    def test_parse_12_hour_time(self):
        """Test parsing 12-hour time format."""
        response = """
| Time | Meeting Title | External Company |
|------|---------------|------------------|
| 2:30 PM | **Afternoon Call** | Test Corp |
        """
        meetings = _parse_meetings_response(response, "2026-02-24")
        
        assert len(meetings) == 1
        assert meetings[0]['start_time'].hour == 14


class TestSummaryResponseParsing:
    """Test parsing of WorkIQ summary responses."""
    
    def test_parse_full_summary(self):
        """Test parsing a complete summary response."""
        response = """
        SUMMARY: This meeting covered the implementation details of the Azure migration project.
        We discussed the timeline and key milestones. The customer expressed interest in using
        Azure Kubernetes Service for their containerized workloads.
        
        TECHNOLOGIES: Azure Kubernetes Service, Azure Container Registry, Azure DevOps
        
        ACTION_ITEMS:
        1. Send AKS documentation
        2. Schedule follow-up demo
        3. Prepare cost estimate
        """
        result = _parse_summary_response(response)
        
        assert "Azure migration project" in result['summary']
        assert "Azure Kubernetes Service" in result['topics']
        assert len(result['action_items']) == 3
        assert "Send AKS documentation" in result['action_items']
        
    def test_parse_summary_only(self):
        """Test parsing response with only summary."""
        response = """
        SUMMARY: A brief discussion about cloud strategy.
        """
        result = _parse_summary_response(response)
        
        assert "cloud strategy" in result['summary']
        assert len(result['topics']) == 0
        assert len(result['action_items']) == 0


class TestFindBestCustomerMatch:
    """Test customer matching against meetings."""
    
    def test_find_match_in_customer_field(self):
        """Test matching against customer field."""
        meetings = [
            {'title': 'Weekly Sync', 'customer': 'Contoso Inc'},
            {'title': 'Other Meeting', 'customer': 'Fabrikam'},
        ]
        result = find_best_customer_match(meetings, "Contoso")
        assert result == 0
        
    def test_find_match_in_title(self):
        """Test matching against meeting title."""
        meetings = [
            {'title': 'Contoso | Weekly Review', 'customer': 'Unknown'},
            {'title': 'Other Meeting', 'customer': 'Unknown'},
        ]
        result = find_best_customer_match(meetings, "Contoso")
        assert result == 0
        
    def test_no_match_found(self):
        """Test when no match is found."""
        meetings = [
            {'title': 'AAAA Meeting', 'customer': 'AAAA Corp'},
            {'title': 'BBBB Meeting', 'customer': 'BBBB Inc'},
        ]
        result = find_best_customer_match(meetings, "Zyxwvu Completely Different")
        assert result is None
        
    def test_empty_meetings_list(self):
        """Test with empty meetings list."""
        result = find_best_customer_match([], "Any Customer")
        assert result is None
        
    def test_empty_customer_name(self):
        """Test with empty customer name."""
        meetings = [{'title': 'Test', 'customer': 'Test Corp'}]
        result = find_best_customer_match(meetings, "")
        assert result is None


class TestMeetingImportAPI:
    """Test meeting import API endpoints."""
    
    @pytest.fixture
    def client(self, app):
        """Create test client."""
        return app.test_client()
    
    @pytest.fixture
    def app(self):
        """Create application for testing."""
        from app import create_app
        app = create_app()
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        
        with app.app_context():
            from app.models import db, User
            db.create_all()
            
            # Create test user
            user = User(name='Test User', email='test@example.com')
            db.session.add(user)
            db.session.commit()
            
            yield app
            
            db.session.remove()
            db.drop_all()
    
    def test_meetings_api_requires_date(self, client):
        """Test that meetings API requires date parameter."""
        response = client.get('/api/meetings')
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data
        
    def test_meetings_api_validates_date_format(self, client):
        """Test that meetings API validates date format."""
        response = client.get('/api/meetings?date=invalid-date')
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data
        
    @patch('app.services.workiq_service.get_meetings_for_date')
    def test_meetings_api_returns_meetings(self, mock_get_meetings, client):
        """Test meetings API returns formatted meetings."""
        mock_get_meetings.return_value = [
            {
                'id': 'test_meeting_1',
                'title': 'Test Meeting',
                'start_time': datetime(2026, 2, 24, 10, 0),
                'start_time_str': '10:00',
                'customer': 'Test Customer',
                'attendees': ['John', 'Jane']
            }
        ]
        
        response = client.get('/api/meetings?date=2026-02-24')
        assert response.status_code == 200
        data = response.get_json()
        
        assert 'meetings' in data
        assert len(data['meetings']) == 1
        assert data['meetings'][0]['title'] == 'Test Meeting'
        
    @patch('app.services.workiq_service.get_meetings_for_date')
    @patch('app.services.workiq_service.find_best_customer_match')
    def test_meetings_api_auto_selects(self, mock_find_match, mock_get_meetings, client):
        """Test meetings API auto-selects matching meeting."""
        mock_get_meetings.return_value = [
            {'id': 'meeting_1', 'title': 'Contoso Sync', 'start_time': None, 
             'start_time_str': '10:00', 'customer': 'Contoso', 'attendees': []}
        ]
        mock_find_match.return_value = 0
        
        response = client.get('/api/meetings?date=2026-02-24&customer_name=Contoso')
        assert response.status_code == 200
        data = response.get_json()
        
        assert data['auto_selected_index'] == 0
        assert data['auto_selected_reason'] is not None
        
    def test_summary_api_requires_title(self, client):
        """Test that summary API requires title parameter."""
        response = client.get('/api/meetings/summary')
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data


class TestCleanAiPreamble:
    """Test stripping of conversational AI preamble and footer from WorkIQ responses."""

    def test_strips_preamble_and_footer_with_dividers(self):
        """Test the exact pattern from the user's real WorkIQ response."""
        response = (
            "Here's what I can summarize **based strictly on the meeting records "
            "available**. Please note an important limitation up front: **there is "
            "no meeting transcript**.\n"
            "\n---\n\n"
            "Meeting Summary (Discovery Call - 2026-03-02)\n\n"
            "The Discovery Call was a 45-minute meeting. The stated purpose was to "
            "conduct a discovery discussion.\n"
            "\n---\n\n"
            "\n---\n\n"
            "If you'd like, I can also:\n"
            "- Draft a **post-meeting recap email**\n"
            "- Create a **discovery findings template**\n"
        )
        result = _clean_ai_preamble(response)
        assert "Here's what I can summarize" not in result
        assert "Please note" not in result
        assert "If you'd like" not in result
        assert "Draft a" not in result
        assert "Discovery Call was a 45-minute meeting" in result

    def test_strips_heres_what_opener(self):
        """Test removal of 'Here's what...' openers without dividers."""
        response = (
            "Here's a summary of the meeting:\n\n"
            "The team discussed the migration timeline."
        )
        result = _clean_ai_preamble(response)
        assert "Here's a summary" not in result
        assert "migration timeline" in result

    def test_strips_please_note_disclaimer(self):
        """Test removal of 'Please note...' disclaimers."""
        response = (
            "Please note that no transcript was available.\n\n"
            "The meeting covered data strategy."
        )
        result = _clean_ai_preamble(response)
        assert "Please note" not in result
        assert "data strategy" in result

    def test_strips_if_youd_like_footer(self):
        """Test removal of follow-up offers."""
        response = (
            "The meeting was productive.\n\n"
            "If you'd like, I can also:\n"
            "- Draft a recap email\n"
            "- Create action items\n"
        )
        result = _clean_ai_preamble(response)
        assert "meeting was productive" in result
        assert "If you'd like" not in result
        assert "Draft a recap" not in result

    def test_strips_let_me_know_footer(self):
        """Test removal of 'Let me know if...' footer."""
        response = (
            "Key discussion points included Azure migration.\n\n"
            "Let me know if you'd like more detail on any of these areas."
        )
        result = _clean_ai_preamble(response)
        assert "Azure migration" in result
        assert "Let me know" not in result

    def test_strips_would_you_like_footer(self):
        """Test removal of 'Would you like me to...' footer."""
        response = (
            "The call focused on Fabric adoption.\n\n"
            "Would you like me to draft a follow-up email?"
        )
        result = _clean_ai_preamble(response)
        assert "Fabric adoption" in result
        assert "Would you like" not in result

    def test_strips_based_on_meeting_preamble(self):
        """Test removal of 'Based on the meeting...' caveats."""
        response = (
            "Based on the meeting invitation and metadata:\n\n"
            "The team reviewed Q1 performance."
        )
        result = _clean_ai_preamble(response)
        assert "Based on the meeting" not in result
        assert "Q1 performance" in result

    def test_strips_footnote_markers(self):
        """Test removal of orphaned footnote reference numbers."""
        response = "The meeting was about data migration. 1\nNext steps were discussed. 2"
        result = _clean_ai_preamble(response)
        # Footnote numbers (" 1", " 2") are stripped but periods remain
        assert "migration. 1" not in result
        assert "discussed. 2" not in result
        assert "data migration." in result
        assert "were discussed." in result

    def test_preserves_clean_content(self):
        """Test that clean content without preamble is not modified."""
        response = (
            "Meeting Summary\n\n"
            "The team discussed three main topics: Azure migration, "
            "cost optimization, and timeline adjustments."
        )
        result = _clean_ai_preamble(response)
        assert "three main topics" in result
        assert "Azure migration" in result

    def test_handles_empty_string(self):
        """Test that empty string returns empty."""
        assert _clean_ai_preamble("") == ""

    def test_handles_none(self):
        """Test that None returns None."""
        assert _clean_ai_preamble(None) is None

    def test_strips_trailing_dividers(self):
        """Test removal of trailing --- dividers."""
        response = "The meeting covered Azure strategy.\n\n---\n"
        result = _clean_ai_preamble(response)
        assert result.rstrip().endswith("strategy.")

    def test_end_to_end_through_parse_summary(self):
        """Test that preamble cleanup flows through _parse_summary_response."""
        response = (
            "Here's what I can summarize based on the meeting records.\n"
            "\n---\n\n"
            "The WPCU Discovery Call was a 45-minute meeting organized by Greg. "
            "The stated purpose was to conduct a discovery discussion about "
            "Azure Data Factory and Microsoft Fabric.\n"
            "\n---\n\n"
            "If you'd like, I can also:\n"
            "- Draft a recap email\n"
        )
        result = _parse_summary_response(response)
        assert "Here's what I can summarize" not in result['summary']
        assert "If you'd like" not in result['summary']
        assert "Discovery Call" in result['summary']
        # Should also extract topics from the cleaned content
        assert any("Fabric" in t for t in result['topics'])


class TestCitationStripping:
    """Test stripping of citation markers from WorkIQ/Copilot responses."""

    def test_strips_citation_links(self):
        """Citation-style markdown links [1](url) should be removed entirely."""
        response = (
            "The customer discussed Azure migration plans. [1](https://ref)\n\n"
            "Key topics included AKS and DevOps. [1](https://ref2)\n"
        )
        result = _parse_summary_response(response)
        assert '1' not in result['summary'].replace('AKS', '').replace('DevOps', '')
        assert '[1]' not in result['summary']

    def test_strips_plain_citation_brackets(self):
        """Plain citation references [1] without URL should be removed."""
        response = "The team reviewed the migration plan. [1] Next steps were discussed. [2]"
        result = _parse_summary_response(response)
        assert '[1]' not in result['summary']
        assert '[2]' not in result['summary']
        # The numbers themselves should be gone
        assert not result['summary'].rstrip().endswith('1')
        assert not result['summary'].rstrip().endswith('2')

    def test_preserves_regular_markdown_links(self):
        """Regular markdown links [text](url) should keep the text."""
        response = "Check the [Azure Portal](https://portal.azure.com) for details."
        result = _parse_summary_response(response)
        assert 'Azure Portal' in result['summary']
        assert '](https' not in result['summary']

    def test_strips_numbered_citations_in_real_summary(self):
        """Test with a realistic WorkIQ response containing citation markers."""
        response = (
            "The meeting focused on EdgeConnex's urgent requirement to migrate "
            "a large Oracle database to Azure SQL Managed Instance. [1]\n\n"
            "Key discussion centered on the limitations of first-party tooling. "
            "Azure Data Factory was discussed as a possible approach. [1]\n\n"
            "Given these constraints, the conversation shifted to Striim. [1]\n\n"
            "Action items included following up internally. [1]\n"
        )
        result = _parse_summary_response(response)
        # No paragraph should end with a bare "1"
        paragraphs = [p.strip() for p in result['summary'].split('\n\n') if p.strip()]
        for para in paragraphs:
            assert not para.endswith(' 1'), f"Paragraph ends with citation marker: ...{para[-20:]}"
            assert '[1]' not in para

    def test_multiple_different_citation_numbers(self):
        """Different citation numbers [1], [2], [3] should all be stripped."""
        response = (
            "First point about Azure. [1]\n"
            "Second point about migration. [2]\n"
            "Third point about Striim. [3]\n"
        )
        result = _parse_summary_response(response)
        assert '[1]' not in result['summary']
        assert '[2]' not in result['summary']
        assert '[3]' not in result['summary']
