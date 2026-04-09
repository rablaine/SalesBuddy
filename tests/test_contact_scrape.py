"""Tests for contact scraping services and meeting attendee import."""
import json
import pytest
from unittest.mock import patch, MagicMock

from app.models import (
    db, Partner, PartnerContact, Customer, CustomerContact,
    Seller, SolutionEngineer, Specialty, Note,
)


class TestPartnerScrapeService:
    """Tests for partner_scrape.py service functions."""

    def test_get_domain_hint_from_website(self, app, sample_data):
        """Domain hint derived from partner website."""
        with app.app_context():
            p = Partner(name='TestPartner', website='acme.com')
            db.session.add(p)
            db.session.flush()
            from app.services.partner_scrape import _get_domain_hint
            assert _get_domain_hint(p) == 'acme.com'
            db.session.delete(p)
            db.session.commit()

    def test_get_domain_hint_from_contact_email(self, app, sample_data):
        """Domain hint derived from contact email when no website."""
        with app.app_context():
            p = Partner(name='TestPartner')
            db.session.add(p)
            db.session.flush()
            c = PartnerContact(partner_id=p.id, name='Joe', email='joe@example.org')
            db.session.add(c)
            db.session.flush()
            from app.services.partner_scrape import _get_domain_hint
            assert _get_domain_hint(p) == 'example.org'
            db.session.delete(c)
            db.session.delete(p)
            db.session.commit()

    def test_get_domain_hint_none(self, app, sample_data):
        """No domain hint when no website or contacts."""
        with app.app_context():
            p = Partner(name='TestPartner')
            db.session.add(p)
            db.session.flush()
            from app.services.partner_scrape import _get_domain_hint
            assert _get_domain_hint(p) is None
            db.session.delete(p)
            db.session.commit()

    def test_parse_response_valid_json(self, app):
        """Parse valid JSON response from WorkIQ."""
        from app.services.partner_scrape import _parse_response
        raw = '```json\n{"contacts": [{"name": "Jane", "email": "jane@acme.com", "title": "CTO"}], "specialties": ["Fabric"], "overview": "Great partner", "meetings_found": 5}\n```'
        result = _parse_response(raw)
        assert len(result['contacts']) == 1
        assert result['contacts'][0]['name'] == 'Jane'
        assert result['specialties'] == ['Fabric']
        assert result['overview'] == 'Great partner'
        assert result['meetings_found'] == 5

    def test_parse_response_invalid(self, app):
        """Parse garbage response returns empty."""
        from app.services.partner_scrape import _parse_response
        result = _parse_response('I could not find any data for this partner.')
        assert result['contacts'] == []
        assert result['meetings_found'] == 0

    def test_match_contacts_new(self, app, sample_data):
        """New contacts are marked as is_new."""
        from app.services.partner_scrape import _match_contacts
        scraped = [{'name': 'New Person', 'email': 'new@acme.com', 'title': 'Dev'}]
        result = _match_contacts(scraped, [])
        assert len(result) == 1
        assert result[0]['is_new'] is True
        assert result[0]['existing_id'] is None

    def test_match_contacts_existing_by_email(self, app, sample_data):
        """Existing contact matched by email."""
        with app.app_context():
            p = Partner(name='TestP')
            db.session.add(p)
            db.session.flush()
            c = PartnerContact(partner_id=p.id, name='Jane', email='jane@acme.com')
            db.session.add(c)
            db.session.flush()

            from app.services.partner_scrape import _match_contacts
            scraped = [{'name': 'Jane D', 'email': 'jane@acme.com', 'title': None}]
            result = _match_contacts(scraped, [c])
            assert result[0]['is_new'] is False
            assert result[0]['existing_id'] == c.id

            db.session.delete(c)
            db.session.delete(p)
            db.session.commit()

    def test_match_specialties(self, app, sample_data):
        """New specialties marked as is_new, existing as not."""
        with app.app_context():
            s = Specialty(name='Fabric')
            db.session.add(s)
            db.session.flush()

            from app.services.partner_scrape import _match_specialties
            result = _match_specialties(['Fabric', 'SQL Migration'], [s])
            assert result[0]['name'] == 'Fabric'
            assert result[0]['is_new'] is False
            assert result[1]['name'] == 'SQL Migration'
            assert result[1]['is_new'] is True

            db.session.delete(s)
            db.session.commit()

    def test_apply_scrape_results(self, app, sample_data):
        """Apply creates contacts and specialties."""
        with app.app_context():
            p = Partner(name='TestPartner')
            db.session.add(p)
            db.session.commit()

            from app.services.partner_scrape import apply_scrape_results
            summary = apply_scrape_results(
                p,
                contacts=[{'name': 'Bob', 'email': 'bob@acme.com', 'title': 'VP'}],
                specialties=['NewSpec'],
                overview='Updated overview',
            )
            assert summary['contacts_created'] == 1
            assert summary['specialties_created'] == 1
            assert summary['overview_updated'] is True
            assert p.overview == 'Updated overview'
            assert len(p.contacts) == 1
            assert p.contacts[0].name == 'Bob'

            # Cleanup
            db.session.delete(p)
            db.session.commit()


