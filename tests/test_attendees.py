"""Tests for NoteAttendee model and attendee API endpoints."""
import pytest
from app.models import (
    db, Note, NoteAttendee, CustomerContact, PartnerContact,
    SolutionEngineer, Seller, Partner,
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
