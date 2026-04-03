"""
Tests for customer favicon feature.

Covers:
- Domain extraction from URLs
- Favicon fetch API endpoints
- Customer model website/favicon_b64 fields
- Jinja macro rendering
- MSX import website population
"""
import base64
from unittest.mock import patch, MagicMock

import pytest


# ===========================================================================
# Domain extraction tests
# ===========================================================================

class TestExtractDomain:
    """Tests for _extract_domain helper in msx routes."""

    def _extract(self, url_or_domain: str) -> str:
        from app.routes.msx import _extract_domain
        return _extract_domain(url_or_domain)

    def test_full_https_url(self):
        assert self._extract("https://www.example.com") == "example.com"

    def test_full_http_url(self):
        assert self._extract("http://www.example.com") == "example.com"

    def test_url_with_path(self):
        assert self._extract("https://example.com/foo/bar") == "example.com"

    def test_bare_domain(self):
        assert self._extract("example.com") == "example.com"

    def test_bare_domain_with_www(self):
        assert self._extract("www.example.com") == "example.com"

    def test_subdomain_preserved(self):
        assert self._extract("https://app.example.com") == "app.example.com"

    def test_empty_string(self):
        assert self._extract("") == ""

    def test_none(self):
        assert self._extract(None) == ""

    def test_no_dot_returns_empty(self):
        assert self._extract("localhost") == ""

    def test_uppercase_normalized(self):
        assert self._extract("EXAMPLE.COM") == "example.com"

    def test_whitespace_stripped(self):
        assert self._extract("  example.com  ") == "example.com"

    def test_azara_healthcare_real_value(self):
        """Real MSX value from Azara Healthcare."""
        assert self._extract("azarahealthcare.com") == "azarahealthcare.com"

    def test_i2i_systems_real_value(self):
        """Real MSX value from I2I Systems child account."""
        assert self._extract("http://www.i2isys.com") == "i2isys.com"


# ===========================================================================
# Model tests
# ===========================================================================

class TestCustomerFaviconModel:
    """Tests for website and favicon_b64 columns on Customer model."""

    def test_customer_has_website_field(self, app):
        with app.app_context():
            from app.models import Customer
            c = Customer(name="Test Co", tpid=99990001)
            assert c.website is None

    def test_customer_website_can_be_set(self, app):
        with app.app_context():
            from app.models import db, Customer
            c = Customer(name="Website Co", tpid=99990002, website="example.com")
            db.session.add(c)
            db.session.commit()
            fetched = Customer.query.filter_by(tpid=99990002).first()
            assert fetched.website == "example.com"
            # Cleanup
            db.session.delete(fetched)
            db.session.commit()

    def test_customer_favicon_b64_can_be_set(self, app):
        with app.app_context():
            from app.models import db, Customer
            fake_b64 = base64.b64encode(b"fake-png-data").decode("ascii")
            c = Customer(name="Favicon Co", tpid=99990003, website="favicon.co",
                         favicon_b64=fake_b64)
            db.session.add(c)
            db.session.commit()
            fetched = Customer.query.filter_by(tpid=99990003).first()
            assert fetched.favicon_b64 == fake_b64
            # Cleanup
            db.session.delete(fetched)
            db.session.commit()


# ===========================================================================
# Favicon fetch logic tests
# ===========================================================================

class TestFetchFaviconForDomain:
    """Tests for fetch_favicon_for_domain helper."""

    @patch('app.routes.admin.requests.get')
    def test_successful_fetch(self, mock_get):
        """Returns base64 when Google returns a real favicon."""
        from app.routes.admin import fetch_favicon_for_domain
        fake_png = b'\x89PNG' + b'\x00' * 500  # 504 bytes, not a globe
        mock_resp = MagicMock()
        mock_resp.content = fake_png
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_favicon_for_domain("example.com")
        assert result is not None
        assert result == base64.b64encode(fake_png).decode("ascii")
        mock_get.assert_called_once()

    @patch('app.routes.admin.requests.get')
    def test_generic_globe_returns_none(self, mock_get):
        """Returns None when Google returns the generic globe icon."""
        from app.routes.admin import fetch_favicon_for_domain, _GOOGLE_GLOBE_SIZES
        globe_size = next(iter(_GOOGLE_GLOBE_SIZES))
        fake_globe = b'\x00' * globe_size
        mock_resp = MagicMock()
        mock_resp.content = fake_globe
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_favicon_for_domain("nonexistent-domain-xyz.com")
        assert result is None

    @patch('app.routes.admin.requests.get')
    def test_network_error_returns_none(self, mock_get):
        """Returns None on network failure."""
        from app.routes.admin import fetch_favicon_for_domain
        mock_get.side_effect = Exception("Connection timeout")
        result = fetch_favicon_for_domain("unreachable.com")
        assert result is None

    def test_empty_domain_returns_none(self):
        from app.routes.admin import fetch_favicon_for_domain
        assert fetch_favicon_for_domain("") is None
        assert fetch_favicon_for_domain(None) is None


