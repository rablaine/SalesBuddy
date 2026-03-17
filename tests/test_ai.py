"""
Tests for AI-powered features (gateway-only).

All AI calls go through the APIM gateway.  AI is always enabled —
the onboarding wizard enforces consent before users access the product.
"""

import json
from datetime import datetime
from unittest.mock import patch, MagicMock
import pytest
from app import db
from app.models import AIQueryLog, Topic, User, UserPreference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_admin(app):
    """Promote the first user to admin."""
    with app.app_context():
        user = User.query.first()
        user.is_admin = True
        db.session.commit()


# ---------------------------------------------------------------------------
# Consent grant / revoke endpoint tests
# ---------------------------------------------------------------------------

class TestConsentEndpoints:
    """Test the /api/admin/ai-enable and ai-disable endpoints."""

    @patch('app.gateway_client.check_ai_consent', return_value={
        'status': 'ok', 'consented': True, 'error': None, 'needs_relogin': False
    })
    def test_grant_enables_ai(self, mock_consent, app, client):
        """Granting consent should set ai_enabled = True."""
        _make_admin(app)

        resp = client.post('/api/admin/ai-enable')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['ai_enabled'] is True

        with app.app_context():
            prefs = UserPreference.query.first()
            assert prefs.ai_enabled is True

    @patch('app.gateway_client.check_ai_consent', return_value={
        'status': 'needs_relogin', 'consented': False,
        'error': 'consent_required', 'needs_relogin': True
    })
    def test_grant_rejected_without_consent(self, mock_consent, app, client):
        """Granting without consent returns 403."""
        _make_admin(app)
        resp = client.post('/api/admin/ai-enable')
        assert resp.status_code == 403
        data = resp.get_json()
        assert data['success'] is False
        assert data['ai_enabled'] is False

    def test_revoke_disables_ai(self, app, client):
        """Revoking should set ai_enabled = False."""
        _make_admin(app)

        resp = client.post('/api/admin/ai-disable')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['ai_enabled'] is False

        with app.app_context():
            prefs = UserPreference.query.first()
            assert prefs.ai_enabled is False


# ---------------------------------------------------------------------------
# AI connection test endpoint
# ---------------------------------------------------------------------------

