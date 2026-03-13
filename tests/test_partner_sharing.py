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
)


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
        """Existing partner matched by website gets updated."""
        with app.app_context():
            partner = Partner(name='DiffName LLC', website='sameco.com')
            db.session.add(partner)
            db.session.commit()

            data = {
                'name': 'SameCo',  # different name
                'website': 'SAMECO.COM',  # case-insensitive website match
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
