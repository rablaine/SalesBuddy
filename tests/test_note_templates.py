"""
Tests for the Note Templates feature (issue #47).
Covers template CRUD, API endpoints, settings page, default templates,
and template application on the note form.
"""
import pytest


# =============================================================================
# Helper fixtures
# =============================================================================

@pytest.fixture
def template_data(app):
    """Create sample note templates for tests."""
    with app.app_context():
        from app.models import db, NoteTemplate
        t1 = NoteTemplate(name='Standard Call Log', content='<h2>Attendees</h2><ul><li><br></li></ul>')
        t2 = NoteTemplate(name='Quick Check-In', content='<h2>Summary</h2><p><br></p>')
        t3 = NoteTemplate(name='Deep Dive', content='<h2>Architecture</h2><p><br></p>')
        db.session.add_all([t1, t2, t3])
        db.session.commit()
        return {'t1': t1.id, 't2': t2.id, 't3': t3.id}


# =============================================================================
# Settings page
# =============================================================================

class TestSettingsPage:
    """Tests for the /preferences settings page."""

    def test_settings_page_loads(self, client):
        """Settings page renders without error."""
        response = client.get('/preferences')
        assert response.status_code == 200
        assert b'Settings' in response.data

    def test_settings_page_shows_all_cards(self, client):
        """Settings page has Appearance, WorkIQ, and Note Templates cards."""
        response = client.get('/preferences')
        assert b'Appearance' in response.data
        assert b'WorkIQ Settings' in response.data
        assert b'Note Templates' in response.data

    def test_settings_page_lists_templates(self, client, template_data):
        """Settings page lists all templates in the dropdown."""
        response = client.get('/preferences')
        assert b'Standard Call Log' in response.data
        assert b'Quick Check-In' in response.data
        assert b'Deep Dive' in response.data

    def test_settings_page_no_templates_message(self, client, app):
        """Settings page shows helpful message when no templates exist."""
        with app.app_context():
            from app.models import db, NoteTemplate
            NoteTemplate.query.delete()
            db.session.commit()
        response = client.get('/preferences')
        assert b'No templates yet' in response.data


# =============================================================================
# Template CRUD
# =============================================================================

