"""Tests for the Copilot chat endpoint (POST /api/ai/chat)."""
import json
from unittest.mock import patch, MagicMock

import pytest


class TestChatEndpointValidation:
    """Test request validation on the chat endpoint."""

    def test_missing_body(self, client):
        """Rejects requests with no JSON body."""
        resp = client.post('/api/ai/chat', content_type='application/json')
        assert resp.status_code == 400

    def test_missing_message(self, client):
        """Rejects requests with no message."""
        resp = client.post('/api/ai/chat', json={
            'context': {'page': 'index'},
        })
        assert resp.status_code == 400
        assert 'message is required' in resp.get_json()['error']

    def test_empty_message(self, client):
        """Rejects empty string messages."""
        resp = client.post('/api/ai/chat', json={
            'message': '   ',
            'context': {'page': 'index'},
        })
        assert resp.status_code == 400

    def test_message_too_long(self, client):
        """Rejects messages over 2000 characters."""
        resp = client.post('/api/ai/chat', json={
            'message': 'x' * 2001,
            'context': {'page': 'index'},
        })
        assert resp.status_code == 400
        assert 'too long' in resp.get_json()['error']

    def test_missing_context(self, client):
        """Rejects requests with no context."""
        resp = client.post('/api/ai/chat', json={
            'message': 'Hello',
        })
        assert resp.status_code == 400
        assert 'context' in resp.get_json()['error']

    def test_missing_context_page(self, client):
        """Rejects context without a page field."""
        resp = client.post('/api/ai/chat', json={
            'message': 'Hello',
            'context': {'customer_id': 1},
        })
        assert resp.status_code == 400