# ===========================================================================
# API endpoint tests
# ===========================================================================

class TestFetchFaviconsEndpoint:
    """Tests for /api/admin/fetch-favicons endpoint."""

    @patch('app.routes.admin.fetch_favicon_for_domain')
    def test_fetch_favicons_with_customers(self, mock_fetch, app, client):
        """Fetches favicons for customers with website but no favicon."""
        with app.app_context():
            from app.models import db, Customer
            c = Customer(name="FavTest Co", tpid=99990010, website="favtest.com")
            db.session.add(c)
            db.session.commit()
            cid = c.id

        mock_fetch.return_value = base64.b64encode(b"png-data").decode()

        resp = client.post('/api/admin/fetch-favicons')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['fetched'] >= 1

        # Verify favicon was stored
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, cid)
            assert c.favicon_b64 is not None
            # Cleanup
            db.session.delete(c)
            db.session.commit()

    def test_fetch_favicons_no_customers_needed(self, app, client):
        """Returns success with 0 fetched when no customers need favicons."""
        resp = client.post('/api/admin/fetch-favicons')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    @patch('app.routes.admin.fetch_favicon_for_domain')
    def test_fetch_skips_existing_favicons(self, mock_fetch, app, client):
        """Doesn't re-fetch favicons that already exist."""
        with app.app_context():
            from app.models import db, Customer
            c = Customer(name="Already Has", tpid=99990011, website="existing.com",
                         favicon_b64="existing-data")
            db.session.add(c)
            db.session.commit()

        resp = client.post('/api/admin/fetch-favicons')
        data = resp.get_json()
        # Should not call fetch since customer already has a favicon
        mock_fetch.assert_not_called()

        with app.app_context():
            from app.models import db, Customer
            c = Customer.query.filter_by(tpid=99990011).first()
            db.session.delete(c)
            db.session.commit()


class TestRefreshFaviconsEndpoint:
    """Tests for /api/admin/refresh-favicons endpoint."""

    @patch('app.routes.admin.fetch_favicon_for_domain')
    def test_refresh_re_fetches_existing(self, mock_fetch, app, client):
        """Refresh endpoint re-fetches even for customers with existing favicons."""
        with app.app_context():
            from app.models import db, Customer
            c = Customer(name="Refresh Me", tpid=99990020, website="refresh.com",
                         favicon_b64="old-data")
            db.session.add(c)
            db.session.commit()

        new_b64 = base64.b64encode(b"new-png").decode()
        mock_fetch.return_value = new_b64

        resp = client.post('/api/admin/refresh-favicons')
        data = resp.get_json()
        assert data['success'] is True
        assert data['fetched'] >= 1

        with app.app_context():
            from app.models import db, Customer
            c = Customer.query.filter_by(tpid=99990020).first()
            assert c.favicon_b64 == new_b64
            db.session.delete(c)
            db.session.commit()


# ===========================================================================
# Admin panel tests
# ===========================================================================

class TestAdminPanelFaviconCard:
    """Tests for favicon card in admin panel."""

    def test_admin_panel_shows_favicon_card(self, client):
        """Admin panel renders the Customer Favicons card."""
        resp = client.get('/admin')
        assert resp.status_code == 200
        assert b'Customer Favicons' in resp.data
        assert b'fetchFaviconsBtn' in resp.data
        assert b'refreshFaviconsBtn' in resp.data