class TestAIConnection:
    """Test AI connection testing via gateway."""

    @patch('app.gateway_client.gateway_call')
    def test_connection_test_success(self, mock_gw, app, client):
        """Successful gateway ping returns 200."""
        _make_admin(app)
        mock_gw.return_value = {'response': 'pong', 'success': True}

        resp = client.post('/api/admin/ai-config/test', json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'successful' in data['message'].lower()

    @patch('app.gateway_client.gateway_call')
    def test_connection_test_failure(self, mock_gw, app, client):
        """Failed gateway ping returns error."""
        _make_admin(app)
        from app.gateway_client import GatewayError
        mock_gw.side_effect = GatewayError("Connection refused")

        resp = client.post('/api/admin/ai-config/test', json={})
        data = resp.get_json()
        assert data['success'] is False
        assert 'failed' in data['error'].lower() or 'connection' in data['error'].lower()


# ---------------------------------------------------------------------------
# Topic suggestion tests
# ---------------------------------------------------------------------------

class TestAISuggestions:
    """Test AI topic suggestion via gateway."""

    @patch('app.routes.ai.gateway_call')
    def test_suggest_topics_success(self, mock_gw, app, client):
        """Successful gateway topic suggestion."""
        mock_gw.return_value = {
            'topics': ['Azure Functions', 'API Management', 'Serverless'],
            'usage': {'model': 'gpt-4o-mini', 'prompt_tokens': 100,
                      'completion_tokens': 50, 'total_tokens': 150},
        }

        resp = client.post('/api/ai/suggest-topics', json={
            'call_notes': 'Discussed Azure Functions and API Management for serverless architecture'
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert len(data['topics']) == 3
        assert any('azure functions' in t['name'].lower() for t in data['topics'])

        # Verify existing_topics was passed to gateway
        call_args = mock_gw.call_args
        assert 'existing_topics' in call_args[0][1]

        # Topics should NOT be created in DB (deferred until note save)
        with app.app_context():
            assert Topic.query.count() == 0
            log = AIQueryLog.query.first()
            assert log is not None
            assert log.success is True

    @patch('app.routes.ai.gateway_call')
    def test_suggest_topics_reuses_existing(self, mock_gw, app, client):
        """Existing topics are reused (case-insensitive), new ones returned with id=None."""
        with app.app_context():
            existing = Topic(name='azure functions')
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id

        mock_gw.return_value = {
            'topics': ['Azure Functions', 'Cosmos DB'],
            'usage': {},
        }

        resp = client.post('/api/ai/suggest-topics', json={
            'call_notes': 'Discussed Azure Functions and Cosmos DB'
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['topics']) == 2
        assert any(t['id'] == existing_id for t in data['topics'])
        # New topic should have id=None (deferred creation)
        assert any(t['id'] is None and t['name'] == 'Cosmos DB' for t in data['topics'])
        with app.app_context():
            # Only the pre-existing topic should be in DB
            assert Topic.query.count() == 1

        # Verify existing topic name was sent to gateway
        call_args = mock_gw.call_args
        assert 'azure functions' in call_args[0][1]['existing_topics']

    def test_suggest_topics_requires_call_notes(self, app, client):
        """call_notes parameter is required."""
        resp = client.post('/api/ai/suggest-topics', json={})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False


# ---------------------------------------------------------------------------
# Audit logging tests
# ---------------------------------------------------------------------------

class TestAuditLogging:
    """Test AI audit logging."""

    @patch('app.routes.ai.gateway_call')
    def test_audit_log_success(self, mock_gw, app, client):
        """Successful calls are logged."""
        mock_gw.return_value = {'topics': ['Topic1', 'Topic2'], 'usage': {}}

        client.post('/api/ai/suggest-topics', json={
            'call_notes': 'Test content for audit log'
        })

        with app.app_context():
            log = AIQueryLog.query.first()
            assert log is not None
            assert log.success is True
            assert 'Test content for audit log' in log.request_text
            assert 'Topic1' in log.response_text
            assert log.error_message is None

    @patch('app.routes.ai.gateway_call')
    def test_audit_log_failure(self, mock_gw, app, client):
        """Failed calls are logged with error."""
        from app.gateway_client import GatewayError
        mock_gw.side_effect = GatewayError("Rate limit exceeded")

        client.post('/api/ai/suggest-topics', json={
            'call_notes': 'This call will fail'
        })

        with app.app_context():
            log = AIQueryLog.query.first()
            assert log is not None
            assert log.success is False
            assert 'This call will fail' in log.request_text
            assert log.error_message is not None
            assert 'rate limit' in log.error_message.lower()

    @patch('app.routes.ai.gateway_call')
    def test_audit_log_truncation(self, mock_gw, app, client):
        """Long texts are truncated in audit log."""
        long_notes = 'A' * 1500
        mock_gw.return_value = {'topics': ['B' * 1500], 'usage': {}}

        client.post('/api/ai/suggest-topics', json={'call_notes': long_notes})

        with app.app_context():
            log = AIQueryLog.query.first()
            assert log is not None
            assert len(log.request_text) <= 1000
            assert len(log.response_text) <= 1000


# ---------------------------------------------------------------------------
# Engagement summary tests
# ---------------------------------------------------------------------------

class TestGenerateEngagementSummary:
    """Tests for the AI engagement summary generation endpoint."""

    def _create_customer_with_notes(self, app):
        """Helper to create a customer with notes for testing."""
        from app.models import Customer, Note, Seller, Territory
        with app.app_context():
            territory = Territory(name="Test Territory")
            seller = Seller(name="Test Seller", alias="tseller", seller_type="Growth")
            db.session.add_all([territory, seller])
            db.session.flush()
            customer = Customer(
                name="Test AI Customer", tpid=11111,
                seller_id=seller.id, territory_id=territory.id,
            )
            db.session.add(customer)
            db.session.flush()
            n1 = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 6, 15, 12, 0, 0),
                content="Discussed migrating their monolith to AKS. Key contact: Jane Smith, CTO.",
            )
            n2 = Note(
                customer_id=customer.id,
                call_date=datetime(2025, 7, 1, 14, 0, 0),
                content="Follow-up on AKS migration. Targeting Q3 go-live. Estimated $50K ACR.",
            )
            db.session.add_all([n1, n2])
            db.session.commit()
            return customer.id

    def test_returns_400_when_no_customer_id(self, app, client):
        """Should return 400 when customer_id is missing."""
        resp = client.post('/api/ai/generate-engagement-summary', json={})
        assert resp.status_code == 400
        assert 'customer_id' in resp.get_json()['error']

    def test_returns_404_when_customer_not_found(self, app, client):
        """Should return 404 for nonexistent customer."""
        resp = client.post(
            '/api/ai/generate-engagement-summary',
            json={'customer_id': 999999},
        )
        assert resp.status_code == 404

    def test_returns_400_when_no_notes(self, app, client):
        """Should return 400 when customer has no notes."""
        from app.models import Customer
        with app.app_context():
            customer = Customer(name="Empty Customer", tpid=22222)
            db.session.add(customer)
            db.session.commit()
            cid = customer.id
        resp = client.post(
            '/api/ai/generate-engagement-summary',
            json={'customer_id': cid},
        )
        assert resp.status_code == 400
        assert 'No notes' in resp.get_json()['error']

    @patch('app.routes.ai.gateway_call')
    def test_success_returns_summary(self, mock_gw, app, client):
        """Should return AI-generated summary on success."""
        customer_id = self._create_customer_with_notes(app)

        summary_text = (
            "Key Individuals & Titles: Jane Smith, CTO\n"
            "Technical/Business Problem: Monolith architecture\n"
            "Solution Resources: Azure Kubernetes Service (AKS)\n"
            "Business Outcome in Estimated $$ACR: $50K ACR\n"
            "Future Date/Timeline: Q3 go-live target"
        )
        mock_gw.return_value = {
            'success': True,
            'summary': summary_text,
            'usage': {},
        }

        resp = client.post(
            '/api/ai/generate-engagement-summary',
            json={'customer_id': customer_id},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'Jane Smith' in data['summary']
        assert data['note_count'] == 2

    @patch('app.routes.ai.gateway_call')
    def test_logs_success_to_ai_query_log(self, mock_gw, app, client):
        """Should create an AIQueryLog entry on success."""
        customer_id = self._create_customer_with_notes(app)
        mock_gw.return_value = {'success': True, 'summary': 'Summary here', 'usage': {}}

        client.post(
            '/api/ai/generate-engagement-summary',
            json={'customer_id': customer_id},
        )

        with app.app_context():
            log = AIQueryLog.query.filter(
                AIQueryLog.request_text.like('%Engagement summary%')
            ).first()
            assert log is not None
            assert log.success is True
            assert 'Test AI Customer' in log.request_text

    @patch('app.routes.ai.gateway_call')
    def test_logs_failure_to_ai_query_log(self, mock_gw, app, client):
        """Should create an AIQueryLog entry on failure."""
        customer_id = self._create_customer_with_notes(app)
        from app.gateway_client import GatewayError
        mock_gw.side_effect = GatewayError("API quota exceeded")

        resp = client.post(
            '/api/ai/generate-engagement-summary',
            json={'customer_id': customer_id},
        )
        assert resp.status_code == 500

        with app.app_context():
            log = AIQueryLog.query.filter(
                AIQueryLog.request_text.like('%Engagement summary%')
            ).first()
            assert log is not None
            assert log.success is False
            assert 'quota' in log.error_message.lower()


# ---------------------------------------------------------------------------
# Generate button visibility tests
# ---------------------------------------------------------------------------

class TestGenerateButtonVisibility:
    """Tests that the Generate button appears when linked notes exist."""

    def test_generate_button_visible_with_linked_notes(self, app, client):
        """Generate button should appear when engagement has linked notes."""
        from app.models import Customer, Note, Engagement
        with app.app_context():
            customer = Customer(name="Button Test", tpid=33333)
            db.session.add(customer)
            db.session.flush()
            engagement = Engagement(
                customer_id=customer.id, title="Test Engagement", status="Active",
            )
            db.session.add(engagement)
            db.session.flush()
            note = Note(
                customer_id=customer.id, call_date=datetime(2025, 6, 1),
                content="Test call",
            )
            db.session.add(note)
            db.session.flush()
            engagement.notes.append(note)
            db.session.commit()
            eid = engagement.id

        resp = client.get(f'/engagement/{eid}')
        assert resp.status_code == 200
        assert b'id="generateStoryBtn"' in resp.data

    def test_generate_button_hidden_when_no_notes(self, app, client):
        """Generate button hidden when engagement has no linked notes."""
        from app.models import Customer, Engagement
        with app.app_context():
            customer = Customer(name="No Logs Test", tpid=55555)
            db.session.add(customer)
            db.session.flush()
            engagement = Engagement(
                customer_id=customer.id, title="Empty Engagement", status="Active",
            )
            db.session.add(engagement)
            db.session.commit()
            eid = engagement.id

        resp = client.get(f'/engagement/{eid}')
        assert resp.status_code == 200
        assert b'id="generateStoryBtn"' not in resp.data


# ---------------------------------------------------------------------------
# Engagement story preview + apply tests
# ---------------------------------------------------------------------------

class TestEngagementStoryPreviewAndApply:
    """Tests for the two-step generate preview and apply workflow."""

    def _create_engagement_with_notes(self, app):
        """Helper to create an engagement with linked notes."""
        from app.models import Customer, Note, Engagement
        with app.app_context():
            customer = Customer(name="Preview Test Customer", tpid=77777)
            db.session.add(customer)
            db.session.flush()
            engagement = Engagement(
                customer_id=customer.id, title="Preview Engagement",
                status="Active", key_individuals="Old contact",
                technical_problem="Old problem",
            )
            db.session.add(engagement)
            db.session.flush()
            note = Note(
                customer_id=customer.id, call_date=datetime(2025, 6, 15),
                content="Discussed migration to Azure.",
            )
            db.session.add(note)
            db.session.flush()
            engagement.notes.append(note)
            db.session.commit()
            return engagement.id

    @patch('app.routes.ai.gateway_call')
    def test_preview_returns_current_and_generated(self, mock_gw, app, client):
        """Preview mode should return both current and generated values without saving."""
        eid = self._create_engagement_with_notes(app)
        mock_gw.return_value = {
            'story': {
                'key_individuals': 'Jane Smith, CTO',
                'technical_problem': 'Legacy monolith',
                'business_impact': 'Slow releases',
            },
            'usage': {},
        }

        resp = client.post(
            '/api/ai/generate-engagement-story',
            json={'engagement_id': eid, 'preview': True},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['current']['key_individuals'] == 'Old contact'
        assert data['current']['technical_problem'] == 'Old problem'
        assert data['story']['key_individuals'] == 'Jane Smith, CTO'

        # Verify nothing was saved
        from app.models import Engagement
        with app.app_context():
            eng = Engagement.query.get(eid)
            assert eng.key_individuals == 'Old contact'
            assert eng.technical_problem == 'Old problem'

    @patch('app.routes.ai.gateway_call')
    def test_non_preview_still_saves(self, mock_gw, app, client):
        """Without preview flag, generate should save fields (legacy behavior)."""
        eid = self._create_engagement_with_notes(app)
        mock_gw.return_value = {
            'story': {
                'key_individuals': 'Jane Smith, CTO',
                'technical_problem': 'Legacy monolith',
            },
            'usage': {},
        }

        resp = client.post(
            '/api/ai/generate-engagement-story',
            json={'engagement_id': eid},
        )
        assert resp.status_code == 200

        from app.models import Engagement
        with app.app_context():
            eng = Engagement.query.get(eid)
            assert eng.key_individuals == 'Jane Smith, CTO'

    def test_apply_updates_selected_fields(self, app, client):
        """Apply endpoint should update only the fields provided."""
        eid = self._create_engagement_with_notes(app)

        resp = client.post(
            '/api/ai/apply-engagement-story',
            json={
                'engagement_id': eid,
                'fields': {
                    'key_individuals': 'Jane Smith, CTO',
                    'business_impact': 'Slow release velocity',
                },
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        from app.models import Engagement
        with app.app_context():
            eng = Engagement.query.get(eid)
            assert eng.key_individuals == 'Jane Smith, CTO'
            assert eng.business_impact == 'Slow release velocity'
            # technical_problem should remain unchanged
            assert eng.technical_problem == 'Old problem'

    def test_apply_rejects_empty_fields(self, app, client):
        """Apply should return 400 when no fields are selected."""
        eid = self._create_engagement_with_notes(app)

        resp = client.post(
            '/api/ai/apply-engagement-story',
            json={'engagement_id': eid, 'fields': {}},
        )
        assert resp.status_code == 400
        assert 'No fields' in resp.get_json()['error']

    def test_apply_rejects_missing_engagement_id(self, app, client):
        """Apply should return 400 when engagement_id is missing."""
        resp = client.post(
            '/api/ai/apply-engagement-story',
            json={'fields': {'key_individuals': 'test'}},
        )
        assert resp.status_code == 400

    def test_apply_rejects_unknown_engagement(self, app, client):
        """Apply should return 404 for nonexistent engagement."""
        resp = client.post(
            '/api/ai/apply-engagement-story',
            json={'engagement_id': 999999, 'fields': {'key_individuals': 'test'}},
        )
        assert resp.status_code == 404

    def test_apply_ignores_disallowed_fields(self, app, client):
        """Apply should ignore fields not in the allowed list."""
        eid = self._create_engagement_with_notes(app)

        resp = client.post(
            '/api/ai/apply-engagement-story',
            json={
                'engagement_id': eid,
                'fields': {
                    'key_individuals': 'Jane',
                    'title': 'HACKED TITLE',
                },
            },
        )
        assert resp.status_code == 200

        from app.models import Engagement
        with app.app_context():
            eng = Engagement.query.get(eid)
            assert eng.key_individuals == 'Jane'
            assert eng.title == 'Preview Engagement'  # unchanged

    def test_apply_handles_target_date(self, app, client):
        """Apply should correctly parse and save target_date."""
        eid = self._create_engagement_with_notes(app)

        resp = client.post(
            '/api/ai/apply-engagement-story',
            json={
                'engagement_id': eid,
                'fields': {'target_date': '2025-12-31'},
            },
        )
        assert resp.status_code == 200

        from app.models import Engagement
        from datetime import date
        with app.app_context():
            eng = Engagement.query.get(eid)
            assert eng.target_date == date(2025, 12, 31)