class TestTemplateCRUD:
    """Tests for template create, read, update, delete."""

    def test_create_template_page_loads(self, client):
        """GET /templates/new renders the editor."""
        response = client.get('/templates/new')
        assert response.status_code == 200
        assert b'New' in response.data
        assert b'Template' in response.data

    def test_create_template(self, client, app):
        """POST /templates creates a new template."""
        response = client.post('/templates', data={
            'name': 'Test Template',
            'content': '<h2>Test</h2><p>Hello</p>',
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b'Test Template' in response.data

        with app.app_context():
            from app.models import NoteTemplate
            t = NoteTemplate.query.filter_by(name='Test Template').first()
            assert t is not None
            assert '<h2>Test</h2>' in t.content

    def test_create_template_empty_name_rejected(self, client):
        """POST /templates with empty name flashes error."""
        response = client.post('/templates', data={
            'name': '',
            'content': '<p>Some content</p>',
        }, follow_redirects=True)
        assert b'Template name is required' in response.data

    def test_create_template_empty_content_rejected(self, client):
        """POST /templates with empty content flashes error."""
        response = client.post('/templates', data={
            'name': 'Empty',
            'content': '',
        }, follow_redirects=True)
        assert b'Template content is required' in response.data

    def test_edit_template_page_loads(self, client, template_data):
        """GET /templates/<id>/edit renders the editor with content."""
        response = client.get(f'/templates/{template_data["t1"]}/edit')
        assert response.status_code == 200
        assert b'Standard Call Log' in response.data
        assert b'Attendees' in response.data

    def test_update_template(self, client, app, template_data):
        """POST /templates/<id> updates an existing template."""
        tid = template_data['t1']
        response = client.post(f'/templates/{tid}', data={
            'name': 'Renamed Template',
            'content': '<h2>Updated</h2>',
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b'Renamed Template' in response.data

        with app.app_context():
            from app.models import db, NoteTemplate
            t = db.session.get(NoteTemplate, tid)
            assert t.name == 'Renamed Template'
            assert '<h2>Updated</h2>' in t.content

    def test_delete_template(self, client, app, template_data):
        """POST /api/templates/<id>/delete removes the template."""
        tid = template_data['t1']
        response = client.post(f'/api/templates/{tid}/delete', follow_redirects=True)
        assert response.status_code == 200
        assert b'deleted' in response.data

        with app.app_context():
            from app.models import db, NoteTemplate
            assert db.session.get(NoteTemplate, tid) is None

    def test_delete_template_clears_default(self, client, app, template_data):
        """Deleting a template that is set as default clears the FK."""
        tid = template_data['t1']
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.default_template_customer_id = tid
            db.session.commit()

        client.post(f'/api/templates/{tid}/delete', follow_redirects=True)

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.default_template_customer_id is None

    def test_delete_nonexistent_template_404(self, client):
        """Deleting a non-existent template returns 404."""
        response = client.post('/api/templates/99999/delete')
        assert response.status_code == 404


# =============================================================================
# Template API endpoints
# =============================================================================

class TestTemplateAPI:
    """Tests for template JSON API endpoints."""

    def test_api_templates_list(self, client, template_data):
        """GET /api/templates returns all templates as JSON."""
        response = client.get('/api/templates')
        assert response.status_code == 200
        data = response.get_json()
        assert len(data) == 3
        names = {t['name'] for t in data}
        assert 'Standard Call Log' in names
        assert 'Quick Check-In' in names

    def test_api_templates_list_empty(self, client, app):
        """GET /api/templates returns empty list when no templates."""
        with app.app_context():
            from app.models import db, NoteTemplate
            NoteTemplate.query.delete()
            db.session.commit()
        response = client.get('/api/templates')
        assert response.status_code == 200
        assert response.get_json() == []

    def test_api_template_get(self, client, template_data):
        """GET /api/templates/<id> returns template content."""
        response = client.get(f'/api/templates/{template_data["t1"]}')
        assert response.status_code == 200
        data = response.get_json()
        assert data['name'] == 'Standard Call Log'
        assert 'Attendees' in data['content']

    def test_api_template_get_404(self, client):
        """GET /api/templates/<id> returns 404 for missing template."""
        response = client.get('/api/templates/99999')
        assert response.status_code == 404


# =============================================================================
# Default template preferences
# =============================================================================

class TestDefaultTemplates:
    """Tests for default template preferences."""

    def test_save_default_templates(self, client, app, template_data):
        """POST /api/preferences/default-templates saves both defaults."""
        response = client.post('/api/preferences/default-templates', data={
            'default_template_customer_id': str(template_data['t1']),
            'default_template_noncustomer_id': str(template_data['t2']),
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b'Default templates saved' in response.data

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.default_template_customer_id == template_data['t1']
            assert pref.default_template_noncustomer_id == template_data['t2']

    def test_save_default_templates_clear(self, client, app, template_data):
        """POST with empty values clears both defaults."""
        # First set them
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.default_template_customer_id = template_data['t1']
            pref.default_template_noncustomer_id = template_data['t2']
            db.session.commit()

        # Then clear them
        response = client.post('/api/preferences/default-templates', data={
            'default_template_customer_id': '',
            'default_template_noncustomer_id': '',
        }, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.default_template_customer_id is None
            assert pref.default_template_noncustomer_id is None

    def test_save_only_customer_default(self, client, app, template_data):
        """Can set customer default without setting non-customer."""
        response = client.post('/api/preferences/default-templates', data={
            'default_template_customer_id': str(template_data['t1']),
            'default_template_noncustomer_id': '',
        }, follow_redirects=True)
        assert response.status_code == 200

        with app.app_context():
            from app.models import UserPreference
            pref = UserPreference.query.first()
            assert pref.default_template_customer_id == template_data['t1']
            assert pref.default_template_noncustomer_id is None


# =============================================================================
# Note form template integration
# =============================================================================

class TestNoteFormTemplates:
    """Tests for template selector and auto-populate on the note form."""

    def test_note_form_shows_template_dropdown(self, client, app, template_data):
        """Note create form includes the template selector dropdown."""
        with app.app_context():
            from app.models import db, Customer
            c = Customer(name='Test Customer', tpid=900001)
            db.session.add(c)
            db.session.commit()
            cid = c.id

        response = client.get(f'/note/new?customer_id={cid}')
        assert response.status_code == 200
        assert b'templateSelect' in response.data
        assert b'Apply template...' in response.data
        assert b'Standard Call Log' in response.data

    def test_note_edit_form_shows_template_dropdown(self, client, app, template_data):
        """Note edit form includes the template selector dropdown."""
        with app.app_context():
            from app.models import db, Customer, Note
            from datetime import datetime
            c = Customer(name='Test Customer Edit', tpid=900002)
            db.session.add(c)
            db.session.flush()
            n = Note(customer_id=c.id, call_date=datetime.now(), content='<p>Test</p>')
            db.session.add(n)
            db.session.commit()
            nid = n.id

        response = client.get(f'/note/{nid}/edit')
        assert response.status_code == 200
        assert b'templateSelect' in response.data

    def test_note_form_auto_populates_customer_default(self, client, app, template_data):
        """New customer note auto-populates with the customer default template."""
        with app.app_context():
            from app.models import db, Customer, UserPreference
            c = Customer(name='Auto Populate Test', tpid=900003)
            db.session.add(c)
            db.session.flush()
            cid = c.id
            pref = UserPreference.query.first()
            pref.default_template_customer_id = template_data['t1']
            db.session.commit()

        response = client.get(f'/note/new?customer_id={cid}')
        assert response.status_code == 200
        # The default template content should be in the page for JS auto-populate
        assert b'Attendees' in response.data

    def test_note_form_auto_populates_noncustomer_default(self, client, app, template_data):
        """New general note auto-populates with the non-customer default template."""
        with app.app_context():
            from app.models import db, UserPreference
            pref = UserPreference.query.first()
            pref.default_template_noncustomer_id = template_data['t2']
            db.session.commit()

        response = client.get('/note/new')
        assert response.status_code == 200
        # The default template content should be in the page for JS auto-populate
        assert b'Summary' in response.data

    def test_note_form_no_default_when_not_set(self, client, app, template_data):
        """No auto-populate when defaults are not set."""
        with app.app_context():
            from app.models import db, Customer, UserPreference
            c = Customer(name='No Default Test', tpid=900005)
            db.session.add(c)
            db.session.flush()
            cid = c.id
            pref = UserPreference.query.first()
            pref.default_template_customer_id = None
            pref.default_template_noncustomer_id = None
            db.session.commit()

        response = client.get(f'/note/new?customer_id={cid}')
        assert response.status_code == 200
        # The default_template_content variable should not cause auto-populate JS
        assert b'default_template_content' not in response.data


# =============================================================================
# NoteTemplate model
# =============================================================================

class TestNoteTemplateModel:
    """Tests for the NoteTemplate model."""

    def test_create_template_model(self, app):
        """Can create a NoteTemplate instance."""
        with app.app_context():
            from app.models import db, NoteTemplate
            t = NoteTemplate(name='Model Test', content='<p>Test</p>')
            db.session.add(t)
            db.session.commit()
            assert t.id is not None
            assert t.name == 'Model Test'
            assert t.created_at is not None
            assert t.updated_at is not None

    def test_template_repr(self, app):
        """NoteTemplate repr is meaningful."""
        with app.app_context():
            from app.models import db, NoteTemplate
            t = NoteTemplate(name='Repr Test', content='<p>x</p>')
            db.session.add(t)
            db.session.commit()
            assert 'Repr Test' in repr(t)

    def test_user_preference_template_relationships(self, app, template_data):
        """UserPreference FK relationships to templates work."""
        with app.app_context():
            from app.models import db, UserPreference, NoteTemplate
            pref = UserPreference.query.first()
            pref.default_template_customer_id = template_data['t1']
            pref.default_template_noncustomer_id = template_data['t2']
            db.session.commit()

            # Refresh and check relationships
            db.session.refresh(pref)
            assert pref.default_template_customer is not None
            assert pref.default_template_customer.name == 'Standard Call Log'
            assert pref.default_template_noncustomer is not None
            assert pref.default_template_noncustomer.name == 'Quick Check-In'
