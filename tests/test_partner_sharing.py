"""
Tests for partner sharing — serialization, upsert, and API endpoints.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from app.models import db, Partner, PartnerContact, Specialty
from app.services.partner_sharing import (
    serialize_partner,
    serialize_all_partners,
    upsert_partner,
    upsert_partners,
    preview_partners,
    _normalize_company_name,
)


# ── Name normalization tests ─────────────────────────────────────────────────


class TestNormalizeCompanyName:
    """Test company name normalization for matching."""

    def test_strips_llc(self):
        assert _normalize_company_name('Contoso LLC') == 'contoso'

    def test_strips_inc_with_period(self):
        assert _normalize_company_name('Contoso, Inc.') == 'contoso'

    def test_strips_corp(self):
        assert _normalize_company_name('Contoso Corp') == 'contoso'

    def test_strips_corporation(self):
        assert _normalize_company_name('Contoso Corporation') == 'contoso'

    def test_strips_ltd(self):
        assert _normalize_company_name('Contoso Ltd.') == 'contoso'

    def test_strips_multiple_suffixes(self):
        assert _normalize_company_name('Contoso Holdings LLC') == 'contoso'

    def test_case_insensitive(self):
        assert _normalize_company_name('CONTOSO LLC') == 'contoso'

    def test_preserves_meaningful_words(self):
        assert _normalize_company_name('Blue Corp Industries') == 'blue corp industries'

    def test_plain_name_unchanged(self):
        assert _normalize_company_name('Profisee') == 'profisee'

    def test_strips_leading_trailing_whitespace(self):
        assert _normalize_company_name('  Contoso Inc  ') == 'contoso'

    def test_strips_emojis(self):
        assert _normalize_company_name('Nerdio ❤️') == 'nerdio'

    def test_strips_emojis_mixed(self):
        assert _normalize_company_name('🚀 Contoso 🎉 LLC') == 'contoso'

    def test_strips_heart_emoji_in_middle(self):
        assert _normalize_company_name('Nerdio❤️Corp') == 'nerdiocorp'


# ── Serialization tests ─────────────────────────────────────────────────────


class TestSerializePartner:
    """Test partner → JSON serialization."""

    def test_serializes_all_fields(self, app):
        """Full partner with contacts and specialties serializes correctly."""
        with app.app_context():
            specialty = Specialty(name='Azure SQL')
            db.session.add(specialty)
            db.session.flush()

            partner = Partner(
                name='Profisee',
                overview='MDM platform partner',
                rating=5,
                website='profisee.com',
                favicon_b64='iVBORw0KGgo=',
            )
            db.session.add(partner)
            db.session.flush()

            partner.specialties.append(specialty)

            contact = PartnerContact(
                partner_id=partner.id,
                name='Heidi Morrison',
                email='heidi@profisee.com',
                is_primary=True,
            )
            db.session.add(contact)
            db.session.flush()

            result = serialize_partner(partner)

            assert result['name'] == 'Profisee'
            assert result['overview'] == 'MDM platform partner'
            assert result['rating'] == 5
            assert result['website'] == 'profisee.com'
            assert result['favicon_b64'] == 'iVBORw0KGgo='
            assert result['specialties'] == ['Azure SQL']
            assert len(result['contacts']) == 1
            assert result['contacts'][0]['name'] == 'Heidi Morrison'
            assert result['contacts'][0]['email'] == 'heidi@profisee.com'
            assert result['contacts'][0]['is_primary'] is True

    def test_serializes_minimal_partner(self, app):
        """Partner with only a name serializes without errors."""
        with app.app_context():
            partner = Partner(name='MinimalCo')
            db.session.add(partner)
            db.session.flush()

            result = serialize_partner(partner)

            assert result['name'] == 'MinimalCo'
            assert result['rating'] is None
            assert result['specialties'] == []
            assert result['contacts'] == []

    def test_serialize_all_returns_list(self, app):
        """serialize_all_partners returns a list of all partners."""
        with app.app_context():
            db.session.add(Partner(name='Alpha'))
            db.session.add(Partner(name='Beta'))
            db.session.commit()

            results = serialize_all_partners()

            assert len(results) == 2
            names = {r['name'] for r in results}
            assert names == {'Alpha', 'Beta'}


# ── Upsert tests ────────────────────────────────────────────────────────────


class TestUpsertPartner:
    """Test partner upsert from received share data."""

    def test_creates_new_partner(self, app):
        """Partner not in DB gets created."""
        with app.app_context():
            data = {
                'name': 'NewCo',
                'overview': 'A new partner',
                'rating': 4,
                'website': 'newco.com',
                'specialties': ['Kubernetes'],
                'contacts': [
                    {'name': 'John Doe', 'email': 'john@newco.com', 'is_primary': True},
                ],
            }

            result = upsert_partner(data, 'Alice')
            db.session.commit()

            assert result['action'] == 'created'
            assert result['name'] == 'NewCo'

            partner = Partner.query.filter_by(name='NewCo').first()
            assert partner is not None
            assert partner.rating == 4
            assert partner.website == 'newco.com'
            assert partner.overview == 'A new partner'
            assert len(partner.contacts) == 1
            assert partner.contacts[0].email == 'john@newco.com'
            assert len(partner.specialties) == 1
            assert partner.specialties[0].name == 'Kubernetes'

    def test_updates_existing_by_name_match(self, app):
        """Existing partner matched by name gets updated."""
        with app.app_context():
            partner = Partner(name='ExistCo', overview='Original notes', rating=3)
            db.session.add(partner)
            db.session.flush()

            contact = PartnerContact(
                partner_id=partner.id, name='Existing', email='existing@existco.com',
            )
            db.session.add(contact)
            db.session.commit()

            data = {
                'name': 'existco',  # case-insensitive match
                'overview': 'Sender notes about ExistCo',
                'rating': 5,
                'contacts': [
                    {'name': 'Existing', 'email': 'existing@existco.com'},  # dupe
                    {'name': 'New Person', 'email': 'new@existco.com'},     # new
                ],
                'specialties': ['Data Migration'],
            }

            result = upsert_partner(data, 'Bob')
            db.session.commit()

            assert result['action'] == 'updated'

            refreshed = Partner.query.filter_by(name='ExistCo').first()
            # New contact added, dupe skipped
            assert len(refreshed.contacts) == 2
            emails = {c.email for c in refreshed.contacts}
            assert 'new@existco.com' in emails

            # Overview has sender's section appended
            assert "--- Bob's review ---" in refreshed.overview
            assert '★★★★★' in refreshed.overview

            # Specialty added
            assert any(s.name == 'Data Migration' for s in refreshed.specialties)

    def test_updates_existing_by_website_match(self, app):
        """Existing partner matched by website gets updated (normalizes URLs)."""
        with app.app_context():
            partner = Partner(name='DiffName LLC', website='sameco.com')
            db.session.add(partner)
            db.session.commit()

            data = {
                'name': 'SameCo',  # different name
                'website': 'https://www.SAMECO.COM/page',  # scheme + www + path stripped
                'specialties': ['AI/ML'],
            }

            result = upsert_partner(data, 'Carol')
            db.session.commit()

            assert result['action'] == 'updated'
            assert result['name'] == 'DiffName LLC'  # keeps existing name

    def test_skips_empty_name(self, app):
        """Partner with empty name is skipped."""
        with app.app_context():
            result = upsert_partner({'name': '  '}, 'Alice')
            assert result['action'] == 'skipped'

    def test_matches_name_with_suffix_stripped(self, app):
        """Partner matched by name after stripping LLC/Inc/etc."""
        with app.app_context():
            partner = Partner(name='Contoso')
            db.session.add(partner)
            db.session.commit()

            data = {
                'name': 'Contoso, LLC',
                'specialties': ['Azure'],
            }
            result = upsert_partner(data, 'Alice')
            db.session.commit()

            assert result['action'] == 'updated'
            assert result['name'] == 'Contoso'

    def test_matches_existing_suffix_to_bare_name(self, app):
        """Existing 'Contoso Inc.' matched by incoming 'Contoso'."""
        with app.app_context():
            partner = Partner(name='Contoso Inc.')
            db.session.add(partner)
            db.session.commit()

            data = {'name': 'Contoso', 'specialties': ['AI']}
            result = upsert_partner(data, 'Bob')
            db.session.commit()

            assert result['action'] == 'updated'
            assert result['name'] == 'Contoso Inc.'

    def test_no_duplicate_sender_review(self, app):
        """Running upsert twice from same sender doesn't duplicate review."""
        with app.app_context():
            partner = Partner(name='DupTest', overview='Original')
            db.session.add(partner)
            db.session.commit()

            data = {'name': 'DupTest', 'overview': 'Sender notes', 'rating': 4}
            upsert_partner(data, 'Alice')
            db.session.commit()

            upsert_partner(data, 'Alice')
            db.session.commit()

            refreshed = Partner.query.filter_by(name='DupTest').first()
            count = refreshed.overview.count("--- Alice's review ---")
            assert count == 1

    def test_website_upsert_updates_missing(self, app):
        """Partner without website gets it from share data."""
        with app.app_context():
            partner = Partner(name='NoWebsite Co')
            db.session.add(partner)
            db.session.commit()

            data = {'name': 'NoWebsite Co', 'website': 'nowebsite.com', 'favicon_b64': 'abc='}
            upsert_partner(data, 'Alice')
            db.session.commit()

            refreshed = Partner.query.filter_by(name='NoWebsite Co').first()
            assert refreshed.website == 'nowebsite.com'
            assert refreshed.favicon_b64 == 'abc='

    def test_existing_specialties_reused(self, app):
        """Specialties that already exist in DB are reused, not duplicated."""
        with app.app_context():
            existing_spec = Specialty(name='Azure SQL')
            db.session.add(existing_spec)
            db.session.commit()

            data = {
                'name': 'SpecTest Co',
                'specialties': ['Azure SQL', 'New Specialty'],
            }
            upsert_partner(data, 'Alice')
            db.session.commit()

            partner = Partner.query.filter_by(name='SpecTest Co').first()
            spec_names = {s.name for s in partner.specialties}
            assert spec_names == {'Azure SQL', 'New Specialty'}

            # Azure SQL should still be just one record
            count = Specialty.query.filter_by(name='Azure SQL').count()
            assert count == 1