class TestChatEndpointFlow:
    """Test the chat endpoint's tool-calling orchestration."""

    @patch('app.routes.ai.gateway_call')
    def test_simple_reply_no_tools(self, mock_gw, client):
        """Gateway returns a plain text reply with no tool calls."""
        mock_gw.return_value = {
            'success': True,
            'message': {'role': 'assistant', 'content': 'Hello! How can I help?'},
            'usage': {
                'model': 'gpt-4o',
                'prompt_tokens': 50,
                'completion_tokens': 10,
                'total_tokens': 60,
            },
        }
        resp = client.post('/api/ai/chat', json={
            'message': 'Hi',
            'context': {'page': 'index'},
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['reply'] == 'Hello! How can I help?'
        assert data['tools_used'] == []

        # Verify gateway was called with correct structure
        call_args = mock_gw.call_args
        assert call_args[0][0] == '/v1/chat'
        payload = call_args[0][1]
        assert payload['context']['page'] == 'index'
        assert any(m['content'] == 'Hi' for m in payload['messages'])
        assert len(payload['tools']) > 0

    @patch('app.routes.ai.execute_tool')
    @patch('app.routes.ai.gateway_call')
    def test_tool_call_round_trip(self, mock_gw, mock_exec, client):
        """Gateway requests a tool call, Flask executes it, gets final reply."""
        # First call: model requests a tool call
        mock_gw.side_effect = [
            {
                'success': True,
                'message': {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_123',
                        'type': 'function',
                        'function': {
                            'name': 'search_customers',
                            'arguments': '{"query": "Contoso"}',
                        },
                    }],
                },
                'usage': {
                    'model': 'gpt-4o',
                    'prompt_tokens': 100,
                    'completion_tokens': 20,
                    'total_tokens': 120,
                },
            },
            # Second call: model returns final answer with tool results
            {
                'success': True,
                'message': {
                    'role': 'assistant',
                    'content': 'Contoso has 3 active engagements.',
                },
                'usage': {
                    'model': 'gpt-4o',
                    'prompt_tokens': 200,
                    'completion_tokens': 15,
                    'total_tokens': 215,
                },
            },
        ]
        mock_exec.return_value = [
            {'id': 1, 'name': 'Contoso', 'tpid': '12345'},
        ]

        resp = client.post('/api/ai/chat', json={
            'message': 'Tell me about Contoso',
            'context': {'page': 'customers_list'},
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'Contoso' in data['reply']
        assert 'search_customers' in data['tools_used']

        # Verify tool was executed
        mock_exec.assert_called_once_with('search_customers', {'query': 'Contoso'})

        # Verify second gateway call included tool result
        second_call = mock_gw.call_args_list[1]
        msgs = second_call[0][1]['messages']
        tool_msg = [m for m in msgs if m.get('role') == 'tool']
        assert len(tool_msg) == 1
        assert tool_msg[0]['tool_call_id'] == 'call_123'

    @patch('app.routes.ai.execute_tool')
    @patch('app.routes.ai.gateway_call')
    def test_tool_execution_error_handled(self, mock_gw, mock_exec, client):
        """Tool execution errors are sent back to the model gracefully."""
        mock_gw.side_effect = [
            {
                'success': True,
                'message': {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [{
                        'id': 'call_456',
                        'type': 'function',
                        'function': {
                            'name': 'search_customers',
                            'arguments': '{"query": "test"}',
                        },
                    }],
                },
                'usage': {
                    'model': 'gpt-4o', 'prompt_tokens': 50,
                    'completion_tokens': 10, 'total_tokens': 60,
                },
            },
            {
                'success': True,
                'message': {
                    'role': 'assistant',
                    'content': 'Sorry, I had trouble looking that up.',
                },
                'usage': {
                    'model': 'gpt-4o', 'prompt_tokens': 80,
                    'completion_tokens': 12, 'total_tokens': 92,
                },
            },
        ]
        mock_exec.side_effect = ValueError("DB connection failed")

        resp = client.post('/api/ai/chat', json={
            'message': 'Search for test',
            'context': {'page': 'index'},
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        # The error was sent to the model, which provided a graceful reply
        assert 'trouble' in data['reply']

    @patch('app.routes.ai.gateway_call')
    def test_max_tool_rounds_cap(self, mock_gw, client):
        """Tool calling is capped at MAX_TOOL_ROUNDS to prevent runaway."""
        # Every call returns a tool_call (infinite loop scenario)
        tool_response = {
            'success': True,
            'message': {
                'role': 'assistant',
                'content': 'Still working...',
                'tool_calls': [{
                    'id': 'call_loop',
                    'type': 'function',
                    'function': {
                        'name': 'search_customers',
                        'arguments': '{"query": "test"}',
                    },
                }],
            },
            'usage': {
                'model': 'gpt-4o', 'prompt_tokens': 50,
                'completion_tokens': 10, 'total_tokens': 60,
            },
        }
        mock_gw.return_value = tool_response

        with patch('app.routes.ai.execute_tool', return_value=[]):
            resp = client.post('/api/ai/chat', json={
                'message': 'Keep going',
                'context': {'page': 'index'},
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        # Should have called gateway MAX_TOOL_ROUNDS + 1 times then stopped
        assert mock_gw.call_count <= 4  # 3 rounds + 1

    @patch('app.routes.ai.gateway_call')
    def test_history_passed_through(self, mock_gw, client):
        """Conversation history is included in the messages sent to gateway."""
        mock_gw.return_value = {
            'success': True,
            'message': {'role': 'assistant', 'content': 'Got it.'},
            'usage': {
                'model': 'gpt-4o', 'prompt_tokens': 50,
                'completion_tokens': 5, 'total_tokens': 55,
            },
        }
        history = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there!'},
        ]
        resp = client.post('/api/ai/chat', json={
            'message': 'What about Contoso?',
            'context': {'page': 'index'},
            'history': history,
        })
        assert resp.status_code == 200

        # Verify history was included in messages
        msgs = mock_gw.call_args[0][1]['messages']
        contents = [m.get('content') for m in msgs]
        assert 'Hello' in contents
        assert 'Hi there!' in contents
        assert 'What about Contoso?' in contents

    @patch('app.routes.ai.gateway_call')
    def test_gateway_error_returns_502(self, mock_gw, client):
        """Gateway errors are handled and logged."""
        from app.gateway_client import GatewayError
        mock_gw.side_effect = GatewayError("Service unavailable", status_code=503)

        resp = client.post('/api/ai/chat', json={
            'message': 'Hello',
            'context': {'page': 'index'},
        })
        assert resp.status_code == 503
        data = resp.get_json()
        assert data['success'] is False

    @patch('app.routes.ai.gateway_call')
    def test_usage_accumulated_across_rounds(self, mock_gw, client):
        """Token usage is accumulated across multiple tool-call rounds."""
        mock_gw.side_effect = [
            {
                'success': True,
                'message': {
                    'role': 'assistant', 'content': '',
                    'tool_calls': [{
                        'id': 'c1', 'type': 'function',
                        'function': {'name': 'search_customers', 'arguments': '{}'},
                    }],
                },
                'usage': {'model': 'gpt-4o', 'prompt_tokens': 100,
                          'completion_tokens': 20, 'total_tokens': 120},
            },
            {
                'success': True,
                'message': {'role': 'assistant', 'content': 'Done.'},
                'usage': {'model': 'gpt-4o', 'prompt_tokens': 200,
                          'completion_tokens': 10, 'total_tokens': 210},
            },
        ]
        with patch('app.routes.ai.execute_tool', return_value=[]):
            resp = client.post('/api/ai/chat', json={
                'message': 'test',
                'context': {'page': 'index'},
            })
        data = resp.get_json()
        assert data['usage']['prompt_tokens'] == 300
        assert data['usage']['completion_tokens'] == 30
        assert data['usage']['total_tokens'] == 330
