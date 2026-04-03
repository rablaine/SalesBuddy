"""
Tests for general notes (notes not associated with a customer).

Tests cover:
- Creating general notes (no customer_id)
- Editing general notes
- General notes in list view with filtering
- General notes in detail view
- General notes in Connect Export (text, markdown, JSON)
- General notes in search results
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest


class TestGeneralNoteCreate:
    """Tests for creating notes without a customer."""

    def test_create_form_loads_without_customer(self, client):
        """The create form should load without a customer_id (general note)."""
        response = client.get('/note/new')
        assert response.status_code == 200
        assert b'General Note' in response.data

    def test_create_form_shows_general_note_info(self, client):
        """General note form should show info alert instead of customer dropdown."""
        response = client.get('/note/new')
        assert response.status_code == 200
        assert b'not associated with a specific customer' in response.data

    def test_create_general_note_post(self, client, app):
        """Should successfully create a note without a customer."""
        response = client.post('/note/new', data={
            'call_date': '2025-06-15',
            'content': '<p>Internal planning session notes</p>',
        }, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.models import Note
            cl = Note.query.filter_by(content='<p>Internal planning session notes</p>').first()
            assert cl is not None
            assert cl.customer_id is None

    def test_create_general_note_with_topics(self, client, app, sample_data):
        """General notes should support topic tagging."""
        topic_id = sample_data['topic1_id']
        response = client.post('/note/new', data={
            'call_date': '2025-06-15',
            'content': '<p>Topic research notes</p>',
            'topic_ids': [str(topic_id)],
        }, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.models import Note
            cl = Note.query.filter_by(content='<p>Topic research notes</p>').first()
            assert cl is not None
            assert cl.customer_id is None
            assert len(cl.topics) == 1

    def test_create_general_note_no_milestone_section(self, client):
        """General note form should not show milestone picker section."""
        response = client.get('/note/new')
        assert response.status_code == 200
        assert b'Milestone Picker' not in response.data

    def test_create_general_note_no_task_section(self, client):
        """General note form should not show task creation section."""
        response = client.get('/note/new')
        assert response.status_code == 200
        assert b'Create MSX Task' not in response.data

    def test_create_with_customer_still_works(self, client, app, sample_data):
        """Creating a note with a customer should still work normally."""
        customer_id = sample_data['customer1_id']
        response = client.post('/note/new', data={
            'customer_id': str(customer_id),
            'call_date': '2025-06-15',
            'content': '<p>Customer meeting notes</p>',
        }, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.models import Note
            cl = Note.query.filter_by(content='<p>Customer meeting notes</p>').first()
            assert cl is not None
            assert cl.customer_id == customer_id


class TestGeneralNoteEdit:
    """Tests for editing general notes."""

    def test_edit_general_note_loads(self, client, app):
        """Edit form should load for a general note."""
        with app.app_context():
            from app.models import db, Note
            cl = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>General note to edit</p>',
            )
            db.session.add(cl)
            db.session.commit()
            cl_id = cl.id

        response = client.get(f'/note/{cl_id}/edit')
        assert response.status_code == 200
        assert b'General Note' in response.data
        assert b'General note to edit' in response.data

    def test_edit_general_note_post(self, client, app):
        """Should successfully update a general note."""
        with app.app_context():
            from app.models import db, Note
            cl = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>Original content</p>',
            )
            db.session.add(cl)
            db.session.commit()
            cl_id = cl.id

        response = client.post(f'/note/{cl_id}/edit', data={
            'call_date': '2025-06-15',
            'content': '<p>Updated general note</p>',
        }, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.models import Note
            cl = Note.query.get(cl_id)
            assert cl.content == '<p>Updated general note</p>'
            assert cl.customer_id is None


class TestGeneralNoteListView:
    """Tests for general notes in the list view."""

    def _create_mixed_notes(self, app, sample_data):
        """Helper to create both customer and general notes."""
        with app.app_context():
            from app.models import db, Note
            general = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>General planning note</p>',
            )
            db.session.add(general)
            db.session.commit()
            return general.id

    def test_list_shows_general_notes(self, client, app, sample_data):
        """General notes should appear in the notes list."""
        general_id = self._create_mixed_notes(app, sample_data)
        response = client.get('/notes')
        assert response.status_code == 200
        assert b'General Note' in response.data
        assert b'Acme Corp' in response.data  # Customer note still shows

    def test_filter_customer_notes(self, client, app, sample_data):
        """Filter=customer should show only customer-associated notes."""
        self._create_mixed_notes(app, sample_data)
        response = client.get('/notes?filter=customer')
        assert response.status_code == 200
        assert b'Acme Corp' in response.data
        assert b'General planning note' not in response.data

    def test_filter_general_notes(self, client, app, sample_data):
        """Filter=general should show only general notes."""
        self._create_mixed_notes(app, sample_data)
        response = client.get('/notes?filter=general')
        assert response.status_code == 200
        assert b'General Note' in response.data
        # Customer account context should not appear
        assert b'Acme Corp' not in response.data

    def test_no_filter_shows_all(self, client, app, sample_data):
        """No filter should show both customer and general notes."""
        self._create_mixed_notes(app, sample_data)
        response = client.get('/notes')
        assert response.status_code == 200
        assert b'General Note' in response.data
        assert b'Acme Corp' in response.data


class TestGeneralNoteDetailView:
    """Tests for viewing a general note."""

    def test_view_general_note(self, client, app):
        """General note detail should render with 'General Note' label."""
        with app.app_context():
            from app.models import db, Note
            cl = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>Detailed general note</p>',
            )
            db.session.add(cl)
            db.session.commit()
            cl_id = cl.id

        response = client.get(f'/note/{cl_id}')
        assert response.status_code == 200
        assert b'General Note' in response.data
        assert b'Detailed general note' in response.data


class TestGeneralNotesInConnectExport:
    """Tests for general notes in Connect Export."""

    def _create_export_data(self, app, sample_data):
        """Create mixed data for export testing."""
        with app.app_context():
            from app.models import db, Note, Topic
            topic = Topic.query.get(sample_data['topic1_id'])
            general = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>General research on Azure trends</p>',
            )
            general.topics.append(topic)
            db.session.add(general)
            db.session.commit()

    def test_build_export_data_includes_general_notes(self, app, sample_data):
        """_build_export_data should include general_notes in output."""
        self._create_export_data(app, sample_data)
        with app.app_context():
            from app.routes.connect_export import _build_export_data
            data = _build_export_data(
                date(2020, 1, 1),
                date(2030, 12, 31),
            )
            assert 'general_notes' in data
            assert len(data['general_notes']) == 1
            assert data['general_notes'][0]['content_text']
            assert 'Azure VM' in data['general_notes'][0]['topics']

    def test_summary_includes_general_notes_count(self, app, sample_data):
        """Summary should track general_notes_count."""
        self._create_export_data(app, sample_data)
        with app.app_context():
            from app.routes.connect_export import _build_export_data
            data = _build_export_data(
                date(2020, 1, 1),
                date(2030, 12, 31),
            )
            assert data['summary']['general_notes_count'] == 1

    def test_text_export_includes_general_notes(self, app, sample_data):
        """Text export should have a General Notes section."""
        self._create_export_data(app, sample_data)
        with app.app_context():
            from app.routes.connect_export import _build_export_data, _build_text_export
            data = _build_export_data(
                date(2020, 1, 1),
                date(2030, 12, 31),
            )
            text = _build_text_export(data, 'Test Export')
            assert 'GENERAL NOTES' in text
            assert 'Azure trends' in text

    def test_markdown_export_includes_general_notes(self, app, sample_data):
        """Markdown export should have a General Notes section."""
        self._create_export_data(app, sample_data)
        with app.app_context():
            from app.routes.connect_export import _build_export_data, _build_markdown_export
            data = _build_export_data(
                date(2020, 1, 1),
                date(2030, 12, 31),
            )
            md = _build_markdown_export(data, 'Test Export')
            assert '## General Notes' in md
            assert 'Azure trends' in md

    def test_json_export_includes_general_notes(self, app, sample_data):
        """JSON export should include general_notes key."""
        self._create_export_data(app, sample_data)
        with app.app_context():
            from app.routes.connect_export import _build_export_data, _build_json_export
            data = _build_export_data(
                date(2020, 1, 1),
                date(2030, 12, 31),
            )
            json_data = _build_json_export(data, 'Test Export')
            assert 'general_notes' in json_data
            assert len(json_data['general_notes']) == 1

    def test_general_notes_topics_in_export_summary(self, app, sample_data):
        """Topics from general notes should appear in export topic summary."""
        self._create_export_data(app, sample_data)
        with app.app_context():
            from app.routes.connect_export import _build_export_data
            data = _build_export_data(
                date(2020, 1, 1),
                date(2030, 12, 31),
            )
            # Azure VM topic should appear in summary
            topic_names = [t['name'] for t in data['summary']['topics']]
            assert 'Azure VM' in topic_names
            # The topic from general notes should list 'General Notes' as a customer
            azure_vm = next(t for t in data['summary']['topics'] if t['name'] == 'Azure VM')
            assert 'General Notes' in azure_vm['customers']

    def test_export_no_general_notes_section_when_none(self, app, sample_data):
        """Text export should not have General Notes section when there are none."""
        with app.app_context():
            from app.routes.connect_export import _build_export_data, _build_text_export
            data = _build_export_data(
                date(2020, 1, 1),
                date(2030, 12, 31),
            )
            text = _build_text_export(data, 'Test Export')
            assert 'GENERAL NOTES' not in text

    def test_api_generate_includes_general_notes(self, client, app, sample_data):
        """The generate API should include general notes in the export."""
        self._create_export_data(app, sample_data)
        response = client.post('/api/connect-export/generate',
                               json={'name': 'General Test',
                                     'start_date': '2020-01-01',
                                     'end_date': '2030-12-31'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        # Total should include the general note
        assert data['summary']['total_notes'] == 3  # 2 customer + 1 general
        assert 'GENERAL NOTES' in data['text_export']


class TestGeneralNoteModel:
    """Tests for the Note model with nullable customer_id."""

    def test_create_note_without_customer(self, app):
        """Note should allow null customer_id."""
        with app.app_context():
            from app.models import db, Note
            cl = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>No customer</p>',
            )
            db.session.add(cl)
            db.session.commit()

            fetched = Note.query.get(cl.id)
            assert fetched.customer_id is None
            assert fetched.customer is None

    def test_repr_general_note(self, app):
        """Note repr should show 'General' for notes without a customer."""
        with app.app_context():
            from app.models import db, Note
            cl = Note(
                customer_id=None,
                call_date=datetime(2025, 6, 15),
                content='<p>Test</p>',
            )
            db.session.add(cl)
            db.session.commit()
            assert 'General' in repr(cl)

    def test_seller_property_returns_none(self, app):
        """seller property should return None for general notes."""
        with app.app_context():
            from app.models import db, Note
            cl = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>Test</p>',
            )
            db.session.add(cl)
            db.session.commit()
            assert cl.seller is None

    def test_territory_property_returns_none(self, app):
        """territory property should return None for general notes."""
        with app.app_context():
            from app.models import db, Note
            cl = Note(
                customer_id=None,
                call_date=datetime.now(timezone.utc),
                content='<p>Test</p>',
            )
            db.session.add(cl)
            db.session.commit()
            assert cl.territory is None


class TestGeneralNotesOnCalendar:
    """Tests for general notes appearing on the calendar API."""

    def test_calendar_includes_general_notes(self, client, app):
        """General notes should appear in the calendar API response."""
        with app.app_context():
            from app.models import db, Note
            now = datetime.now(timezone.utc)
            note = Note(
                customer_id=None,
                call_date=now,
                content='<p>Internal planning meeting</p>',
            )
            db.session.add(note)
            db.session.commit()

        response = client.get(f'/api/notes/calendar?year={now.year}&month={now.month}')
        assert response.status_code == 200
        data = response.get_json()

        day_entries = data['days'].get(str(now.day), [])
        general = [e for e in day_entries if e.get('is_general')]
        assert len(general) >= 1
        assert general[0]['customer_id'] is None
        assert general[0]['is_general'] is True

    def test_calendar_general_note_uses_topic_as_label(self, client, app):
        """When a general note has topics, the first topic name should be the label."""
        with app.app_context():
            from app.models import db, Note, Topic
            now = datetime.now(timezone.utc)
            topic = Topic(name='Team Sync')
            db.session.add(topic)
            db.session.flush()

            note = Note(
                customer_id=None,
                call_date=now,
                content='<p>Weekly standup</p>',
            )
            note.topics.append(topic)
            db.session.add(note)
            db.session.commit()

        response = client.get(f'/api/notes/calendar?year={now.year}&month={now.month}')
        data = response.get_json()

        day_entries = data['days'].get(str(now.day), [])
        general = [e for e in day_entries if e.get('is_general')]
        assert any(e['customer_name'] == 'Team Sync' for e in general)

    def test_calendar_general_note_uses_content_snippet(self, client, app):
        """When a general note has no topics, a content snippet should be the label."""
        with app.app_context():
            from app.models import db, Note
            now = datetime.now(timezone.utc)
            note = Note(
                customer_id=None,
                call_date=now,
                content='<p>Preparing quarterly business review slides</p>',
            )
            db.session.add(note)
            db.session.commit()

        response = client.get(f'/api/notes/calendar?year={now.year}&month={now.month}')
        data = response.get_json()

        day_entries = data['days'].get(str(now.day), [])
        general = [e for e in day_entries if e.get('is_general')]
        assert len(general) >= 1
        assert 'Preparing quarterly' in general[0]['customer_name']

    def test_calendar_customer_notes_not_marked_general(self, client, sample_data):
        """Customer-associated notes should have is_general=False."""
        from datetime import datetime
        response = client.get(f'/api/notes/calendar?year={datetime.now().year}&month={datetime.now().month}')
        data = response.get_json()

        for day_entries in data['days'].values():
            for entry in day_entries:
                if entry['customer_id'] is not None:
                    assert entry['is_general'] is False
