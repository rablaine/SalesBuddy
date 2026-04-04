"""
Tests for the APIM gateway integration.

Verifies that AI routes call the gateway client instead of Azure OpenAI
directly, and that the gateway client module works correctly.
"""
import json
import os
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from app.models import db, AIQueryLog, Topic, Customer, Note, User, UserPreference


# ---------------------------------------------------------------------------
# Gateway client module tests
# ---------------------------------------------------------------------------
class TestGatewayClientModule:
    """Unit tests for app.gateway_client."""

    def test_is_gateway_always_enabled(self):
        from app.gateway_client import is_gateway_enabled
        assert is_gateway_enabled() is True

    def test_verify_tenant_rejects_wrong_tenant(self):
        """_verify_tenant raises GatewayError for non-Microsoft tenants."""
        import base64
        import json as _json
        from app.gateway_client import _verify_tenant, GatewayError

        # Build a fake JWT with wrong tenant
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload_data = {"tid": "96d12531-723e-46c1-842b-0480739c7419", "aud": "test"}
        payload = base64.urlsafe_b64encode(
            _json.dumps(payload_data).encode()
        ).rstrip(b"=").decode()
        fake_jwt = f"{header}.{payload}.fakesig"

        with pytest.raises(GatewayError, match="non-Microsoft account"):
            _verify_tenant(fake_jwt)

    def test_verify_tenant_accepts_microsoft_tenant(self):
        """_verify_tenant passes silently for Microsoft corporate tenant."""
        import base64
        import json as _json
        from app.gateway_client import _verify_tenant

        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload_data = {"tid": "72f988bf-86f1-41af-91ab-2d7cd011db47", "aud": "test"}
        payload = base64.urlsafe_b64encode(
            _json.dumps(payload_data).encode()
        ).rstrip(b"=").decode()
        fake_jwt = f"{header}.{payload}.fakesig"

        # Should not raise
        _verify_tenant(fake_jwt)


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
            {"call_notes": "We discussed Azure OpenAI and RAG patterns.",
             "existing_topics": []},
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

        resp = client.post("/api/admin/ai-config/test")
        data = resp.get_json()
        assert resp.status_code == 400
        assert data["success"] is False
        assert "Gateway test failed" in data["error"]
