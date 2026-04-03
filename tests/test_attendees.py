"""Tests for NoteAttendee model and attendee API endpoints."""
import pytest
from app.models import (
    db, Note, NoteAttendee, CustomerContact, PartnerContact,
    SolutionEngineer, Seller, Partner, InternalContact,
)


class TestNoteAttendeeModel:
    """Test NoteAttendee model properties."""

    def test_customer_contact_attendee(self, app, sample_data):
        """Customer contact attendee has correct type and display name."""
        with app.app_context():
            from app.models import Customer
            customer = db.session.get(Customer, sample_data['customer1_id'])
            contact = CustomerContact(
                customer_id=customer.id, name='Jane Doe',
                email='jane@example.com', title='CTO'
            )
            db.session.add(contact)
            db.session.flush()

            note = db.session.get(Note, sample_data['call1_id'])
            att = NoteAttendee(note_id=note.id, customer_contact_id=contact.id)
            db.session.add(att)
            db.session.commit()

            assert att.person_type == 'customer_contact'
            assert att.display_name == 'Jane Doe'
            assert att.email == 'jane@example.com'
            d = att.to_dict()
            assert d['person_type'] == 'customer_contact'
            assert d['ref_id'] == contact.id

            # Cleanup
            db.session.delete(att)
            db.session.delete(contact)
            db.session.commit()

    def test_seller_attendee(self, app, sample_data):
        """Seller attendee has correct type and display name."""
        with app.app_context():
            note = db.session.get(Note, sample_data['call1_id'])
            att = NoteAttendee(
                note_id=note.id, seller_id=sample_data['seller1_id']
            )
            db.session.add(att)
            db.session.commit()

            assert att.person_type == 'seller'
            assert att.display_name == 'Alice Smith'
            assert att.email == 'alices@microsoft.com'

            db.session.delete(att)
            db.session.commit()

    def test_se_attendee(self, app, sample_data):
        """Solution engineer attendee has correct type."""
        with app.app_context():
            se = SolutionEngineer(name='Bob SE', alias='bobse', specialty='Azure Data')
            db.session.add(se)
            db.session.flush()

            note = db.session.get(Note, sample_data['call1_id'])
            att = NoteAttendee(note_id=note.id, solution_engineer_id=se.id)
            db.session.add(att)
            db.session.commit()

            assert att.person_type == 'se'
            assert att.display_name == 'Bob SE'
            assert att.email == 'bobse@microsoft.com'

            db.session.delete(att)
            db.session.delete(se)
            db.session.commit()

    def test_external_attendee(self, app, sample_data):
        """External attendee uses external fields."""
        with app.app_context():
            note = db.session.get(Note, sample_data['call1_id'])
            att = NoteAttendee(
                note_id=note.id,
                external_name='Guest User',
                external_email='guest@corp.com'
            )
            db.session.add(att)
            db.session.commit()

            assert att.person_type == 'external'
            assert att.display_name == 'Guest User'
            assert att.email == 'guest@corp.com'

            db.session.delete(att)
            db.session.commit()

    def test_cascade_delete(self, app, sample_data):
        """Attendees are deleted when the note is deleted."""
        with app.app_context():
            note = Note(
                customer_id=sample_data['customer1_id'],
                call_date=db.func.now(),
                content='Test cascade delete'
            )
            db.session.add(note)
            db.session.flush()
            att = NoteAttendee(
                note_id=note.id, seller_id=sample_data['seller1_id']
            )
            db.session.add(att)
            db.session.commit()
            att_id = att.id

            db.session.delete(note)
            db.session.commit()

            assert db.session.get(NoteAttendee, att_id) is None