class TestCustomerScrapeService:
    """Tests for customer_scrape.py service functions."""

    def test_get_domain_hint(self, app, sample_data):
        """Domain from customer website."""
        with app.app_context():
            c = db.session.get(Customer, sample_data['customer1_id'])
            c.website = 'contoso.com'
            db.session.commit()
            from app.services.customer_scrape import _get_domain_hint
            assert _get_domain_hint(c) == 'contoso.com'
            c.website = None
            db.session.commit()

    def test_parse_response(self, app):
        """Parse valid customer scrape response."""
        from app.services.customer_scrape import _parse_response
        raw = '{"contacts": [{"name": "Alice", "email": "alice@corp.com", "title": "CTO"}], "meetings_found": 3}'
        result = _parse_response(raw)
        assert len(result['contacts']) == 1
        assert result['meetings_found'] == 3

    def test_apply_customer_contacts(self, app, sample_data):
        """Apply creates new customer contacts."""
        with app.app_context():
            from app.services.customer_scrape import apply_customer_contacts
            c = db.session.get(Customer, sample_data['customer1_id'])
            summary = apply_customer_contacts(c, contacts=[
                {'name': 'NewGuy', 'email': 'ng@test.com', 'title': 'Dev'}
            ])
            assert summary['contacts_created'] == 1

            # Cleanup
            contact = CustomerContact.query.filter_by(email='ng@test.com').first()
            if contact:
                db.session.delete(contact)
                db.session.commit()


