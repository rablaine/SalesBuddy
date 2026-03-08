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

from app.models import db, AIQueryLog, Topic, Customer, Note, User


# ---------------------------------------------------------------------------
# Gateway client module tests
# ---------------------------------------------------------------------------
class TestGatewayClientModule:
    """Unit tests for app.gateway_client."""

    def test_is_gateway_always_enabled(self):
        from app.gateway_client import is_gateway_enabled
        assert is_gateway_enabled() is True

    def test_verify_tenant_rejects_wrong_tenant(self):
        """_verify_tenant raises GatewayConsentError for non-Microsoft tenants."""
        import base64
        import json as _json
        from app.gateway_client import _verify_tenant, GatewayConsentError

        # Build a fake JWT with wrong tenant
        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload_data = {"tid": "96d12531-723e-46c1-842b-0480739c7419", "aud": "test"}
        payload = base64.urlsafe_b64encode(
            _json.dumps(payload_data).encode()
        ).rstrip(b"=").decode()
        fake_jwt = f"{header}.{payload}.fakesig"

        with pytest.raises(GatewayConsentError, match="non-Microsoft account"):
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

    def test_check_consent_wrong_tenant_returns_needs_relogin(self):
        """check_ai_consent returns needs_relogin when logged into wrong tenant."""
        import base64
        import json as _json
        from app.gateway_client import check_ai_consent

        header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload_data = {"tid": "96d12531-723e-46c1-842b-0480739c7419"}
        payload = base64.urlsafe_b64encode(
            _json.dumps(payload_data).encode()
        ).rstrip(b"=").decode()
        fake_jwt = f"{header}.{payload}.fakesig"

        mock_token = MagicMock()
        mock_token.token = fake_jwt
        mock_token.expires_on = 9999999999.0

        with patch("app.gateway_client.AzureCliCredential") as mock_cred_cls:
            mock_cred = MagicMock()
            mock_cred.get_token.return_value = mock_token
            mock_cred_cls.return_value = mock_cred

            # Clear any cached state
            import app.gateway_client as gc
            gc._credential = None
            gc._cached_token = None
            gc._token_expiry = 0

            result = check_ai_consent()

        assert result["consented"] is False
        assert result["needs_relogin"] is True
        assert "non-Microsoft" in result["error"]


class TestIsAIEnabledGatewayMode:
    """Verify is_ai_enabled returns True when gateway is available."""

    def test_ai_enabled_via_gateway(self, app):
        from app.routes.ai import is_ai_enabled
        with app.app_context():
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
