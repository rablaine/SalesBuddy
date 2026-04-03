"""Tests for internal contact list, edit, and delete routes."""
import pytest
from app.models import db, InternalContact, NoteAttendee, Note


class TestInternalContactsListPage:
    """Test the internal contacts list page."""

    def test_list_page_loads(self, client, app):
        """GET /internal-contacts returns 200."""
        resp = client.get('/internal-contacts')
        assert resp.status_code == 200
        assert b'Internal Contacts' in resp.data

    def test_list_shows_contacts(self, client, app):
        """List page shows existing internal contacts."""
        with app.app_context():
            ic = InternalContact(name='Listed Person', alias='listedp', role='DAE')
            db.session.add(ic)
            db.session.commit()

        resp = client.get('/internal-contacts')
        assert b'Listed Person' in resp.data
        assert b'listedp@microsoft.com' in resp.data
        assert b'DAE' in resp.data

        with app.app_context():
            ic = InternalContact.query.filter_by(alias='listedp').first()
            db.session.delete(ic)
            db.session.commit()

    def test_empty_list_shows_message(self, client, app):
        """Empty list shows helpful message."""
        with app.app_context():
            InternalContact.query.delete()
            db.session.commit()

        resp = client.get('/internal-contacts')
        assert b'No internal contacts yet' in resp.data


class TestInternalContactNewPage:
    """Test creating new internal contacts via the form."""

    def test_new_form_loads(self, client, app):
        """GET /internal-contacts/new returns 200."""
        resp = client.get('/internal-contacts/new')
        assert resp.status_code == 200
        assert b'New' in resp.data

    def test_create_contact(self, client, app):
        """POST creates a contact and redirects to list."""
        resp = client.post('/internal-contacts/new', data={
            'name': 'New IC',
            'alias': 'newic',
            'role': 'DSS',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b'New IC' in resp.data

        with app.app_context():
            ic = InternalContact.query.filter_by(alias='newic').first()
            assert ic is not None
            assert ic.name == 'New IC'
            assert ic.role == 'DSS'
            db.session.delete(ic)
            db.session.commit()

    def test_create_strips_microsoft_domain(self, client, app):
        """Alias with @microsoft.com gets stripped."""
        resp = client.post('/internal-contacts/new', data={
            'name': 'Domain Test',
            'alias': 'domtest@microsoft.com',
            'role': '',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            ic = InternalContact.query.filter_by(alias='domtest').first()
            assert ic is not None
            db.session.delete(ic)
            db.session.commit()

    def test_create_duplicate_alias_redirects(self, client, app):
        """Creating with existing alias redirects to edit."""
        with app.app_context():
            ic = InternalContact(name='Original', alias='dupalias', role='DAE')
            db.session.add(ic)
            db.session.commit()
            ic_id = ic.id

        resp = client.post('/internal-contacts/new', data={
            'name': 'Duplicate',
            'alias': 'dupalias',
            'role': '',
        })
        assert resp.status_code == 302
        assert f'/internal-contacts/{ic_id}/edit' in resp.headers['Location']

        with app.app_context():
            ic = db.session.get(InternalContact, ic_id)
            db.session.delete(ic)
            db.session.commit()

    def test_create_requires_name(self, client, app):
        """POST without name redirects back with flash."""
        resp = client.post('/internal-contacts/new', data={
            'name': '',
            'alias': 'noname',
        })
        assert resp.status_code == 302
        assert '/internal-contacts/new' in resp.headers['Location']


class TestInternalContactEditPage:
    """Test editing internal contacts."""

    def test_edit_form_loads(self, client, app):
        """GET /internal-contacts/<id>/edit returns 200 with prefilled data."""
        with app.app_context():
            ic = InternalContact(name='Edit Me', alias='editme', role='DAE')
            db.session.add(ic)
            db.session.commit()
            ic_id = ic.id

        resp = client.get(f'/internal-contacts/{ic_id}/edit')
        assert resp.status_code == 200
        assert b'Edit Me' in resp.data
        assert b'editme' in resp.data

        with app.app_context():
            ic = db.session.get(InternalContact, ic_id)
            db.session.delete(ic)
            db.session.commit()

    def test_edit_saves_changes(self, client, app):
        """POST updates the contact and redirects to list."""
        with app.app_context():
            ic = InternalContact(name='Before', alias='before', role='DAE')
            db.session.add(ic)
            db.session.commit()
            ic_id = ic.id

        resp = client.post(f'/internal-contacts/{ic_id}/edit', data={
            'name': 'After',
            'alias': 'after',
            'role': 'DSS',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b'After' in resp.data

        with app.app_context():
            ic = db.session.get(InternalContact, ic_id)
            assert ic.name == 'After'
            assert ic.alias == 'after'
            assert ic.role == 'DSS'
            db.session.delete(ic)
            db.session.commit()

    def test_edit_duplicate_alias_shows_warning(self, client, app):
        """Editing to a taken alias shows a warning."""
        with app.app_context():
            ic1 = InternalContact(name='First', alias='taken')
            ic2 = InternalContact(name='Second', alias='second')
            db.session.add_all([ic1, ic2])
            db.session.commit()
            ic2_id = ic2.id

        resp = client.post(f'/internal-contacts/{ic2_id}/edit', data={
            'name': 'Second',
            'alias': 'taken',
            'role': '',
        })
        assert resp.status_code == 302  # Redirects back to edit

        with app.app_context():
            InternalContact.query.filter(
                InternalContact.alias.in_(['taken', 'second'])
            ).delete()
            db.session.commit()

    def test_edit_nonexistent_returns_404(self, client):
        """Editing a nonexistent contact returns 404."""
        resp = client.get('/internal-contacts/99999/edit')
        assert resp.status_code == 404


class TestInternalContactDelete:
    """Test deleting internal contacts."""

    def test_delete_contact(self, client, app):
        """POST delete removes the contact."""
        with app.app_context():
            ic = InternalContact(name='Delete Me', alias='deleteme')
            db.session.add(ic)
            db.session.commit()
            ic_id = ic.id

        resp = client.post(f'/internal-contacts/{ic_id}/delete', follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            assert db.session.get(InternalContact, ic_id) is None

    def test_delete_preserves_attendee_records(self, client, app, sample_data):
        """Deleting converts note attendees to external type."""
        with app.app_context():
            ic = InternalContact(name='Will Delete', alias='willdelete', role='DAE')
            db.session.add(ic)
            db.session.flush()

            note = db.session.get(Note, sample_data['call1_id'])
            att = NoteAttendee(note_id=note.id, internal_contact_id=ic.id)
            db.session.add(att)
            db.session.commit()
            ic_id = ic.id
            att_id = att.id

        client.post(f'/internal-contacts/{ic_id}/delete')

        with app.app_context():
            att = db.session.get(NoteAttendee, att_id)
            assert att is not None
            assert att.internal_contact_id is None
            assert att.external_name == 'Will Delete'
            assert att.external_email == 'willdelete@microsoft.com'
            assert att.person_type == 'external'

            db.session.delete(att)
            db.session.commit()
