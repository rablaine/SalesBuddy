"""
Tests for the APIM gateway integration.

Verifies that when ``AI_GATEWAY_URL`` is set, AI routes call the gateway
client instead of Azure OpenAI directly, and that the gateway client module
works correctly.
"""
import json
import os
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from app.models import db, AIQueryLog, Topic, Customer, Note, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
GATEWAY_ENV = {"AI_GATEWAY_URL": "https://apim-test.azure-api.net/ai"}


class TestGatewayClientModule:
    """Unit tests for app.gateway_client."""

    def test_is_gateway_enabled_when_set(self):
        with patch.dict(os.environ, GATEWAY_ENV):
            from app.gateway_client import is_gateway_enabled
            assert is_gateway_enabled() is True

    def test_is_gateway_disabled_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            from app.gateway_client import is_gateway_enabled
            assert is_gateway_enabled() is False

    def test_is_gateway_disabled_when_empty(self):
        with patch.dict(os.environ, {"AI_GATEWAY_URL": ""}):
            from app.gateway_client import is_gateway_enabled
            assert is_gateway_enabled() is False


class TestIsAIEnabledGatewayMode:
    """Verify is_ai_enabled returns True when gateway is configured."""

    def test_ai_enabled_via_gateway(self, app):
        from app.routes.ai import is_ai_enabled
        with app.app_context():
            with patch.dict(os.environ, GATEWAY_ENV, clear=True):
                assert is_ai_enabled() is True

    def test_ai_enabled_via_direct(self, app):
        """Legacy path still works."""
        from app.routes.ai import is_ai_enabled
        direct_env = {
            "AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com/",
            "AZURE_OPENAI_DEPLOYMENT": "gpt-4o-mini",
        }
        with app.app_context():
            with patch.dict(os.environ, direct_env, clear=True):
                assert is_ai_enabled() is True


class TestSuggestTopicsGateway:
    """Test /api/ai/suggest-topics through the gateway path."""

    @patch("app.routes.ai.gateway_call")
    def test_suggest_topics_via_gateway(self, mock_gw_call, app, client):
        mock_gw_call.return_value = {
            "success": True,
            "topics": ["Azure OpenAI", "RAG Pattern"],
            "usage": {"model": "gpt-4o-mini", "prompt_tokens": 50,
                      "completion_tokens": 20, "total_tokens": 70},
        }
        with app.app_context():
            test_user = User.query.first()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(test_user.id)

        with patch.dict(os.environ, GATEWAY_ENV):
            resp = client.post(
                "/api/ai/suggest-topics",
                json={"call_notes": "We discussed Azure OpenAI and RAG patterns."},
            )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert len(data["topics"]) == 2

        # Gateway should have been called with the right endpoint
        mock_gw_call.assert_called_once_with(
            "/v1/suggest-topics",
            {"call_notes": "We discussed Azure OpenAI and RAG patterns."},
        )

        # Check audit log
        with app.app_context():
            log = AIQueryLog.query.order_by(AIQueryLog.id.desc()).first()
            assert log is not None
            assert log.success is True
            assert log.model == "gpt-4o-mini"


class TestMatchMilestoneGateway:
    """Test /api/ai/match-milestone through the gateway path."""

    @patch("app.routes.ai.gateway_call")
    def test_match_milestone_via_gateway(self, mock_gw_call, app, client):
        mock_gw_call.return_value = {
            "success": True,
            "milestone_id": "MS-123",
            "reason": "Matches the AKS discussion",
            "usage": {},
        }
        with app.app_context():
            test_user = User.query.first()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(test_user.id)

        milestones = [
            {"id": "MS-123", "name": "AKS Deploy", "status": "Active",
             "opportunity": "Contoso", "workload": "Containers"},
        ]
        with patch.dict(os.environ, GATEWAY_ENV):
            resp = client.post(
                "/api/ai/match-milestone",
                json={
                    "call_notes": "Discussed Kubernetes deployment strategies and AKS best practices.",
                    "milestones": milestones,
                },
            )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["matched_milestone_id"] == "MS-123"


class TestAnalyzeCallGateway:
    """Test /api/ai/analyze-call through the gateway path."""

    @patch("app.routes.ai.gateway_call")
    def test_analyze_call_via_gateway(self, mock_gw_call, app, client):
        mock_gw_call.return_value = {
            "success": True,
            "topics": ["Azure Kubernetes Service", "Cost Optimization"],
            "usage": {"model": "gpt-4o-mini", "prompt_tokens": 80,
                      "completion_tokens": 30, "total_tokens": 110},
        }
        with app.app_context():
            test_user = User.query.first()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(test_user.id)

        with patch.dict(os.environ, GATEWAY_ENV):
            resp = client.post(
                "/api/ai/analyze-call",
                json={"call_notes": "Customer wants to optimize AKS costs and explore reserved instances."},
            )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert len(data["topics"]) == 2

        # Topics should have been created in DB
        with app.app_context():
            for t in data["topics"]:
                assert Topic.query.get(t["id"]) is not None


class TestEngagementSummaryGateway:
    """Test /api/ai/generate-engagement-summary through the gateway path."""

    @patch("app.routes.ai.gateway_call")
    def test_engagement_summary_via_gateway(self, mock_gw_call, app, client):
        mock_gw_call.return_value = {
            "success": True,
            "summary": "Key Individuals: John Doe\nTechnical Problem: Migration",
            "usage": {"model": "gpt-4o-mini", "prompt_tokens": 200,
                      "completion_tokens": 100, "total_tokens": 300},
        }
        with app.app_context():
            test_user = User.query.first()
            user_id = test_user.id
            customer = Customer(name="TestCo", tpid="12345")
            db.session.add(customer)
            db.session.flush()
            note = Note(
                customer_id=customer.id,
                call_date=date(2025, 1, 15),
                content="Discussed migration to Azure",
            )
            db.session.add(note)
            db.session.commit()
            customer_id = customer.id

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_id)

        with patch.dict(os.environ, GATEWAY_ENV):
            resp = client.post(
                "/api/ai/generate-engagement-summary",
                json={"customer_id": customer_id},
            )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert "summary" in data

        # Verify gateway was called with structured note data
        call_args = mock_gw_call.call_args
        assert call_args[0][0] == "/v1/engagement-summary"
        payload = call_args[0][1]
        assert payload["customer_name"] == "TestCo"
        assert len(payload["notes"]) == 1


class TestAdminAITestGateway:
    """Test /api/admin/ai-config/test through the gateway path."""

    @patch("app.gateway_client.gateway_call")
    def test_admin_ping_via_gateway(self, mock_gw_call, app, client):
        mock_gw_call.return_value = {
            "success": True,
            "status": "ok",
            "response": "Connection successful!",
        }
        with app.app_context():
            test_user = User.query.first()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(test_user.id)

        with patch.dict(os.environ, GATEWAY_ENV):
            resp = client.post("/api/admin/ai-config/test")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["mode"] == "gateway"

    @patch("app.gateway_client.gateway_call")
    def test_admin_ping_gateway_failure(self, mock_gw_call, app, client):
        from app.gateway_client import GatewayError
        mock_gw_call.side_effect = GatewayError("Connection refused")

        with app.app_context():
            test_user = User.query.first()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(test_user.id)

        with patch.dict(os.environ, GATEWAY_ENV):
            resp = client.post("/api/admin/ai-config/test")
        data = resp.get_json()
        assert resp.status_code == 400
        assert data["success"] is False
        assert "Gateway test failed" in data["error"]
