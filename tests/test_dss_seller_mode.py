"""
Tests for DSS seller mode features.
Covers: Phase 1 identity linking (alias save, auto-match, manual select),
Phase 4 note-opportunity attachment (model, save handler, route),
Phase 5 dynamic bucket resolution.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from app.models import (
    db, Note, Customer, Seller, Opportunity, UserPreference,
    notes_opportunities, CustomerRevenueData,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def dss_pref(app):
    """Set user role to DSS."""
    with app.app_context():
        pref = UserPreference.query.first()
        pref.user_role = 'dss'
        db.session.commit()
    yield


@pytest.fixture
def se_pref(app):
    """Ensure user role is SE."""
    with app.app_context():
        pref = UserPreference.query.first()
        pref.user_role = 'se'
        db.session.commit()
    yield


@pytest.fixture
def basic_data(app, sample_data):
    """Create basic data including a customer with tpid_url and a seller with alias."""
    with app.app_context():
        # Seller1 already has alias='alices' from sample_data
        return sample_data


# ============================================================================
# Phase 1: Identity Linking Tests
# ============================================================================

class TestSaveAlias:
    """Tests for POST /api/preferences/save-alias."""

    def test_save_alias_extracts_from_email(self, client, app):
        """Should extract alias from full email address."""
        resp = client.post('/api/preferences/save-alias',
                           json={'email': 'testuser@microsoft.com'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['alias'] == 'testuser'

        with app.app_context():
            pref = UserPreference.query.first()
            assert pref.my_seller_alias == 'testuser'

    def test_save_alias_rejects_plain_alias(self, client, app):
        """Should reject a plain alias without @ (requires email format)."""
        resp = client.post('/api/preferences/save-alias',
                           json={'email': 'plainuser'})
        assert resp.status_code == 400

    def test_save_alias_empty(self, client):
        """Should fail with empty email."""
        resp = client.post('/api/preferences/save-alias', json={'email': ''})
        assert resp.status_code == 400


class TestMatchSeller:
    """Tests for POST /api/preferences/match-seller."""

    def test_auto_match_found(self, client, app, basic_data):
        """Should auto-match when alias matches a seller."""
        with app.app_context():
            pref = UserPreference.query.first()
            pref.my_seller_alias = 'alices'
            db.session.commit()

        resp = client.post('/api/preferences/match-seller')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['matched'] is True
        assert data['seller_name'] == 'Alice Smith'

        with app.app_context():
            pref = UserPreference.query.first()
            assert pref.my_seller_id == basic_data['seller1_id']

    def test_auto_match_not_found(self, client, app, basic_data):
        """Should return sellers list when no match found."""
        with app.app_context():
            pref = UserPreference.query.first()
            pref.my_seller_alias = 'unknownalias'
            db.session.commit()

        resp = client.post('/api/preferences/match-seller')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['matched'] is False
        assert 'sellers' in data
        assert len(data['sellers']) >= 2

    def test_auto_match_case_insensitive(self, client, app, basic_data):
        """Auto-match should be case-insensitive."""
        with app.app_context():
            pref = UserPreference.query.first()
            pref.my_seller_alias = 'ALICES'
            db.session.commit()

        resp = client.post('/api/preferences/match-seller')
        data = resp.get_json()
        assert data['matched'] is True


class TestManualSellerSelect:
    """Tests for POST /api/preferences/my-seller."""

    def test_manual_select(self, client, app, basic_data):
        """Should save seller_id from manual selection."""
        resp = client.post('/api/preferences/my-seller',
                           json={'seller_id': basic_data['seller1_id']})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['my_seller_id'] == basic_data['seller1_id']

        with app.app_context():
            pref = UserPreference.query.first()
            assert pref.my_seller_id == basic_data['seller1_id']

    def test_manual_select_invalid_id(self, client, app):
        """Should fail with non-existent seller ID."""
        resp = client.post('/api/preferences/my-seller',
                           json={'seller_id': 99999})
        assert resp.status_code == 404

    def test_manual_select_missing_id(self, client, app):
        """Should fail with missing seller_id."""
        resp = client.post('/api/preferences/my-seller', json={})
        assert resp.status_code == 400


# ============================================================================
# Phase 4: Note-Opportunity Model Tests
# ============================================================================

class TestNotesOpportunitiesModel:
    """Tests for the notes_opportunities association table."""

    def test_note_opportunity_association(self, app, basic_data):
        """Test linking a note to an opportunity."""
        with app.app_context():
            opp = Opportunity(
                msx_opportunity_id='opp-1234',
                name='Test Opportunity',
                opportunity_number='OPP-001',
                state='Open',
                estimated_value=50000.0,
                customer_id=basic_data['customer1_id'],
            )
            db.session.add(opp)
            db.session.flush()

            note = db.session.get(Note, basic_data['call1_id'])
            note.opportunities.append(opp)
            db.session.commit()

            # Verify
            note_fresh = db.session.get(Note, basic_data['call1_id'])
            assert len(note_fresh.opportunities) == 1
            assert note_fresh.opportunities[0].name == 'Test Opportunity'

            # Reverse relationship
            opp_fresh = db.session.get(Opportunity, opp.id)
            assert len(opp_fresh.notes) == 1
            assert opp_fresh.notes[0].id == basic_data['call1_id']

    def test_multiple_opportunities_per_note(self, app, basic_data):
        """Test linking multiple opportunities to a single note."""
        with app.app_context():
            opp1 = Opportunity(msx_opportunity_id='opp-a', name='Opp A',
                               customer_id=basic_data['customer1_id'])
            opp2 = Opportunity(msx_opportunity_id='opp-b', name='Opp B',
                               customer_id=basic_data['customer1_id'])
            db.session.add_all([opp1, opp2])
            db.session.flush()

            note = db.session.get(Note, basic_data['call1_id'])
            note.opportunities.extend([opp1, opp2])
            db.session.commit()

            note_fresh = db.session.get(Note, basic_data['call1_id'])
            assert len(note_fresh.opportunities) == 2


# ============================================================================
# Phase 4: Opportunity Save Handler Tests
# ============================================================================

class TestHandleOpportunity:
    """Tests for _handle_opportunity during note create/edit."""

    def test_dss_note_create_with_opportunity(self, client, app, basic_data, dss_pref):
        """DSS user creating a note should link opportunities."""
        resp = client.post('/note/new', data={
            'customer_id': basic_data['customer1_id'],
            'call_date': '2026-03-14',
            'content': 'Discussed cloud migration opportunity with Acme.',
            'opportunity_msx_id': 'opp-new-1',
            'opportunity_name': 'Cloud Migration',
            'opportunity_number': 'OPP-100',
            'opportunity_state': 'Open',
            'opportunity_estimated_value': '75000',
            'opportunity_url': 'https://msx.example.com/opp/1',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.filter(Note.content.contains('cloud migration')).first()
            assert note is not None
            assert len(note.opportunities) == 1
            assert note.opportunities[0].name == 'Cloud Migration'
            assert note.opportunities[0].estimated_value == 75000.0

    def test_dss_note_create_no_opportunity(self, client, app, basic_data, dss_pref):
        """DSS user creating a note without opportunity should succeed."""
        resp = client.post('/note/new', data={
            'customer_id': basic_data['customer1_id'],
            'call_date': '2026-03-14',
            'content': 'Quick sync call with customer.',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.filter(Note.content.contains('Quick sync')).first()
            assert note is not None
            assert len(note.opportunities) == 0

    def test_se_note_create_uses_milestone(self, client, app, basic_data, se_pref):
        """SE user creating a note should NOT call _handle_opportunity."""
        resp = client.post('/note/new', data={
            'customer_id': basic_data['customer1_id'],
            'call_date': '2026-03-14',
            'content': 'Milestone discussion with customer.',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.filter(Note.content.contains('Milestone discussion')).first()
            assert note is not None
            assert len(note.opportunities) == 0

    def test_dss_note_edit_updates_opportunities(self, client, app, basic_data, dss_pref):
        """DSS user editing a note should update opportunity links."""
        # First create a note
        with app.app_context():
            note = Note(
                customer_id=basic_data['customer1_id'],
                call_date=datetime.now(timezone.utc),
                content='Edit test note.',
            )
            db.session.add(note)
            db.session.commit()
            note_id = note.id

        # Edit with opportunity
        resp = client.post(f'/note/{note_id}/edit', data={
            'customer_id': basic_data['customer1_id'],
            'call_date': '2026-03-14',
            'content': 'Edit test note - updated.',
            'opportunity_msx_id': 'opp-edit-1',
            'opportunity_name': 'Edited Opportunity',
            'opportunity_number': 'OPP-200',
            'opportunity_state': 'Open',
            'opportunity_estimated_value': '100000',
            'opportunity_url': '',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = db.session.get(Note, note_id)
            assert len(note.opportunities) == 1
            assert note.opportunities[0].name == 'Edited Opportunity'

    def test_existing_opportunity_reused(self, client, app, basic_data, dss_pref):
        """Should reuse existing Opportunity records by msx_opportunity_id."""
        with app.app_context():
            opp = Opportunity(
                msx_opportunity_id='opp-reuse-1',
                name='Existing Opp',
                customer_id=basic_data['customer1_id'],
            )
            db.session.add(opp)
            db.session.commit()
            opp_id = opp.id

        resp = client.post('/note/new', data={
            'customer_id': basic_data['customer1_id'],
            'call_date': '2026-03-14',
            'content': 'Reuse existing opportunity record.',
            'opportunity_msx_id': 'opp-reuse-1',
            'opportunity_name': 'Updated Name',
            'opportunity_number': '',
            'opportunity_state': 'Open',
            'opportunity_estimated_value': '',
            'opportunity_url': '',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            # Should not create a duplicate
            opps = Opportunity.query.filter_by(msx_opportunity_id='opp-reuse-1').all()
            assert len(opps) == 1
            assert opps[0].id == opp_id
            assert opps[0].name == 'Updated Name'


# ============================================================================
# Phase 4: Opportunities-for-Customer Route Tests
# ============================================================================

class TestOpportunitiesForCustomer:
    """Tests for GET /api/msx/opportunities-for-customer/<id>."""

    @patch('app.routes.msx.extract_account_id_from_url', return_value='fake-account-guid')
    @patch('app.routes.msx.get_opportunities_by_account')
    def test_returns_opportunities(self, mock_get, mock_extract, client, basic_data):
        """Should return opportunities for a valid customer."""
        mock_get.return_value = {
            'success': True,
            'opportunities': [
                {'id': 'opp-1', 'name': 'Test Opp', 'number': 'OPP-001',
                 'state': 'Open', 'estimated_value': 50000},
            ],
            'count': 1,
        }
        resp = client.get(f'/api/msx/opportunities-for-customer/{basic_data["customer1_id"]}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['count'] == 1

    def test_customer_not_found(self, client):
        """Should return error for non-existent customer."""
        resp = client.get('/api/msx/opportunities-for-customer/99999')
        data = resp.get_json()
        assert data['success'] is False

    def test_customer_no_tpid(self, client, basic_data):
        """Should return needs_tpid for customer without MSX link."""
        # customer2 has no tpid_url
        resp = client.get(f'/api/msx/opportunities-for-customer/{basic_data["customer2_id"]}')
        data = resp.get_json()
        assert data['success'] is False
        assert data.get('needs_tpid') is True


# ============================================================================
# Phase 4: AI Opportunity Match Endpoint Tests
# ============================================================================

class TestAIMatchOpportunity:
    """Tests for POST /api/ai/match-opportunity."""

    @patch('app.routes.ai.gateway_call')
    def test_match_success(self, mock_gw, client):
        """Should return matched opportunity ID on success."""
        mock_gw.return_value = {
            'opportunity_id': 'opp-matched',
            'reason': 'Best content match',
        }
        resp = client.post('/api/ai/match-opportunity', json={
            'call_notes': 'We discussed migrating their SQL Server workloads to Azure SQL.',
            'opportunities': [
                {'id': 'opp-matched', 'name': 'SQL Migration', 'number': 'OPP-1'},
                {'id': 'opp-other', 'name': 'VM Optimization', 'number': 'OPP-2'},
            ],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['matched_opportunity_id'] == 'opp-matched'

    @patch('app.routes.ai.gateway_call')
    def test_match_no_match(self, mock_gw, client):
        """Should return null when no opportunity matches."""
        mock_gw.return_value = {
            'opportunity_id': None,
            'reason': 'No match',
        }
        resp = client.post('/api/ai/match-opportunity', json={
            'call_notes': 'Discussed internal team restructuring, no Azure work.',
            'opportunities': [
                {'id': 'opp-1', 'name': 'SQL Migration', 'number': 'OPP-1'},
            ],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['matched_opportunity_id'] is None

    def test_short_notes_rejected(self, client):
        """Should reject call notes under 20 chars."""
        resp = client.post('/api/ai/match-opportunity', json={
            'call_notes': 'Short',
            'opportunities': [{'id': '1', 'name': 'Test'}],
        })
        assert resp.status_code == 400

    def test_empty_opportunities_rejected(self, client):
        """Should reject empty opportunities list."""
        resp = client.post('/api/ai/match-opportunity', json={
            'call_notes': 'A sufficiently long note about Azure.',
            'opportunities': [],
        })
        assert resp.status_code == 400


# ============================================================================
# Phase 4: Note View Opportunity Display Tests
# ============================================================================

class TestNoteViewOpportunities:
    """Tests for opportunity display on note view page."""

    def test_note_view_shows_opportunity_card(self, client, app, basic_data):
        """Note view should display linked opportunities as cards with status and value."""
        with app.app_context():
            opp = Opportunity(
                msx_opportunity_id='opp-view-1',
                name='Visible Opportunity',
                state='Open',
                estimated_value=75000,
                owner_name='Jane Doe',
                customer_id=basic_data['customer1_id'],
            )
            db.session.add(opp)
            db.session.flush()

            note = db.session.get(Note, basic_data['call1_id'])
            note.opportunities.append(opp)
            db.session.commit()

        resp = client.get(f'/note/{basic_data["call1_id"]}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Visible Opportunity' in html
        assert 'bi-briefcase' in html
        assert 'border-info' in html  # card style
        assert 'Open' in html  # state badge
        assert '$75,000' in html  # estimated value
        assert 'Jane Doe' in html  # owner name

    def test_notes_list_shows_opportunity_badges(self, client, app, basic_data):
        """Notes list should display opportunity badges on notes."""
        with app.app_context():
            opp = Opportunity(
                msx_opportunity_id='opp-list-1',
                name='List Opp Badge',
                state='Open',
                customer_id=basic_data['customer1_id'],
            )
            db.session.add(opp)
            db.session.flush()

            note = db.session.get(Note, basic_data['call1_id'])
            note.opportunities.append(opp)
            db.session.commit()

        resp = client.get('/notes')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'List Opp Badge' in html
        assert 'bi-briefcase' in html


# ============================================================================
# Phase 5: Dynamic Bucket Resolution Tests
# ============================================================================

class TestDynamicBuckets:
    """Tests for revenue bucket generalization."""

    def test_revenue_customer_view_dynamic_buckets(self, client, app, basic_data):
        """Revenue customer view should discover buckets from data."""
        with app.app_context():
            from datetime import date
            from app.models import RevenueImport
            # Create an import record first
            rev_import = RevenueImport(
                filename='test.csv',
                record_count=2,
            )
            db.session.add(rev_import)
            db.session.flush()

            for bucket_name in ['Custom Bucket A', 'Custom Bucket B']:
                rev = CustomerRevenueData(
                    customer_name='Acme Corp',
                    tpid='1001',
                    bucket=bucket_name,
                    customer_id=basic_data['customer1_id'],
                    fiscal_month='FY26-Jan',
                    month_date=date(2026, 1, 1),
                    revenue=1000.0,
                    last_import_id=rev_import.id,
                )
                db.session.add(rev)
            db.session.commit()

        resp = client.get(f'/revenue/customer/{basic_data["customer1_id"]}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Custom Bucket A' in html
        assert 'Custom Bucket B' in html


# ============================================================================
# Settings & Customers List Fix Tests
# ============================================================================

class TestSettingsRoleDisplay:
    """Tests for settings page in DSS mode."""

    def test_settings_loads_in_dss_mode(self, client, app, dss_pref):
        """Settings page should load successfully for DSS users."""
        resp = client.get('/preferences')
        assert resp.status_code == 200


class TestCustomersListSellerBadge:
    """Tests for seller badge visibility in customers list."""

    def test_seller_badge_hidden_in_seller_mode(self, client, app, basic_data, dss_pref):
        """Seller badges should be hidden in seller mode."""
        with app.app_context():
            pref = UserPreference.query.first()
            pref.my_seller_id = basic_data['seller1_id']
            db.session.commit()

        resp = client.get('/customers')
        assert resp.status_code == 200
        html = resp.data.decode()
        # The seller badge should not appear in customer rows
        # (bi-person may appear in navbar, so check for seller name badge specifically)
        assert 'badge' in html  # page renders
        # In seller mode, seller name should not appear as a badge link
        from app.models import Seller
        with app.app_context():
            seller = db.session.get(Seller, basic_data['seller1_id'])
            seller_view_url = f'/seller/{seller.id}'
        assert seller_view_url not in html