class TestFaviconGallery:
    """Tests for /admin/favicons gallery page."""

    def test_gallery_renders_empty(self, client):
        """Gallery page loads with no customers with websites."""
        resp = client.get('/admin/favicons')
        assert resp.status_code == 200
        assert b'Favicon Gallery' in resp.data

    def test_gallery_shows_fetched_favicon(self, app, client, sample_data):
        """Gallery lists customers with fetched favicons."""
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.website = "example.com"
            c.favicon_b64 = base64.b64encode(b"gallery-test-png").decode()
            db.session.commit()

        resp = client.get('/admin/favicons')
        assert resp.status_code == 200
        assert b'Fetched Favicons' in resp.data
        assert b'data:image/png;base64,' in resp.data

    def test_gallery_shows_missing_favicon(self, app, client, sample_data):
        """Gallery lists customers with website but no favicon."""
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.website = "nofavicon.com"
            c.favicon_b64 = None
            db.session.commit()

        resp = client.get('/admin/favicons')
        assert resp.status_code == 200
        assert b'Missing Favicons' in resp.data
        assert b'bi-building' in resp.data

    def test_gallery_has_action_buttons(self, client):
        """Gallery page has fetch and refresh buttons."""
        resp = client.get('/admin/favicons')
        assert resp.status_code == 200
        assert b'fetchMissingBtn' in resp.data
        assert b'refreshAllBtn' in resp.data


# ===========================================================================
# Jinja macro rendering tests
# ===========================================================================

class TestCustomerFaviconMacro:
    """Tests that the favicon macro renders correctly in templates."""

    def test_customer_view_shows_favicon(self, app, client, sample_data):
        """Customer view page renders favicon img tag when favicon_b64 is set."""
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.favicon_b64 = base64.b64encode(b"test-png").decode()
            db.session.commit()

        resp = client.get(f'/customer/{sample_data["customer1_id"]}')
        assert resp.status_code == 200
        assert b'data:image/png;base64,' in resp.data
        assert b'customer-favicon' in resp.data

    def test_customer_view_shows_building_icon_without_favicon(self, app, client, sample_data):
        """Customer view page falls back to building icon when no favicon."""
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.favicon_b64 = None
            db.session.commit()

        resp = client.get(f'/customer/{sample_data["customer1_id"]}')
        assert resp.status_code == 200
        assert b'bi-building' in resp.data

    def test_customers_list_shows_favicons(self, app, client, sample_data):
        """Customers list page renders favicon for customers that have one."""
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.favicon_b64 = base64.b64encode(b"list-test-png").decode()
            db.session.commit()

        resp = client.get('/customers')
        assert resp.status_code == 200
        assert b'customer-favicon' in resp.data

    def test_notes_list_shows_favicons(self, app, client, sample_data):
        """Notes list page renders favicon next to customer name."""
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.favicon_b64 = base64.b64encode(b"calllog-test-png").decode()
            db.session.commit()

        resp = client.get('/notes')
        assert resp.status_code == 200
        assert b'customer-favicon' in resp.data

    def test_note_view_shows_favicon(self, app, client, sample_data):
        """Individual note view renders favicon next to customer."""
        with app.app_context():
            from app.models import db, Customer
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.favicon_b64 = base64.b64encode(b"view-test-png").decode()
            db.session.commit()

        resp = client.get(f'/note/{sample_data["call1_id"]}')
        assert resp.status_code == 200
        assert b'customer-favicon' in resp.data


# ===========================================================================
# Migration tests
# ===========================================================================

class TestFaviconMigration:
    """Tests for the favicon column migration."""

    def test_migration_adds_columns(self, app):
        """Migration adds website and favicon_b64 columns to customers."""
        with app.app_context():
            from sqlalchemy import inspect
            from app.models import db
            inspector = inspect(db.engine)
            columns = [c['name'] for c in inspector.get_columns('customers')]
            assert 'website' in columns
            assert 'favicon_b64' in columns

    def test_migration_is_idempotent(self, app):
        """Migration can run multiple times without error."""
        with app.app_context():
            from sqlalchemy import inspect
            from app.models import db
            from app.migrations import _migrate_customer_favicon_columns
            inspector = inspect(db.engine)
            # Run migration again - should not raise
            _migrate_customer_favicon_columns(db, inspector)
            columns = [c['name'] for c in inspector.get_columns('customers')]
            assert 'website' in columns
            assert 'favicon_b64' in columns


# ===========================================================================
# Globe icon detection tests
# ===========================================================================

class TestGlobeDetection:
    """Tests for _is_generic_globe function."""

    def test_known_globe_sizes_detected(self):
        from app.routes.admin import _is_generic_globe, _GOOGLE_GLOBE_SIZES
        for size in _GOOGLE_GLOBE_SIZES:
            assert _is_generic_globe(b'\x00' * size) is True

    def test_real_favicon_size_not_detected(self):
        from app.routes.admin import _is_generic_globe
        # Real favicons are typically 1-4KB
        assert _is_generic_globe(b'\x00' * 2048) is False

    def test_empty_bytes_not_globe(self):
        from app.routes.admin import _is_generic_globe
        assert _is_generic_globe(b'') is False