class TestAttendeeAPI:
    """Test attendee CRUD API endpoints."""

    def test_add_and_list_attendee(self, client, app, sample_data):
        """POST then GET attendees for a note."""
        note_id = sample_data['call1_id']

        # Add a seller attendee
        resp = client.post(
            f'/api/note/{note_id}/attendees',
            json={'type': 'seller', 'id': sample_data['seller1_id']}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['attendee']['person_type'] == 'seller'
        att_id = data['attendee']['id']

        # List attendees
        resp = client.get(f'/api/note/{note_id}/attendees')
        assert resp.status_code == 200
        attendees = resp.get_json()
        assert len(attendees) >= 1
        assert any(a['id'] == att_id for a in attendees)

        # Cleanup
        client.delete(f'/api/note/{note_id}/attendees/{att_id}')

    def test_add_external_attendee(self, client, app, sample_data):
        """Add an external attendee with name and email."""
        note_id = sample_data['call1_id']
        resp = client.post(
            f'/api/note/{note_id}/attendees',
            json={'type': 'external', 'name': 'Outside Person', 'email': 'out@corp.com'}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['attendee']['person_type'] == 'external'
        assert data['attendee']['display_name'] == 'Outside Person'

        # Cleanup
        client.delete(f'/api/note/{note_id}/attendees/{data["attendee"]["id"]}')

    def test_remove_attendee(self, client, app, sample_data):
        """DELETE removes an attendee."""
        note_id = sample_data['call1_id']
        resp = client.post(
            f'/api/note/{note_id}/attendees',
            json={'type': 'seller', 'id': sample_data['seller1_id']}
        )
        att_id = resp.get_json()['attendee']['id']

        resp = client.delete(f'/api/note/{note_id}/attendees/{att_id}')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        # Verify gone
        resp = client.get(f'/api/note/{note_id}/attendees')
        assert all(a['id'] != att_id for a in resp.get_json())

    def test_invalid_attendee_type(self, client, app, sample_data):
        """Invalid type returns 400."""
        note_id = sample_data['call1_id']
        resp = client.post(
            f'/api/note/{note_id}/attendees',
            json={'type': 'bogus', 'id': 1}
        )
        assert resp.status_code == 400


class TestAttendeeSearch:
    """Test the attendee search API."""

    def test_search_sellers(self, client, app, sample_data):
        """Search returns matching sellers."""
        resp = client.get('/api/attendee-search?q=alice')
        data = resp.get_json()
        results = data['results']
        assert any(r['type'] == 'seller' and 'Alice' in r['name'] for r in results)

    def test_search_customer_contacts(self, client, app, sample_data):
        """Search returns customer contacts when customer_id is provided."""
        with app.app_context():
            contact = CustomerContact(
                customer_id=sample_data['customer1_id'],
                name='Test Contact', email='tc@example.com'
            )
            db.session.add(contact)
            db.session.commit()
            cid = contact.id

        resp = client.get(
            f'/api/attendee-search?q=test&customer_id={sample_data["customer1_id"]}'
        )
        data = resp.get_json()
        assert any(r['type'] == 'customer_contact' and r['id'] == cid for r in data['results'])

        # Cleanup
        with app.app_context():
            c = db.session.get(CustomerContact, cid)
            if c:
                db.session.delete(c)
                db.session.commit()

    def test_search_empty_query(self, client, app, sample_data):
        """Empty query returns empty results."""
        resp = client.get('/api/attendee-search?q=')
        data = resp.get_json()
        assert data['results'] == []

    def test_search_solution_engineers(self, client, app, sample_data):
        """Search returns matching SEs."""
        with app.app_context():
            se = SolutionEngineer(name='SearchableSE', alias='searchse')
            db.session.add(se)
            db.session.commit()

        resp = client.get('/api/attendee-search?q=searchable')
        data = resp.get_json()
        assert any(r['type'] == 'se' and 'SearchableSE' in r['name'] for r in data['results'])

        with app.app_context():
            se = SolutionEngineer.query.filter_by(alias='searchse').first()
            if se:
                db.session.delete(se)
                db.session.commit()


class TestInternalContactModel:
    """Test InternalContact model."""

    def test_internal_contact_attendee(self, app, sample_data):
        """Internal contact attendee has correct type and display name."""
        with app.app_context():
            ic = InternalContact(name='Kevin DAE', alias='kevind', role='DAE')
            db.session.add(ic)
            db.session.flush()

            note = db.session.get(Note, sample_data['call1_id'])
            att = NoteAttendee(note_id=note.id, internal_contact_id=ic.id)
            db.session.add(att)
            db.session.commit()

            assert att.person_type == 'internal_contact'
            assert att.display_name == 'Kevin DAE'
            assert att.email == 'kevind@microsoft.com'
            assert att.ref_id == ic.id
            d = att.to_dict()
            assert d['person_type'] == 'internal_contact'
            assert d['ref_id'] == ic.id

            db.session.delete(att)
            db.session.delete(ic)
            db.session.commit()

    def test_get_email_with_alias(self, app):
        """get_email returns full email when alias is set."""
        with app.app_context():
            ic = InternalContact(name='Test IC', alias='testic', role='DSS')
            db.session.add(ic)
            db.session.commit()
            assert ic.get_email() == 'testic@microsoft.com'
            db.session.delete(ic)
            db.session.commit()

    def test_get_email_without_alias(self, app):
        """get_email returns None when alias is not set."""
        with app.app_context():
            ic = InternalContact(name='No Alias IC')
            db.session.add(ic)
            db.session.commit()
            assert ic.get_email() is None
            db.session.delete(ic)
            db.session.commit()


class TestInternalContactAPI:
    """Test internal contact CRUD API endpoints."""

    def test_create_internal_contact(self, client, app):
        """POST creates a new internal contact."""
        resp = client.post(
            '/api/internal-contacts',
            json={'name': 'New Person', 'alias': 'newp', 'role': 'DSE - Security'}
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['success'] is True
        assert data['contact']['name'] == 'New Person'
        assert data['contact']['alias'] == 'newp'
        assert data['contact']['role'] == 'DSE - Security'

        with app.app_context():
            ic = db.session.get(InternalContact, data['contact']['id'])
            db.session.delete(ic)
            db.session.commit()

    def test_create_with_full_email_strips_domain(self, client, app):
        """Alias strips @microsoft.com if user enters full email."""
        resp = client.post(
            '/api/internal-contacts',
            json={'name': 'Email Person', 'alias': 'emailp@microsoft.com'}
        )
        data = resp.get_json()
        assert data['contact']['alias'] == 'emailp'

        with app.app_context():
            ic = db.session.get(InternalContact, data['contact']['id'])
            db.session.delete(ic)
            db.session.commit()

    def test_create_deduplicates_by_alias(self, client, app):
        """Creating with existing alias returns the existing record."""
        with app.app_context():
            ic = InternalContact(name='Existing', alias='existalias', role='DAE')
            db.session.add(ic)
            db.session.commit()
            existing_id = ic.id

        resp = client.post(
            '/api/internal-contacts',
            json={'name': 'Existing', 'alias': 'existalias'}
        )
        assert resp.status_code == 200  # Not 201 - returned existing
        data = resp.get_json()
        assert data['contact']['id'] == existing_id

        with app.app_context():
            ic = db.session.get(InternalContact, existing_id)
            db.session.delete(ic)
            db.session.commit()

    def test_create_name_only(self, client, app):
        """Create with just a name (no alias)."""
        resp = client.post(
            '/api/internal-contacts',
            json={'name': 'Name Only Person'}
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['contact']['name'] == 'Name Only Person'
        assert data['contact']['alias'] is None

        with app.app_context():
            ic = db.session.get(InternalContact, data['contact']['id'])
            db.session.delete(ic)
            db.session.commit()

    def test_create_requires_name_or_alias(self, client):
        """Returns 400 if neither name nor alias provided."""
        resp = client.post('/api/internal-contacts', json={})
        assert resp.status_code == 400

    def test_update_internal_contact(self, client, app):
        """PATCH updates contact details."""
        with app.app_context():
            ic = InternalContact(name='Updatable', alias='upd')
            db.session.add(ic)
            db.session.commit()
            ic_id = ic.id

        resp = client.patch(
            f'/api/internal-contacts/{ic_id}',
            json={'name': 'Updated Name', 'role': 'DSS'}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['contact']['name'] == 'Updated Name'
        assert data['contact']['role'] == 'DSS'

        with app.app_context():
            ic = db.session.get(InternalContact, ic_id)
            db.session.delete(ic)
            db.session.commit()

    def test_add_internal_contact_attendee_to_note(self, client, app, sample_data):
        """POST internal_contact type to note attendees endpoint."""
        with app.app_context():
            ic = InternalContact(name='IC Attendee', alias='icatt', role='DAE')
            db.session.add(ic)
            db.session.commit()
            ic_id = ic.id

        note_id = sample_data['call1_id']
        resp = client.post(
            f'/api/note/{note_id}/attendees',
            json={'type': 'internal_contact', 'id': ic_id}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['attendee']['person_type'] == 'internal_contact'
        assert data['attendee']['display_name'] == 'IC Attendee'

        # Cleanup
        client.delete(f'/api/note/{note_id}/attendees/{data["attendee"]["id"]}')
        with app.app_context():
            ic = db.session.get(InternalContact, ic_id)
            db.session.delete(ic)
            db.session.commit()

    def test_search_internal_contacts(self, client, app):
        """Attendee search returns matching internal contacts."""
        with app.app_context():
            ic = InternalContact(name='Searchable DAE', alias='searchdae', role='DAE')
            db.session.add(ic)
            db.session.commit()

        resp = client.get('/api/attendee-search?q=searchable')
        data = resp.get_json()
        assert any(
            r['type'] == 'internal_contact' and 'Searchable DAE' in r['name']
            for r in data['results']
        )

        with app.app_context():
            ic = InternalContact.query.filter_by(alias='searchdae').first()
            if ic:
                db.session.delete(ic)
                db.session.commit()

    def test_search_internal_contacts_by_alias(self, client, app):
        """Attendee search matches on alias too."""
        with app.app_context():
            ic = InternalContact(name='DAE By Alias', alias='uniquedalias', role='DAE')
            db.session.add(ic)
            db.session.commit()

        resp = client.get('/api/attendee-search?q=uniquedalias')
        data = resp.get_json()
        assert any(
            r['type'] == 'internal_contact' and r['id'] is not None
            for r in data['results']
        )

        with app.app_context():
            ic = InternalContact.query.filter_by(alias='uniquedalias').first()
            if ic:
                db.session.delete(ic)
                db.session.commit()
