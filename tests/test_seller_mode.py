"""
Tests for DSS seller mode feature.
Covers role selection API, seller mode activation/deactivation,
context processor logic, and server-side query scoping.
"""
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# ============================================================================
# Role Selection API
# ============================================================================

class TestRoleSelection:
    """Tests for POST /api/preferences/user-role."""

    def test_set_role_se(self, client, app):
        """Setting role to 'se' stores it in preferences."""
        response = client.post('/api/preferences/user-role', json={'role': 'se'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['user_role'] == 'se'

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.user_role == 'se'

    def test_set_role_dss(self, client, app):
        """Setting role to 'dss' stores it in preferences."""
        response = client.post('/api/preferences/user-role', json={'role': 'dss'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['user_role'] == 'dss'

    def test_set_role_invalid(self, client):
        """Invalid role values are rejected."""
        response = client.post('/api/preferences/user-role', json={'role': 'admin'})
        assert response.status_code == 400

    def test_set_role_missing(self, client):
        """Missing role field is rejected."""
        response = client.post('/api/preferences/user-role', json={})
        assert response.status_code == 400


# ============================================================================
# Seller Mode Activation / Deactivation
# ============================================================================

class TestSellerModeActivation:
    """Tests for seller mode activate/deactivate endpoints."""

    def test_activate_seller_mode(self, client, app, sample_data):
        """Activating seller mode sets session variable."""
        seller_id = sample_data['seller1_id']
        response = client.post(f'/api/seller-mode/activate/{seller_id}')
        assert response.status_code == 200
        data = response.get_json()
        assert data['seller_mode'] is True
        assert data['seller_id'] == seller_id

    def test_activate_seller_mode_invalid_id(self, client):
        """Activating seller mode with non-existent seller returns 404."""
        response = client.post('/api/seller-mode/activate/99999')
        assert response.status_code == 404

    def test_deactivate_seller_mode(self, client, app, sample_data):
        """Deactivating seller mode clears session."""
        # Activate first
        seller_id = sample_data['seller1_id']
        client.post(f'/api/seller-mode/activate/{seller_id}')

        # Deactivate
        response = client.post('/api/seller-mode/deactivate')
        assert response.status_code == 200
        data = response.get_json()
        assert data['seller_mode'] is False


# ============================================================================
# Shared Helper
# ============================================================================

class TestSellerModeHelper:
    """Tests for the get_seller_mode_seller_id() service helper."""

    def test_returns_none_when_no_mode(self, app):
        """Returns None when no seller mode is active."""
        with app.test_request_context():
            from app.services.seller_mode import get_seller_mode_seller_id
            assert get_seller_mode_seller_id() is None

    def test_returns_dss_seller_id(self, app):
        """Returns my_seller_id for DSS users."""
        with app.app_context():
            from app.models import db, UserPreference, Seller
            # Ensure a seller with that ID exists
            seller = Seller.query.first()
            if not seller:
                seller = Seller(name='Test Seller', alias='ts')
                db.session.add(seller)
                db.session.flush()

            seller_id = seller.id
            pref = UserPreference.query.first()
            pref.user_role = 'dss'
            pref.my_seller_id = seller_id
            db.session.commit()

        with app.test_request_context():
            from app.services.seller_mode import get_seller_mode_seller_id
            result = get_seller_mode_seller_id()
            assert result == seller_id

        # Cleanup
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.user_role = None
            pref.my_seller_id = None
            db.session.commit()

    def test_returns_session_seller_id_for_se(self, app):
        """Returns session seller ID for SE users in seller mode."""
        with app.test_request_context():
            from flask import session
            from app.services.seller_mode import get_seller_mode_seller_id
            session['seller_mode_seller_id'] = 42
            assert get_seller_mode_seller_id() == 42


# ============================================================================
# Server-Side Query Scoping
# ============================================================================

class TestQueryScoping:
    """Tests that routes filter data correctly in seller mode."""

    def test_notes_list_scoped_in_seller_mode(self, client, app, sample_data):
        """Notes list only shows notes for the active seller's customers."""
        seller1_id = sample_data['seller1_id']

        # Activate seller mode for seller1 (Alice Smith - has Acme Corp, Initech)
        client.post(f'/api/seller-mode/activate/{seller1_id}')

        response = client.get('/notes')
        assert response.status_code == 200
        # Should see Acme Corp note (seller1's customer)
        assert b'Acme Corp' in response.data
        # Should NOT see Globex Inc note (seller2's customer)
        assert b'Globex Inc' not in response.data

    def test_notes_list_unscoped_without_seller_mode(self, client, app, sample_data):
        """Notes list shows all notes when seller mode is not active."""
        # Make sure seller mode is off
        client.post('/api/seller-mode/deactivate')

        response = client.get('/notes')
        assert response.status_code == 200
        assert b'Acme Corp' in response.data
        assert b'Globex Inc' in response.data

    def test_customers_list_scoped_in_seller_mode(self, client, app, sample_data):
        """Customers list only shows the active seller's customers."""
        seller1_id = sample_data['seller1_id']
        client.post(f'/api/seller-mode/activate/{seller1_id}')

        response = client.get('/customers')
        assert response.status_code == 200
        assert b'Acme Corp' in response.data
        assert b'Initech' in response.data
        # Globex belongs to seller2
        assert b'Globex Inc' not in response.data

    def test_customers_list_unscoped_without_seller_mode(self, client, app, sample_data):
        """Customers list shows all customers when seller mode is not active."""
        client.post('/api/seller-mode/deactivate')

        response = client.get('/customers')
        assert response.status_code == 200
        assert b'Acme Corp' in response.data
        assert b'Globex Inc' in response.data

    def test_search_scoped_in_seller_mode(self, client, app, sample_data):
        """Search results are scoped to active seller's customers."""
        seller1_id = sample_data['seller1_id']
        client.post(f'/api/seller-mode/activate/{seller1_id}')

        # Search with text that appears in both notes
        response = client.get('/search?q=migration')
        assert response.status_code == 200
        # Seller1's note has "migration" in it
        assert b'Acme Corp' in response.data

    def test_index_loads_in_seller_mode(self, client, app, sample_data):
        """Home page loads successfully in seller mode."""
        seller1_id = sample_data['seller1_id']
        client.post(f'/api/seller-mode/activate/{seller1_id}')

        response = client.get('/')
        assert response.status_code == 200

    def test_engagements_hub_loads_in_seller_mode(self, client, app, sample_data):
        """Engagements hub loads successfully in seller mode."""
        seller1_id = sample_data['seller1_id']
        client.post(f'/api/seller-mode/activate/{seller1_id}')

        response = client.get('/engagements')
        assert response.status_code == 200

    def test_engagements_api_scoped_in_seller_mode(self, client, app, sample_data):
        """Engagements API returns only the active seller's engagements."""
        from app.models import db, Engagement, Customer
        seller1_id = sample_data['seller1_id']
        seller2_id = sample_data['seller2_id']

        with app.app_context():
            # Create engagements for both sellers' customers
            c1 = Customer.query.filter_by(seller_id=seller1_id).first()
            c2 = Customer.query.filter_by(seller_id=seller2_id).first()
            eng1 = Engagement(
                title='Seller1 Engagement',
                customer_id=c1.id,
                status='Active',
            )
            eng2 = Engagement(
                title='Seller2 Engagement',
                customer_id=c2.id,
                status='Active',
            )
            db.session.add_all([eng1, eng2])
            db.session.commit()
            eng1_id = eng1.id
            eng2_id = eng2.id

        # Activate seller mode for seller1
        client.post(f'/api/seller-mode/activate/{seller1_id}')

        response = client.get('/api/engagements/all')
        assert response.status_code == 200
        data = response.get_json()
        titles = [e['title'] for e in data['engagements']]
        assert 'Seller1 Engagement' in titles
        assert 'Seller2 Engagement' not in titles

        # Cleanup
        with app.app_context():
            Engagement.query.filter(Engagement.id.in_([eng1_id, eng2_id])).delete()
            db.session.commit()

    def test_revenue_dashboard_loads_in_seller_mode(self, client, app, sample_data):
        """Revenue dashboard loads successfully in seller mode."""
        seller1_id = sample_data['seller1_id']
        client.post(f'/api/seller-mode/activate/{seller1_id}')

        response = client.get('/revenue')
        assert response.status_code == 200


# ============================================================================
# Dev Toggle
# ============================================================================

class TestDevToggle:
    """Tests for POST /api/admin/dev-toggle-role."""

    def test_dev_toggle_requires_dev_env(self, client):
        """Dev toggle rejects requests in non-development environments."""
        with patch.dict(os.environ, {'FLASK_ENV': 'production'}):
            response = client.post('/api/admin/dev-toggle-role')
            assert response.status_code == 403

    def test_dev_toggle_switches_to_dss(self, client, app):
        """Dev toggle switches from SE to DSS mode."""
        with patch.dict(os.environ, {'FLASK_ENV': 'development'}):
            # Ensure starting as SE
            with app.app_context():
                from app.models import db, UserPreference
                pref = UserPreference.query.first()
                pref.user_role = 'se'
                pref.my_seller_id = None
                db.session.commit()

            response = client.post('/api/admin/dev-toggle-role')
            assert response.status_code == 200
            data = response.get_json()
            assert data['user_role'] == 'dss'
            assert data['my_seller_id'] == 7

    def test_dev_toggle_switches_to_se(self, client, app):
        """Dev toggle switches from DSS back to SE mode."""
        with patch.dict(os.environ, {'FLASK_ENV': 'development'}):
            # Set up as DSS first
            with app.app_context():
                from app.models import db, UserPreference
                pref = UserPreference.query.first()
                pref.user_role = 'dss'
                pref.my_seller_id = 7
                db.session.commit()

            response = client.post('/api/admin/dev-toggle-role')
            assert response.status_code == 200
            data = response.get_json()
            assert data['user_role'] == 'se'
            assert data['my_seller_id'] is None

            # Cleanup
            with app.app_context():
                from app.models import db, UserPreference
                pref = UserPreference.query.first()
                pref.user_role = None
                pref.my_seller_id = None
                db.session.commit()
