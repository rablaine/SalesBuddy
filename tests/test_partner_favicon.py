"""
Tests for partner favicon feature.

Covers:
- Partner model website/favicon_b64 fields
- Favicon fetch on create/edit
- Partner favicon macro rendering
"""
import base64
from unittest.mock import patch

import pytest


# ===========================================================================
# Model tests
# ===========================================================================

class TestPartnerFaviconModel:
    """Tests for website and favicon_b64 columns on Partner model."""

    def test_partner_has_website_field(self, app):
        with app.app_context():
            from app.models import Partner
            p = Partner(name="Test Partner")
            p.website = "example.com"
            assert p.website == "example.com"

    def test_partner_has_favicon_b64_field(self, app):
        with app.app_context():
            from app.models import Partner, db
            p = Partner(name="Favicon Partner")
            p.favicon_b64 = "dGVzdA=="  # base64 for "test"
            db.session.add(p)
            db.session.commit()
            
            loaded = Partner.query.filter_by(name="Favicon Partner").first()
            assert loaded.favicon_b64 == "dGVzdA=="


# ===========================================================================
# Route tests - create
# ===========================================================================

class TestPartnerCreateWithFavicon:
    """Tests for favicon fetch on partner creation."""

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_create_partner_with_website_fetches_favicon(self, mock_fetch, app, client):
        """Creating a partner with website should fetch its favicon."""
        mock_fetch.return_value = "FAKE_BASE64_FAVICON"
        
        response = client.post('/partners/new', data={
            'name': 'Partner With Website',
            'website': 'example.com',
            'overview': '',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_called_once_with('example.com')
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.filter_by(name='Partner With Website').first()
            assert partner is not None
            assert partner.website == 'example.com'
            assert partner.favicon_b64 == 'FAKE_BASE64_FAVICON'

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_create_partner_without_website_no_favicon(self, mock_fetch, app, client):
        """Creating a partner without website should not fetch favicon."""
        response = client.post('/partners/new', data={
            'name': 'Partner Without Website',
            'website': '',
            'overview': '',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_not_called()
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.filter_by(name='Partner Without Website').first()
            assert partner is not None
            assert partner.website is None
            assert partner.favicon_b64 is None

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_create_partner_fetch_returns_none(self, mock_fetch, app, client):
        """If favicon fetch returns None, favicon_b64 should be None."""
        mock_fetch.return_value = None  # e.g., generic globe icon
        
        response = client.post('/partners/new', data={
            'name': 'Partner No Favicon',
            'website': 'no-icon-site.com',
            'overview': '',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_called_once_with('no-icon-site.com')
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.filter_by(name='Partner No Favicon').first()
            assert partner is not None
            assert partner.website == 'no-icon-site.com'
            assert partner.favicon_b64 is None

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_create_partner_normalizes_full_url(self, mock_fetch, app, client):
        """Full URL with https/www should be normalized to domain."""
        mock_fetch.return_value = "NORMALIZED_FAV"
        
        response = client.post('/partners/new', data={
            'name': 'URL Partner',
            'website': 'https://www.example.com/page',
            'overview': '',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_called_once_with('example.com')
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.filter_by(name='URL Partner').first()
            assert partner is not None
            assert partner.website == 'example.com'  # Normalized
            assert partner.favicon_b64 == 'NORMALIZED_FAV'


# ===========================================================================
# Route tests - edit
# ===========================================================================

class TestPartnerEditWithFavicon:
    """Tests for favicon fetch on partner edit."""

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_edit_partner_add_website_fetches_favicon(self, mock_fetch, app, client):
        """Adding website to existing partner should fetch favicon."""
        mock_fetch.return_value = "NEW_FAVICON"
        
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(name='Existing Partner')
            db.session.add(partner)
            db.session.commit()
            partner_id = partner.id
        
        response = client.post(f'/partners/{partner_id}/edit', data={
            'name': 'Existing Partner',
            'website': 'newsite.com',
            'overview': '',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_called_once_with('newsite.com')
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.get(partner_id)
            assert partner.website == 'newsite.com'
            assert partner.favicon_b64 == 'NEW_FAVICON'

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_edit_partner_change_website_refetches_favicon(self, mock_fetch, app, client):
        """Changing partner website should refetch favicon."""
        mock_fetch.return_value = "UPDATED_FAVICON"
        
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(name='Website Partner', website='old.com', favicon_b64='OLD_FAV')
            db.session.add(partner)
            db.session.commit()
            partner_id = partner.id
        
        response = client.post(f'/partners/{partner_id}/edit', data={
            'name': 'Website Partner',
            'website': 'new.com',
            'overview': '',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_called_once_with('new.com')
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.get(partner_id)
            assert partner.website == 'new.com'
            assert partner.favicon_b64 == 'UPDATED_FAVICON'

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_edit_partner_same_website_no_refetch(self, mock_fetch, app, client):
        """Keeping the same website should not refetch favicon."""
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(name='Keep Website', website='same.com', favicon_b64='ORIG')
            db.session.add(partner)
            db.session.commit()
            partner_id = partner.id
        
        response = client.post(f'/partners/{partner_id}/edit', data={
            'name': 'Keep Website',
            'website': 'same.com',
            'overview': 'Updated notes',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_not_called()
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.get(partner_id)
            assert partner.website == 'same.com'
            assert partner.favicon_b64 == 'ORIG'  # Unchanged

    @patch('app.routes.partners.fetch_favicon_for_domain')
    def test_edit_partner_clear_website_clears_favicon(self, mock_fetch, app, client):
        """Clearing website should clear favicon."""
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(name='Clear Website', website='had.com', favicon_b64='HAD_FAV')
            db.session.add(partner)
            db.session.commit()
            partner_id = partner.id
        
        response = client.post(f'/partners/{partner_id}/edit', data={
            'name': 'Clear Website',
            'website': '',
            'overview': '',
        }, follow_redirects=True)
        
        assert response.status_code == 200
        mock_fetch.assert_not_called()
        
        with app.app_context():
            from app.models import Partner
            partner = Partner.query.get(partner_id)
            assert partner.website is None
            assert partner.favicon_b64 is None


# ===========================================================================
# Template tests
# ===========================================================================

class TestPartnerFaviconMacro:
    """Tests for partner_favicon Jinja macro."""

    def test_macro_renders_favicon_when_present(self, app, client):
        """partner_favicon macro should render img tag when favicon exists."""
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(
                name='Favicon Test',
                website='test.com',
                favicon_b64='dGVzdA=='  # base64 for "test"
            )
            db.session.add(partner)
            db.session.commit()
            partner_id = partner.id

        response = client.get(f'/partners/{partner_id}')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        
        # Should have img with data URI
        assert 'data:image/png;base64,dGVzdA==' in html
        assert 'partner-favicon' in html or 'class="partner-favicon"' in html or 'class=' in html

    def test_macro_renders_building_icon_when_no_favicon(self, app, client):
        """partner_favicon macro should render building icon when no favicon."""
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(name='No Favicon Test')
            db.session.add(partner)
            db.session.commit()
            partner_id = partner.id

        response = client.get(f'/partners/{partner_id}')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        
        # Should have building icon somewhere in the page for the partner title
        assert 'bi-building' in html

    def test_partners_list_shows_favicon(self, app, client):
        """Partners list should show favicon for partners that have one."""
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(
                name='List Favicon Test',
                website='list.com',
                favicon_b64='bGlzdA=='
            )
            db.session.add(partner)
            db.session.commit()

        response = client.get('/partners')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        
        assert 'data:image/png;base64,bGlzdA==' in html


# ===========================================================================
# Form tests
# ===========================================================================

class TestPartnerFormWebsiteField:
    """Tests for website field in partner form."""

    def test_new_partner_form_has_website_field(self, app, client):
        """New partner form should have website input field."""
        response = client.get('/partners/new')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        
        assert 'name="website"' in html
        assert 'id="website"' in html

    def test_edit_partner_form_shows_existing_website(self, app, client):
        """Edit form should show existing website value."""
        with app.app_context():
            from app.models import Partner, db
            partner = Partner(name='Edit Form Test', website='existing.com')
            db.session.add(partner)
            db.session.commit()
            partner_id = partner.id

        response = client.get(f'/partners/{partner_id}/edit')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        
        assert 'value="existing.com"' in html
