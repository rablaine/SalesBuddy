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



