"""
Tests for AI-powered topic suggestion features.
Uses mocked Azure OpenAI client with Entra ID authentication.
"""

import json
import os
from datetime import date, datetime
from unittest.mock import patch, MagicMock
import pytest
from app import db
from app.models import AIQueryLog, Topic, User

# Common env vars for AI tests
AI_ENV_VARS = {
    'AZURE_OPENAI_ENDPOINT': 'https://test.cognitiveservices.azure.com/',
    'AZURE_OPENAI_DEPLOYMENT': 'gpt-4o-mini',
    'AZURE_OPENAI_API_VERSION': '2024-12-01-preview',
    'AZURE_CLIENT_ID': 'test-client-id',
    'AZURE_CLIENT_SECRET': 'test-client-secret',
    'AZURE_TENANT_ID': 'test-tenant-id'
}


def create_mock_openai_response(content, model='gpt-4o-mini', prompt_tokens=100, completion_tokens=50):
    """Create a mock OpenAI chat completion response."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    mock_response.model = model
    mock_response.usage = MagicMock()
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    mock_response.usage.total_tokens = prompt_tokens + completion_tokens
    return mock_response


class TestAIEnabled:
    """Test AI enabled/disabled detection from environment variables."""

    def test_ai_enabled_when_env_vars_set(self, app):
        """Test that AI is enabled when endpoint and deployment are set."""
        from app.routes.ai import is_ai_enabled
        with app.app_context():
            with patch.dict('os.environ', AI_ENV_VARS):
                assert is_ai_enabled() is True

    def test_ai_disabled_when_no_endpoint(self, app):
        """Test that AI is disabled when endpoint is missing."""
        from app.routes.ai import is_ai_enabled
        with app.app_context():
            env = {k: v for k, v in AI_ENV_VARS.items() if k != 'AZURE_OPENAI_ENDPOINT'}
            with patch.dict('os.environ', env, clear=True):
                assert is_ai_enabled() is False

    def test_ai_disabled_when_no_deployment(self, app):
        """Test that AI is disabled when deployment is missing."""
        from app.routes.ai import is_ai_enabled
        with app.app_context():
            env = {k: v for k, v in AI_ENV_VARS.items() if k != 'AZURE_OPENAI_DEPLOYMENT'}
            with patch.dict('os.environ', env, clear=True):
                assert is_ai_enabled() is False

    def test_ai_disabled_when_env_empty(self, app):
        """Test that AI is disabled when env vars are empty strings."""
        from app.routes.ai import is_ai_enabled
        with app.app_context():
            env = {**AI_ENV_VARS, 'AZURE_OPENAI_ENDPOINT': '', 'AZURE_OPENAI_DEPLOYMENT': ''}
            with patch.dict('os.environ', env, clear=True):
                assert is_ai_enabled() is False
    

class TestAIConnection:
    """Test AI connection testing functionality."""
    
    @patch('app.routes.ai.get_azure_openai_client')
    def test_connection_test_success(self, mock_get_client, app, client):
        """Test successful AI connection test."""
        with app.app_context():
            test_user = User.query.first()
            test_user.is_admin = True
            db.session.commit()
        
        # Mock the Azure OpenAI client
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = create_mock_openai_response('Connection successful!')
        
        # Set env vars for the test
        with patch.dict('os.environ', {
            'AZURE_OPENAI_ENDPOINT': 'https://test.cognitiveservices.azure.com/',
            'AZURE_OPENAI_DEPLOYMENT': 'gpt-4o-mini',
            'AZURE_CLIENT_ID': 'test-id',
            'AZURE_CLIENT_SECRET': 'test-secret',
            'AZURE_TENANT_ID': 'test-tenant'
        }):
            response = client.post('/api/admin/ai-config/test', json={})
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'successful' in data['message'].lower()
    
    @patch('app.routes.ai.get_azure_openai_client')
    def test_connection_test_failure(self, mock_get_client, app, client):
        """Test failed AI connection test."""
        with app.app_context():
            test_user = User.query.first()
            test_user.is_admin = True
            db.session.commit()
        
        # Mock failed API response
        mock_get_client.side_effect = Exception("Authentication failed")
        
        # Set env vars for the test
        with patch.dict('os.environ', {
            'AZURE_OPENAI_ENDPOINT': 'https://test.cognitiveservices.azure.com/',
            'AZURE_OPENAI_DEPLOYMENT': 'gpt-4o-mini',
            'AZURE_CLIENT_ID': 'test-id',
            'AZURE_CLIENT_SECRET': 'test-secret',
            'AZURE_TENANT_ID': 'test-tenant'
        }):
            response = client.post('/api/admin/ai-config/test', json={})
        
        # Should return error response
        data = response.get_json()
        assert data['success'] is False
        error_text = data.get('error', '').lower()
        assert 'failed' in error_text or 'authentication' in error_text


class TestAISuggestions:
    """Test AI topic suggestion functionality."""

    @patch.dict('os.environ', AI_ENV_VARS)
    @patch('app.routes.ai.get_azure_openai_client')
    def test_suggest_topics_success(self, mock_get_client, app, client):
        """Test successful topic suggestion."""
        with app.app_context():
            test_user = User.query.first()
            
            # Mock the Azure OpenAI client
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.chat.completions.create.return_value = create_mock_openai_response(
                '["Azure Functions", "API Management", "Serverless"]'
            )
            
            response = client.post('/api/ai/suggest-topics', json={
                'call_notes': 'Discussed Azure Functions and API Management for serverless architecture'
            })
            
            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] is True
            assert len(data['topics']) == 3
            assert any('azure functions' in t['name'].lower() for t in data['topics'])
            
            # Verify topics were created
            topics = Topic.query.all()
            assert len(topics) == 3
            
            # Verify audit log entry
            log = AIQueryLog.query.first()
            assert log is not None
            assert log.success is True
            assert 'Azure Functions' in log.request_text
    
    @patch.dict('os.environ', AI_ENV_VARS)
    @patch('app.routes.ai.get_azure_openai_client')
    def test_suggest_topics_reuses_existing(self, mock_get_client, app, client):
        """Test that existing topics are reused (case-insensitive)."""
        with app.app_context():
            # Create existing topic with different case
            test_user = User.query.first()
            existing_topic = Topic(name='azure functions')
            db.session.add(existing_topic)
            db.session.commit()
            existing_id = existing_topic.id
            
            # Mock the Azure OpenAI client
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.chat.completions.create.return_value = create_mock_openai_response(
                '["Azure Functions", "Cosmos DB"]'
            )
            
            response = client.post('/api/ai/suggest-topics', json={
                'call_notes': 'Discussed Azure Functions and Cosmos DB'
            })
            
            assert response.status_code == 200
            data = response.get_json()
            
            # Should return 2 topics (one reused, one new)
            assert len(data['topics']) == 2
            assert any(t['id'] == existing_id for t in data['topics'])
            
            # Total topics in DB should be 2 (not 3)
            assert Topic.query.count() == 2
    
    @patch.dict('os.environ', {'AZURE_OPENAI_ENDPOINT': '', 'AZURE_OPENAI_DEPLOYMENT': ''})
    def test_suggest_topics_when_disabled(self, app, client):
        """Test that suggestions fail when AI env vars are not set."""
        response = client.post('/api/ai/suggest-topics', json={
            'call_notes': 'Test content'
        })
        
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'not enabled' in data['error'].lower() or 'not configured' in data['error'].lower()
    
    @patch.dict('os.environ', AI_ENV_VARS)
    def test_suggest_topics_requires_call_notes(self, app, client):
        """Test that call_notes parameter is required."""
        
        response = client.post('/api/ai/suggest-topics', json={})
        
        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'too short' in data['error'].lower() or 'required' in data['error'].lower() or 'call notes' in data['error'].lower()


class TestAuditLogging:
    """Test AI audit logging functionality."""

    @patch.dict('os.environ', AI_ENV_VARS)
    @patch('app.routes.ai.get_azure_openai_client')
    def test_audit_log_success(self, mock_get_client, app, client):
        """Test that successful calls are logged."""
        with app.app_context():
            test_user = User.query.first()
            
            # Mock the Azure OpenAI client
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.chat.completions.create.return_value = create_mock_openai_response(
                '["Topic1", "Topic2"]'
            )
            
            client.post('/api/ai/suggest-topics', json={
                'call_notes': 'Test content for audit log'
            })
            
            # Check audit log
            log = AIQueryLog.query.first()
            assert log is not None
            assert log.success is True
            assert 'Test content for audit log' in log.request_text
            assert 'Topic1' in log.response_text and 'Topic2' in log.response_text
            assert log.error_message is None
            assert isinstance(log.timestamp, datetime)
    
    @patch.dict('os.environ', AI_ENV_VARS)
    @patch('app.routes.ai.get_azure_openai_client')
    def test_audit_log_failure(self, mock_get_client, app, client):
        """Test that failed calls are logged with error."""
        with app.app_context():
            # Mock the Azure OpenAI client to raise an error
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.chat.completions.create.side_effect = Exception("API Error")
            
            client.post('/api/ai/suggest-topics', json={
                'call_notes': 'This call will fail'
            })
            
            # Check audit log
            log = AIQueryLog.query.first()
            assert log is not None
            assert log.success is False
            assert 'This call will fail' in log.request_text
            assert log.error_message is not None
            assert len(log.error_message) > 0
    
    @patch.dict('os.environ', AI_ENV_VARS)
    @patch('app.routes.ai.get_azure_openai_client')
    def test_audit_log_truncation(self, mock_get_client, app, client):
        """Test that long texts are truncated in audit log."""
        with app.app_context():
            # Create very long call notes (over 1000 chars)
            long_notes = 'A' * 1500
            
            # Mock the Azure OpenAI client
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.chat.completions.create.return_value = create_mock_openai_response(
                '["' + 'B' * 1500 + '"]'
            )
            
            client.post('/api/ai/suggest-topics', json={
                'call_notes': long_notes
            })
            
            # Check audit log truncation
            log = AIQueryLog.query.first()
            assert log is not None
            assert len(log.request_text) <= 1000
            assert len(log.response_text) <= 1000


class TestGenerateEngagementSummary:
    """Tests for the AI engagement summary generation endpoint."""

    def _create_customer_with_logs(self, app):
        """Helper to create a customer with call logs for testing."""
        from app.models import Customer, CallLog, Seller, Territory
        with app.app_context():
            territory = Territory(name="Test Territory")
            seller = Seller(name="Test Seller", alias="tseller", seller_type="Growth")
            db.session.add_all([territory, seller])
            db.session.flush()

            customer = Customer(
                name="Test AI Customer",
                tpid=11111,
                seller_id=seller.id,
                territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.flush()

            cl1 = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 6, 15, 12, 0, 0),
                content="Discussed migrating their monolith to AKS. Key contact: Jane Smith, CTO.",
            )
            cl2 = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 7, 1, 14, 0, 0),
                content="Follow-up on AKS migration. Targeting Q3 go-live. Estimated $50K ACR.",
            )
            db.session.add_all([cl1, cl2])
            db.session.commit()
            return customer.id

    def test_returns_400_when_ai_disabled(self, app, client):
        """Should return 400 when AI env vars are not set."""
        customer_id = self._create_customer_with_logs(app)
        with patch.dict('os.environ', {}, clear=True):
            resp = client.post(
                '/api/ai/generate-engagement-summary',
                json={'customer_id': customer_id},
                content_type='application/json',
            )
        assert resp.status_code == 400
        assert 'not configured' in resp.get_json()['error']

    def test_returns_400_when_no_customer_id(self, app, client):
        """Should return 400 when customer_id is missing."""
        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.post(
                '/api/ai/generate-engagement-summary',
                json={},
                content_type='application/json',
            )
        assert resp.status_code == 400
        assert 'customer_id' in resp.get_json()['error']

    def test_returns_404_when_customer_not_found(self, app, client):
        """Should return 404 for nonexistent customer."""
        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.post(
                '/api/ai/generate-engagement-summary',
                json={'customer_id': 999999},
                content_type='application/json',
            )
        assert resp.status_code == 404

    def test_returns_400_when_no_call_logs(self, app, client):
        """Should return 400 when customer has no call logs."""
        from app.models import Customer
        with app.app_context():
            customer = Customer(name="Empty Customer", tpid=22222)
            db.session.add(customer)
            db.session.commit()
            cid = customer.id

        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.post(
                '/api/ai/generate-engagement-summary',
                json={'customer_id': cid},
                content_type='application/json',
            )
        assert resp.status_code == 400
        assert 'No call logs' in resp.get_json()['error']

    @patch('app.routes.ai.get_azure_openai_client')
    def test_success_returns_summary(self, mock_get_client, app, client):
        """Should return AI-generated summary on success."""
        customer_id = self._create_customer_with_logs(app)

        summary_text = (
            "Key Individuals & Titles: Jane Smith, CTO\n"
            "Technical/Business Problem: Monolith architecture limiting scalability\n"
            "Business Process/Strategy: Migration to microservices on AKS\n"
            "Solution Resources: Azure Kubernetes Service (AKS)\n"
            "Business Outcome in Estimated $$ACR: $50K ACR\n"
            "Future Date/Timeline: Q3 go-live target\n"
            "Risks/Blockers: Not identified in call logs"
        )
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = create_mock_openai_response(summary_text)

        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.post(
                '/api/ai/generate-engagement-summary',
                json={'customer_id': customer_id},
                content_type='application/json',
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'Jane Smith' in data['summary']
        assert data['call_log_count'] == 2

    @patch('app.routes.ai.get_azure_openai_client')
    def test_logs_success_to_ai_query_log(self, mock_get_client, app, client):
        """Should create an AIQueryLog entry on success."""
        customer_id = self._create_customer_with_logs(app)

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = create_mock_openai_response("Summary here")

        with patch.dict('os.environ', AI_ENV_VARS):
            client.post(
                '/api/ai/generate-engagement-summary',
                json={'customer_id': customer_id},
                content_type='application/json',
            )

        with app.app_context():
            log = AIQueryLog.query.filter(
                AIQueryLog.request_text.like('%Engagement summary%')
            ).first()
            assert log is not None
            assert log.success is True
            assert 'Test AI Customer' in log.request_text

    @patch('app.routes.ai.get_azure_openai_client')
    def test_logs_failure_to_ai_query_log(self, mock_get_client, app, client):
        """Should create an AIQueryLog entry on failure."""
        customer_id = self._create_customer_with_logs(app)

        mock_get_client.side_effect = Exception("API quota exceeded")

        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.post(
                '/api/ai/generate-engagement-summary',
                json={'customer_id': customer_id},
                content_type='application/json',
            )

        assert resp.status_code == 500
        with app.app_context():
            log = AIQueryLog.query.filter(
                AIQueryLog.request_text.like('%Engagement summary%')
            ).first()
            assert log is not None
            assert log.success is False
            assert 'quota' in log.error_message.lower()

    @patch('app.routes.ai.get_azure_openai_client')
    def test_includes_customer_notes_in_prompt(self, mock_get_client, app, client):
        """Should include existing customer notes in the user message sent to AI."""
        customer_id = self._create_customer_with_logs(app)

        # Add notes to the customer
        from app.models import Customer
        with app.app_context():
            customer = db.session.get(Customer, customer_id)
            customer.notes = "Key contact is Jane Smith (CTO). Budget approved for Q3."
            db.session.commit()

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.chat.completions.create.return_value = create_mock_openai_response("Summary")

        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.post(
                '/api/ai/generate-engagement-summary',
                json={'customer_id': customer_id},
                content_type='application/json',
            )

        assert resp.status_code == 200
        # Verify the user message sent to OpenAI includes the customer notes
        call_args = mock_client.chat.completions.create.call_args
        user_message = call_args[1]['messages'][1]['content'] if 'messages' in call_args[1] else call_args[0][0][1]['content']
        assert 'Existing Customer Notes' in user_message
        assert 'Jane Smith (CTO)' in user_message
        assert 'Budget approved for Q3' in user_message


class TestGenerateButtonVisibility:
    """Tests that the Generate button appears/hides based on AI config."""

    def test_generate_button_visible_when_ai_enabled(self, app, client):
        """Generate button should appear when AI is enabled and customer has call logs."""
        from app.models import Customer, CallLog
        with app.app_context():
            customer = Customer(name="Button Test", tpid=33333)
            db.session.add(customer)
            db.session.flush()
            cl = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 6, 1),
                content="Test call",
            )
            db.session.add(cl)
            db.session.commit()
            cid = customer.id

        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'id="generateNotesBtn"' in resp.data

    def test_generate_button_hidden_when_ai_disabled(self, app, client):
        """Generate button should not appear when AI is disabled."""
        from app.models import Customer, CallLog
        with app.app_context():
            customer = Customer(name="No AI Test", tpid=44444)
            db.session.add(customer)
            db.session.flush()
            cl = CallLog(
                customer_id=customer.id,
                call_date=datetime(2025, 6, 1),
                content="Test call",
            )
            db.session.add(cl)
            db.session.commit()
            cid = customer.id

        with patch.dict('os.environ', {}, clear=True):
            resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'id="generateNotesBtn"' not in resp.data

    def test_generate_button_hidden_when_no_call_logs(self, app, client):
        """Generate button should not appear when customer has no call logs."""
        from app.models import Customer
        with app.app_context():
            customer = Customer(name="No Logs Test", tpid=55555)
            db.session.add(customer)
            db.session.commit()
            cid = customer.id

        with patch.dict('os.environ', AI_ENV_VARS):
            resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'id="generateNotesBtn"' not in resp.data