class TestUpsertPartners:
    """Test bulk upsert of multiple partners."""

    def test_bulk_upsert_returns_counts(self, app):
        """upsert_partners returns correct created/updated counts."""
        with app.app_context():
            # Pre-create one partner
            db.session.add(Partner(name='Existing'))
            db.session.commit()

            partners_data = [
                {'name': 'Existing', 'overview': 'Updated'},
                {'name': 'Brand New', 'overview': 'Fresh partner'},
            ]

            results = upsert_partners(partners_data, 'Alice')

            assert results['created'] == 1
            assert results['updated'] == 1
            assert len(results['details']) == 2


# ── API endpoint tests ──────────────────────────────────────────────────────


class TestShareAPIEndpoints:
    """Test the partner sharing API endpoints."""

    def test_serialize_partner_endpoint(self, client, app):
        """GET /api/share/partner/<id> returns serialized partner."""
        with app.app_context():
            partner = Partner(name='APITest', rating=3)
            db.session.add(partner)
            db.session.commit()
            pid = partner.id

        resp = client.get(f'/api/share/partner/{pid}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['partner']['name'] == 'APITest'
        assert data['partner']['rating'] == 3

    def test_serialize_directory_endpoint(self, client, app):
        """GET /api/share/directory returns all partners."""
        with app.app_context():
            db.session.add(Partner(name='Dir1'))
            db.session.add(Partner(name='Dir2'))
            db.session.commit()

        resp = client.get('/api/share/directory')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert len(data['partners']) == 2

    def test_receive_endpoint_creates_partner(self, client, app):
        """POST /api/share/receive upserts partners."""
        resp = client.post('/api/share/receive', json={
            'sender_name': 'TestSender',
            'partners': [
                {'name': 'ReceivedCo', 'rating': 4, 'specialties': ['Testing']},
            ],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['created'] == 1

        with app.app_context():
            partner = Partner.query.filter_by(name='ReceivedCo').first()
            assert partner is not None
            assert partner.rating == 4

    def test_receive_endpoint_validation(self, client):
        """POST /api/share/receive rejects empty payload."""
        resp = client.post('/api/share/receive', json={'partners': [], 'sender_name': 'X'})
        assert resp.status_code == 400

    def test_connection_info_requires_auth(self, client):
        """GET /api/share/connection-info returns 401 when not authenticated."""
        with patch('app.services.partner_sharing.get_share_token', return_value=None):
            resp = client.get('/api/share/connection-info')
            assert resp.status_code == 401


# ── Preview tests ────────────────────────────────────────────────────────────


class TestPreviewPartners:
    """Test preview_partners dry-run logic."""

    def test_new_partner_shows_create(self, app):
        """Partner not in DB previews as 'create' with incoming values."""
        with app.app_context():
            data = [{
                'name': 'BrandNew',
                'contacts': [{'name': 'Jo', 'email': 'a@b.com', 'is_primary': True}],
                'specialties': ['AI'],
                'website': 'brandnew.com',
                'rating': 4,
                'overview': 'Great partner',
            }]
            previews = preview_partners(data, 'Alice')

            assert len(previews) == 1
            p = previews[0]
            assert p['action'] == 'create'
            assert p['name'] == 'BrandNew'
            assert p['has_changes'] is True
            # Structured incoming data
            inc = p['incoming']
            assert inc['website'] == 'brandnew.com'
            assert inc['rating'] == 4
            assert inc['overview'] == 'Great partner'
            assert inc['specialties'] == ['AI']
            assert len(inc['contacts']) == 1
            assert inc['contacts'][0]['email'] == 'a@b.com'

    def test_existing_partner_with_changes(self, app):
        """Existing partner with new contacts/specialties shows 'update' with structured changes."""
        with app.app_context():
            partner = Partner(name='ExistCo', overview='Original')
            db.session.add(partner)
            db.session.flush()
            contact = PartnerContact(partner_id=partner.id, name='Old', email='old@existco.com')
            db.session.add(contact)
            db.session.commit()

            data = [{
                'name': 'ExistCo',
                'overview': 'New review',
                'rating': 5,
                'website': 'existco.com',
                'contacts': [
                    {'email': 'old@existco.com'},  # existing — skipped
                    {'name': 'New Person', 'email': 'new@existco.com'},  # new
                ],
                'specialties': ['Kubernetes'],
            }]
            previews = preview_partners(data, 'Alice')

            assert len(previews) == 1
            p = previews[0]
            assert p['action'] == 'update'
            assert p['has_changes'] is True
            ch = p['changes']
            assert len(ch['contacts']) == 1
            assert ch['contacts'][0]['email'] == 'new@existco.com'
            assert ch['specialties'] == ['Kubernetes']
            assert ch['overview'] == 'New review'
            assert ch['website'] == 'existco.com'
            assert ch['rating'] == 5

    def test_existing_partner_no_changes(self, app):
        """Existing partner with no new data shows 'update' with no changes."""
        with app.app_context():
            spec = Specialty(name='Azure SQL')
            db.session.add(spec)
            db.session.flush()
            partner = Partner(name='SameCo', overview="--- Bob's review ---\nStuff")
            partner.specialties.append(spec)
            db.session.add(partner)
            db.session.flush()
            contact = PartnerContact(partner_id=partner.id, name='X', email='x@sameco.com')
            db.session.add(contact)
            db.session.commit()

            data = [{
                'name': 'SameCo',
                'overview': 'Something',
                'contacts': [{'email': 'x@sameco.com'}],
                'specialties': ['Azure SQL'],
            }]
            previews = preview_partners(data, 'Bob')

            assert len(previews) == 1
            assert previews[0]['action'] == 'update'
            assert previews[0]['has_changes'] is False
            assert previews[0]['changes'] == {}

    def test_skips_empty_name(self, app):
        """Partners with empty names are skipped in preview."""
        with app.app_context():
            data = [{'name': '  '}, {'name': 'Valid'}]
            previews = preview_partners(data, 'Alice')
            assert len(previews) == 1
            assert previews[0]['name'] == 'Valid'

    def test_website_match(self, app):
        """Preview matches existing partner by website, normalizing URL schemes."""
        with app.app_context():
            partner = Partner(name='OldName', website='sameco.com')
            db.session.add(partner)
            db.session.commit()

            # https:// prefix and www. should still match
            data = [{'name': 'DifferentName', 'website': 'https://www.SAMECO.COM/about'}]
            previews = preview_partners(data, 'Alice')

            assert len(previews) == 1
            assert previews[0]['action'] == 'update'
            assert previews[0]['name'] == 'OldName'

    def test_name_suffix_match(self, app):
        """Preview matches partner after stripping company suffixes."""
        with app.app_context():
            partner = Partner(name='Profisee')
            db.session.add(partner)
            db.session.commit()

            data = [{'name': 'Profisee, Inc.', 'specialties': ['MDM']}]
            previews = preview_partners(data, 'Alice')

            assert len(previews) == 1
            assert previews[0]['action'] == 'update'
            assert previews[0]['name'] == 'Profisee'


class TestPreviewAPIEndpoint:
    """Test the /api/share/preview endpoint."""

    def test_preview_endpoint_returns_previews(self, client, app):
        """POST /api/share/preview returns preview list."""
        with app.app_context():
            db.session.add(Partner(name='ExistingPartner'))
            db.session.commit()

        resp = client.post('/api/share/preview', json={
            'sender_name': 'Bob',
            'partners': [
                {'name': 'ExistingPartner', 'specialties': ['New Spec']},
                {'name': 'NewPartner', 'contacts': [], 'specialties': []},
            ],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert len(data['previews']) == 2

        existing = next(p for p in data['previews'] if p['name'] == 'ExistingPartner')
        assert existing['action'] == 'update'

        new = next(p for p in data['previews'] if p['name'] == 'NewPartner')
        assert new['action'] == 'create'

    def test_preview_endpoint_rejects_empty(self, client):
        """POST /api/share/preview rejects empty partner list."""
        resp = client.post('/api/share/preview', json={'partners': [], 'sender_name': 'X'})
        assert resp.status_code == 400

    def test_preview_endpoint_rejects_no_body(self, client):
        """POST /api/share/preview rejects missing JSON body."""
        resp = client.post('/api/share/preview', content_type='application/json')
        assert resp.status_code == 400
