"""Tests for TPID customer import feature."""

import json
from unittest.mock import patch, MagicMock

import pytest


class TestTpidLookup:
    """Tests for /api/customer/tpid-lookup endpoint."""

    def test_lookup_requires_tpid(self, client):
        """Should reject request without TPID."""
        resp = client.post('/api/customer/tpid-lookup',
                           json={},
                           content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert 'required' in data['error'].lower()

    def test_lookup_rejects_non_numeric(self, client):
        """Should reject non-numeric TPID."""
        resp = client.post('/api/customer/tpid-lookup',
                           json={'tpid': 'abc'},
                           content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'number' in data['error'].lower()

    def test_lookup_detects_existing_customer(self, client, app, sample_data):
        """Should return 409 if TPID already exists in DB."""
        with app.app_context():
            from app.models import db, Customer
            cust = Customer.query.get(sample_data['customer1_id'])
            tpid = cust.tpid

        resp = client.post('/api/customer/tpid-lookup',
                           json={'tpid': str(tpid)},
                           content_type='application/json')
        assert resp.status_code == 409
        data = resp.get_json()
        assert data['success'] is False
        assert 'already exists' in data['error']
        assert data['existing_id'] == sample_data['customer1_id']

    @patch('app.services.msx_api.batch_query_account_teams')
    @patch('app.services.msx_api.get_account_details')
    @patch('app.services.msx_api.lookup_account_by_tpid')
    def test_lookup_returns_account_preview(self, mock_lookup, mock_details,
                                            mock_teams, client, app):
        """Should return account preview after successful MSX lookup."""
        mock_lookup.return_value = {
            'success': True,
            'accounts': [{
                'accountid': 'abc-123',
                'name': 'Test Corp',
                'msp_mstopparentid': '99999',
                'parenting_level': 'Top',
                'url': 'https://msxurl',
                'websiteurl': 'https://www.testcorp.com',
            }],
        }
        mock_details.return_value = {
            'success': True,
            'account': {
                'name': 'Test Corp',
                'tpid': '99999',
                'vertical': 'Healthcare',
                'vertical_category': None,
            },
            'territory': {'name': 'East.SMECC.MAA.0601'},
            'seller': None,
            'pod': None,
        }
        mock_teams.return_value = {
            'success': True,
            'account_sellers': {
                'abc-123': {'name': 'Jane Smith', 'type': 'Growth', 'user_id': 'u1'},
            },
            'unique_sellers': {},
            'account_ses': {},
        }

        resp = client.post('/api/customer/tpid-lookup',
                           json={'tpid': '99999'},
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        acct = data['account']
        assert acct['name'] == 'Test Corp'
        assert acct['tpid'] == '99999'
        assert acct['territory_name'] == 'East.SMECC.MAA.0601'
        assert acct['seller_name'] == 'Jane Smith'
        assert acct['website'] == 'testcorp.com'

    @patch('app.services.msx_api.lookup_account_by_tpid')
    def test_lookup_no_accounts_found(self, mock_lookup, client):
        """Should return 404 if no MSX accounts match."""
        mock_lookup.return_value = {'success': True, 'accounts': []}
        resp = client.post('/api/customer/tpid-lookup',
                           json={'tpid': '11111'},
                           content_type='application/json')
        assert resp.status_code == 404
        data = resp.get_json()
        assert 'No accounts found' in data['error']

    @patch('app.services.msx_api.lookup_account_by_tpid')
    def test_lookup_msx_error(self, mock_lookup, client):
        """Should return 502 on MSX API failure."""
        mock_lookup.return_value = {'success': False, 'error': 'Timeout'}
        resp = client.post('/api/customer/tpid-lookup',
                           json={'tpid': '11111'},
                           content_type='application/json')
        assert resp.status_code == 502

    @patch('app.services.msx_api.batch_query_account_teams')
    @patch('app.services.msx_api.get_account_details')
    @patch('app.services.msx_api.lookup_account_by_tpid')
    def test_lookup_picks_top_level_account(self, mock_lookup, mock_details,
                                            mock_teams, client):
        """Should prefer the top-level parent over child accounts."""
        mock_lookup.return_value = {
            'success': True,
            'accounts': [
                {
                    'accountid': 'child-1',
                    'name': 'Test Corp - East',
                    'msp_mstopparentid': '77777',
                    'parenting_level': 'Child',
                    'url': 'https://child',
                    'websiteurl': None,
                },
                {
                    'accountid': 'top-1',
                    'name': 'Test Corp',
                    'msp_mstopparentid': '77777',
                    'parenting_level': 'Top',
                    'url': 'https://top',
                    'websiteurl': 'https://testcorp.com',
                },
            ],
        }
        mock_details.return_value = {
            'success': True,
            'account': {'name': 'Test Corp', 'tpid': '77777'},
            'territory': None,
            'seller': None,
            'pod': None,
        }
        mock_teams.return_value = {
            'success': True,
            'account_sellers': {},
            'unique_sellers': {},
            'account_ses': {},
        }

        resp = client.post('/api/customer/tpid-lookup',
                           json={'tpid': '77777'},
                           content_type='application/json')
        data = resp.get_json()
        assert data['success'] is True
        assert data['account']['name'] == 'Test Corp'
        # get_account_details should have been called with the top-level ID
        mock_details.assert_called_once_with('top-1')


class TestTpidImport:
    """Tests for /api/customer/tpid-import endpoint."""

    def test_import_requires_account_data(self, client):
        """Should reject empty request."""
        resp = client.post('/api/customer/tpid-import',
                           json={},
                           content_type='application/json')
        assert resp.status_code == 400

    def test_import_requires_tpid_and_name(self, client):
        """Should reject account missing TPID or name."""
        resp = client.post('/api/customer/tpid-import',
                           json={'account': {'name': 'Test'}},
                           content_type='application/json')
        assert resp.status_code == 400

    @patch('app.routes.admin.fetch_favicon_for_domain')
    def test_import_creates_customer(self, mock_favicon, client, app):
        """Should create customer with territory, seller, and favicon."""
        mock_favicon.return_value = 'base64favicondata'

        account = {
            'name': 'Import Corp',
            'tpid': '88888',
            'url': 'https://msx/account/xyz',
            'website': 'importcorp.com',
            'territory_name': 'West.SMECC.NEW.0301',
            'seller_name': 'New Seller',
            'seller_type': 'Growth',
        }

        resp = client.post('/api/customer/tpid-import',
                           json={'account': account},
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['customer_name'] == 'Import Corp'
        assert data['territory_name'] == 'West.SMECC.NEW.0301'
        assert data['seller_name'] == 'New Seller'

        with app.app_context():
            from app.models import Customer, Territory, Seller, POD
            cust = Customer.query.filter_by(tpid=88888).first()
            assert cust is not None
            assert cust.name == 'Import Corp'
            assert cust.website == 'importcorp.com'
            assert cust.favicon_b64 == 'base64favicondata'
            assert cust.tpid_url == 'https://msx/account/xyz'
            assert cust.territory is not None
            assert cust.territory.name == 'West.SMECC.NEW.0301'
            assert cust.seller is not None
            assert cust.seller.name == 'New Seller'
            # Seller should be linked to territory
            assert cust.territory in cust.seller.territories

    @patch('app.routes.admin.fetch_favicon_for_domain')
    def test_import_reuses_existing_territory_and_seller(self, mock_favicon,
                                                          client, app,
                                                          sample_data):
        """Should reuse existing territory and seller, not create duplicates."""
        mock_favicon.return_value = None

        with app.app_context():
            from app.models import Territory, Seller
            territory = Territory.query.first()
            seller = Seller.query.first()
            t_name = territory.name
            s_name = seller.name
            t_count = Territory.query.count()
            s_count = Seller.query.count()

        account = {
            'name': 'Reuse Corp',
            'tpid': '77777',
            'url': 'https://msx/account/reuse',
            'website': '',
            'territory_name': t_name,
            'seller_name': s_name,
        }

        resp = client.post('/api/customer/tpid-import',
                           json={'account': account},
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

        with app.app_context():
            from app.models import Territory, Seller
            assert Territory.query.count() == t_count
            assert Seller.query.count() == s_count

    @patch('app.routes.admin.fetch_favicon_for_domain')
    def test_import_rejects_duplicate_tpid(self, mock_favicon, client, app,
                                            sample_data):
        """Should return 409 if TPID already exists."""
        with app.app_context():
            from app.models import Customer
            cust = Customer.query.get(sample_data['customer1_id'])
            tpid = cust.tpid

        account = {
            'name': 'Dupe Corp',
            'tpid': str(tpid),
            'url': 'https://msx/dupe',
        }

        resp = client.post('/api/customer/tpid-import',
                           json={'account': account},
                           content_type='application/json')
        assert resp.status_code == 409

    @patch('app.routes.admin.fetch_favicon_for_domain')
    def test_import_without_territory_or_seller(self, mock_favicon, client, app):
        """Should create customer even without territory/seller info."""
        mock_favicon.return_value = None

        account = {
            'name': 'Bare Corp',
            'tpid': '66666',
            'url': 'https://msx/bare',
            'website': '',
            'territory_name': '',
            'seller_name': '',
        }

        resp = client.post('/api/customer/tpid-import',
                           json={'account': account},
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['territory_name'] is None
        assert data['seller_name'] is None

        with app.app_context():
            from app.models import Customer
            cust = Customer.query.filter_by(tpid=66666).first()
            assert cust is not None
            assert cust.territory is None
            assert cust.seller is None


class TestTpidImportModal:
    """Integration test: modal renders on customers list page."""

    def test_import_modal_on_customers_page(self, client):
        """Customers list page should have the TPID import modal."""
        resp = client.get('/customers')
        assert resp.status_code == 200
        assert b'tpidImportModal' in resp.data
        assert b'Import by TPID' in resp.data

    def test_manual_button_still_exists(self, client):
        """Manual customer creation link should still be available."""
        resp = client.get('/customers')
        assert resp.status_code == 200
        assert b'Manual' in resp.data