class TestMeetingAttendeeScrapeService:
    """Tests for meeting_attendee_scrape.py service functions."""

    def test_build_attendee_prompt_sanitizes_title(self, app):
        """Pipe characters removed from meeting title."""
        from app.services.meeting_attendee_scrape import _build_attendee_prompt
        prompt = _build_attendee_prompt('Meeting | With Pipes', '2026-03-30')
        assert '|' not in prompt
        assert 'Meeting - With Pipes' in prompt

    def test_parse_response_valid(self, app):
        """Parse valid attendee JSON."""
        from app.services.meeting_attendee_scrape import _parse_response
        raw = '```json\n{"attendees": [{"name": "Alice", "email": "alice@corp.com", "title": "VP"}]}\n```'
        result = _parse_response(raw)
        assert len(result) == 1
        assert result[0]['name'] == 'Alice'
        assert result[0]['title'] == 'VP'

    def test_parse_response_empty(self, app):
        """Empty/garbage response returns empty list."""
        from app.services.meeting_attendee_scrape import _parse_response
        result = _parse_response('No meetings found.')
        assert result == []

    def test_categorize_microsoft_seller(self, app, sample_data):
        """Microsoft employee matched to seller by alias."""
        with app.app_context():
            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'Alice Smith', 'email': 'alices@microsoft.com', 'title': None}]
            result = _categorize_attendees(attendees)
            assert result[0]['category'] == 'microsoft'
            assert result[0]['ref_type'] == 'seller'
            assert result[0]['ref_id'] == sample_data['seller1_id']

    def test_categorize_microsoft_by_name(self, app, sample_data):
        """Microsoft employee matched to seller by name fallback."""
        with app.app_context():
            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'Alice Smith', 'email': 'alice.smith@microsoft.com', 'title': None}]
            result = _categorize_attendees(attendees)
            assert result[0]['category'] == 'microsoft'
            assert result[0]['ref_type'] == 'seller'

    def test_categorize_microsoft_unknown(self, app, sample_data):
        """Unknown Microsoft employee categorized as external, checked by default."""
        with app.app_context():
            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'Random Person', 'email': 'random@microsoft.com', 'title': None}]
            result = _categorize_attendees(attendees)
            assert result[0]['category'] == 'microsoft'
            assert result[0]['ref_type'] == 'external'
            assert result[0]['checked'] is True

    def test_categorize_customer_contact_existing(self, app, sample_data):
        """Existing customer contact matched by email."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            contact = CustomerContact(customer_id=customer.id, name='Joe', email='joe@test.com')
            db.session.add(contact)
            db.session.commit()

            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'Joe', 'email': 'joe@test.com', 'title': None}]
            result = _categorize_attendees(attendees, customer=customer)
            assert result[0]['category'] == 'customer_contact'
            assert result[0]['is_new_contact'] is False
            assert result[0]['ref_id'] == contact.id

            db.session.delete(contact)
            db.session.commit()

    def test_categorize_customer_contact_new_by_domain(self, app, sample_data):
        """New contact matched to customer by domain."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            customer.website = 'acmecorp.com'
            db.session.commit()

            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'NewGuy', 'email': 'newguy@acmecorp.com', 'title': None}]
            result = _categorize_attendees(attendees, customer=customer)
            assert result[0]['category'] == 'customer_contact'
            assert result[0]['is_new_contact'] is True

            customer.website = None
            db.session.commit()

    def test_categorize_new_partner_domain(self, app, sample_data):
        """Unknown external domain categorized as new_partner."""
        with app.app_context():
            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'Vendor', 'email': 'vendor@unknowncorp.com', 'title': None}]
            result = _categorize_attendees(attendees)
            assert result[0]['category'] == 'new_partner'
            assert result[0]['new_partner_domain'] == 'unknowncorp.com'

    def test_fuzzy_domain_match(self, app, sample_data):
        """Fuzzy domain matching groups related domains to existing partner."""
        with app.app_context():
            p = Partner(name='Simform', website='simform.com')
            db.session.add(p)
            db.session.commit()

            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'Matt', 'email': 'matt@simformsolutions.com', 'title': None}]
            result = _categorize_attendees(attendees, partners=[p])
            assert result[0]['category'] == 'partner_contact'
            assert result[0]['partner_id'] == p.id

            db.session.delete(p)
            db.session.commit()

    def test_fuzzy_customer_domain_match(self, app, sample_data):
        """Fuzzy domain matching categorizes related domain as customer contact."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            customer.website = 'redsail.com'
            db.session.commit()

            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [
                {'name': 'Bob', 'email': 'bob@redsailconsultants.com', 'title': None}
            ]
            result = _categorize_attendees(attendees, customer=customer)
            assert result[0]['category'] == 'customer_contact'
            assert result[0]['is_new_contact'] is True

            customer.website = None
            db.session.commit()

    def test_fuzzy_customer_domain_from_contact_email(self, app, sample_data):
        """Fuzzy match works from existing contact emails, not just website."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            contact = CustomerContact(
                customer_id=customer.id, name='Existing', email='existing@acme.com'
            )
            db.session.add(contact)
            db.session.commit()

            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [
                {'name': 'New', 'email': 'new@acmesolutions.com', 'title': None}
            ]
            result = _categorize_attendees(attendees, customer=customer)
            assert result[0]['category'] == 'customer_contact'
            assert result[0]['is_new_contact'] is True

            db.session.delete(contact)
            db.session.commit()

    def test_fuzzy_customer_beats_fuzzy_partner(self, app, sample_data):
        """Customer fuzzy match takes priority over partner fuzzy match."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            customer.website = 'contoso.com'
            db.session.commit()

            p = Partner(name='Contoso Labs', website='contosolabs.net')
            db.session.add(p)
            db.session.commit()

            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [
                {'name': 'Jane', 'email': 'jane@contosoglobal.com', 'title': None}
            ]
            result = _categorize_attendees(attendees, customer=customer, partners=[p])
            # Customer fuzzy match should win over partner fuzzy match
            assert result[0]['category'] == 'customer_contact'

            customer.website = None
            db.session.commit()
            db.session.delete(p)
            db.session.commit()

    def test_fuzzy_customer_domain_short_base_skipped(self, app, sample_data):
        """Bases shorter than 3 chars are not fuzzy matched."""
        with app.app_context():
            from app.services.meeting_attendee_scrape import _fuzzy_match_customer_domain
            # 'ab' is too short to fuzzy match
            assert _fuzzy_match_customer_domain('ab.com', {'abcorp.com'}) is False
            # 'abc' is long enough
            assert _fuzzy_match_customer_domain('abc.com', {'abcorp.com'}) is True

    def test_fuzzy_customer_domain_unit(self, app):
        """Unit test for _fuzzy_match_customer_domain function."""
        from app.services.meeting_attendee_scrape import _fuzzy_match_customer_domain
        # Forward: known base in new domain
        assert _fuzzy_match_customer_domain(
            'redsailconsultants.com', {'redsail.com'}
        ) is True
        # Reverse: new base in known domain
        assert _fuzzy_match_customer_domain(
            'red.com', {'redsail.com'}
        ) is True
        # No overlap
        assert _fuzzy_match_customer_domain(
            'bluewave.com', {'redsail.com'}
        ) is False
        # Exact still works (but function only called for non-exact)
        assert _fuzzy_match_customer_domain(
            'redsail.com', {'redsail.com'}
        ) is True

    def test_merge_related_domains(self, app):
        """Related new_partner domains are merged."""
        from app.services.meeting_attendee_scrape import _merge_related_domains
        domain_groups = {'acme.com': [0], 'acmesolutions.com': [1]}
        attendees = [
            {'category': 'new_partner', 'new_partner_domain': 'acme.com'},
            {'category': 'new_partner', 'new_partner_domain': 'acmesolutions.com'},
        ]
        _merge_related_domains(domain_groups, attendees)
        # Both should point to the shorter domain
        assert attendees[0]['new_partner_domain'] == 'acme.com'
        assert attendees[1]['new_partner_domain'] == 'acme.com'

    def test_title_update_detection(self, app, sample_data):
        """Existing contact with different title shows update."""
        with app.app_context():
            customer = db.session.get(Customer, sample_data['customer1_id'])
            contact = CustomerContact(
                customer_id=customer.id, name='Joe', email='joe@test.com', title='Old Title'
            )
            db.session.add(contact)
            db.session.commit()

            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [{'name': 'Joe', 'email': 'joe@test.com', 'title': 'New Title'}]
            result = _categorize_attendees(attendees, customer=customer)
            assert result[0]['has_updates'] is True
            assert result[0]['existing_title'] == 'Old Title'
            assert result[0]['title'] == 'New Title'

            db.session.delete(contact)
            db.session.commit()

    def test_deduplicates_by_email(self, app, sample_data):
        """Duplicate emails are deduplicated."""
        with app.app_context():
            from app.services.meeting_attendee_scrape import _categorize_attendees
            attendees = [
                {'name': 'Alice', 'email': 'alice@test.com', 'title': None},
                {'name': 'Alice Duplicate', 'email': 'alice@test.com', 'title': None},
            ]
            result = _categorize_attendees(attendees)
            assert len(result) == 1


class TestMeetingAttendeeAPI:
    """Tests for meeting attendee API endpoints."""

    def test_scrape_endpoint_requires_fields(self, client):
        """Scrape endpoint returns 400 without required fields."""
        resp = client.post('/api/meeting-attendees/scrape', json={})
        assert resp.status_code == 400

    def test_apply_endpoint_requires_data(self, client):
        """Apply endpoint returns 400 without data."""
        resp = client.post('/api/meeting-attendees/apply',
                          content_type='application/json', data='')
        assert resp.status_code in (400, 415)

    @patch('app.services.workiq_service.query_workiq')
    def test_scrape_endpoint_success(self, mock_workiq, client, app, sample_data):
        """Scrape endpoint returns categorized attendees."""
        mock_workiq.return_value = json.dumps({
            'attendees': [
                {'name': 'Alice Smith', 'email': 'alices@microsoft.com', 'title': None},
                {'name': 'External', 'email': 'ext@unknown.com', 'title': 'Dev'},
            ]
        })
        resp = client.post('/api/meeting-attendees/scrape', json={
            'meeting_title': 'Test Meeting',
            'meeting_date': '2026-03-30',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert len(data['attendees']) == 2

    def test_apply_creates_contacts_and_partners(self, client, app, sample_data):
        """Apply endpoint creates new partners and contacts."""
        resp = client.post('/api/meeting-attendees/apply', json={
            'customer_id': sample_data['customer1_id'],
            'attendees': [
                {
                    'category': 'customer_contact',
                    'is_new_contact': True,
                    'name': 'New Contact',
                    'email': 'nc@test.com',
                    'title': 'Engineer',
                    'checked': True,
                },
                {
                    'category': 'new_partner',
                    'new_partner_domain': 'newvendor.com',
                    'new_partner_name': 'New Vendor',
                    'name': 'Vendor Guy',
                    'email': 'vg@newvendor.com',
                    'title': None,
                    'checked': True,
                },
            ]
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['contacts_created'] == 2
        assert len(data['new_partners']) == 1
        assert data['new_partners'][0]['name'] == 'New Vendor'
        assert len(data['attendee_results']) == 2

        # Cleanup
        with app.app_context():
            cc = CustomerContact.query.filter_by(email='nc@test.com').first()
            if cc:
                db.session.delete(cc)
            p = Partner.query.filter_by(name='New Vendor').first()
            if p:
                for c in p.contacts:
                    db.session.delete(c)
                db.session.delete(p)
            db.session.commit()


class TestPartnerScrapeAPI:
    """Tests for partner scrape API endpoints."""

    @patch('app.services.workiq_service.query_workiq')
    def test_scrape_endpoint(self, mock_workiq, client, app, sample_data):
        """Partner scrape returns structured data."""
        with app.app_context():
            p = Partner(name='TestP', website='testp.com')
            db.session.add(p)
            db.session.commit()
            pid = p.id

        mock_workiq.return_value = json.dumps({
            'contacts': [{'name': 'Jane', 'email': 'jane@testp.com', 'title': 'CTO'}],
            'specialties': ['Fabric'],
            'overview': 'Good partner',
            'meetings_found': 3,
        })
        resp = client.post(f'/api/partners/{pid}/scrape')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert len(data['contacts']) == 1
        assert len(data['specialties']) == 1

        # Cleanup
        with app.app_context():
            p = db.session.get(Partner, pid)
            if p:
                db.session.delete(p)
                db.session.commit()

    def test_apply_endpoint(self, client, app, sample_data):
        """Partner scrape apply creates contacts."""
        with app.app_context():
            p = Partner(name='TestApply', website='testapply.com')
            db.session.add(p)
            db.session.commit()
            pid = p.id

        resp = client.post(f'/api/partners/{pid}/scrape/apply', json={
            'contacts': [{'name': 'Bob', 'email': 'bob@testapply.com', 'title': 'VP'}],
            'specialties': ['Azure Migration'],
            'overview': 'Updated notes',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['contacts_created'] == 1

        # Cleanup
        with app.app_context():
            p = db.session.get(Partner, pid)
            if p:
                for c in p.contacts:
                    db.session.delete(c)
                db.session.delete(p)
                db.session.commit()


class TestCustomerScrapeAPI:
    """Tests for customer contact scrape API endpoints."""

    def test_scrape_no_domain(self, client, app, sample_data):
        """Scrape returns 400 when customer has no domain."""
        resp = client.post(f'/api/customer/{sample_data["customer1_id"]}/scrape-contacts')
        assert resp.status_code == 400

    def test_apply_endpoint(self, client, app, sample_data):
        """Customer scrape apply creates contacts."""
        resp = client.post(
            f'/api/customer/{sample_data["customer1_id"]}/scrape-contacts/apply',
            json={'contacts': [{'name': 'TestC', 'email': 'tc@test.com', 'title': 'Dev'}]}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['contacts_created'] == 1

        # Cleanup
        with app.app_context():
            c = CustomerContact.query.filter_by(email='tc@test.com').first()
            if c:
                db.session.delete(c)
                db.session.commit()
