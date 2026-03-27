"""
Tests for internal projects - inline creation and copilot_saved filtering.
"""
import json

import pytest
from app.models import db, Project


class TestProjectCreateInlineAPI:
    """Tests for POST /api/project/create-inline."""

    def test_create_project_inline(self, client, app):
        """Should create a project with just a title."""
        resp = client.post('/api/project/create-inline',
                           data=json.dumps({'title': 'My New Project'}),
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['title'] == 'My New Project'
        assert data['project_type'] == 'general'
        assert 'id' in data

        with app.app_context():
            proj = db.session.get(Project, data['id'])
            assert proj is not None
            assert proj.title == 'My New Project'
            assert proj.status == 'Active'

    def test_create_project_inline_with_type(self, client, app):
        """Should accept a project_type parameter."""
        resp = client.post('/api/project/create-inline',
                           data=json.dumps({'title': 'Training Plan',
                                            'project_type': 'training'}),
                           content_type='application/json')
        data = resp.get_json()
        assert data['success'] is True
        assert data['project_type'] == 'training'

    def test_create_project_inline_with_description(self, client, app):
        """Should accept an optional description."""
        resp = client.post('/api/project/create-inline',
                           data=json.dumps({'title': 'Cert Prep',
                                            'description': 'AZ-104 study'}),
                           content_type='application/json')
        data = resp.get_json()
        assert data['success'] is True

        with app.app_context():
            proj = db.session.get(Project, data['id'])
            assert proj.description == 'AZ-104 study'

    def test_create_project_inline_rejects_empty_title(self, client):
        """Should return 400 when title is empty."""
        resp = client.post('/api/project/create-inline',
                           data=json.dumps({'title': '  '}),
                           content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False

    def test_create_project_inline_rejects_copilot_saved(self, client, app):
        """Should not allow creating copilot_saved projects via inline API."""
        resp = client.post('/api/project/create-inline',
                           data=json.dumps({'title': 'Sneaky',
                                            'project_type': 'copilot_saved'}),
                           content_type='application/json')
        data = resp.get_json()
        assert data['success'] is True
        # Type should be forced to 'general', not 'copilot_saved'
        assert data['project_type'] == 'general'

    def test_create_project_inline_invalid_type_defaults_general(self, client):
        """Should default to 'general' for unknown project types."""
        resp = client.post('/api/project/create-inline',
                           data=json.dumps({'title': 'Whatever',
                                            'project_type': 'bogus'}),
                           content_type='application/json')
        data = resp.get_json()
        assert data['success'] is True
        assert data['project_type'] == 'general'

    def test_create_project_inline_with_due_date(self, client, app):
        """Should accept and store a due_date."""
        resp = client.post('/api/project/create-inline',
                           data=json.dumps({'title': 'Deadlined Project',
                                            'due_date': '2026-06-15'}),
                           content_type='application/json')
        data = resp.get_json()
        assert data['success'] is True

        with app.app_context():
            proj = db.session.get(Project, data['id'])
            assert proj.due_date is not None
            assert proj.due_date.isoformat() == '2026-06-15'


class TestProjectFormPartial:
    """Tests that the project form partial renders correctly."""

    def test_dedicated_project_form_renders(self, client):
        """The dedicated /project/new page should render using the partial."""
        resp = client.get('/project/new')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Title' in html
        assert 'Description' in html
        assert 'Due Date' in html

    def test_note_form_has_project_flyout(self, client):
        """The general note form should include the project flyout offcanvas."""
        resp = client.get('/note/new')
        html = resp.data.decode()
        assert 'projectFlyout' in html
        assert 'Create New Project' in html


class TestCopilotSavedHiddenFromNoteForm:
    """Tests that copilot_saved projects are excluded from the note form."""

    def test_general_note_form_excludes_copilot_saved(self, client, app):
        """The new general note form should not list copilot_saved projects."""
        with app.app_context():
            # Create a normal project and a copilot_saved project
            normal = Project(title='Normal Project', project_type='general')
            hidden = Project(title='Hidden Copilot', project_type='copilot_saved')
            db.session.add_all([normal, hidden])
            db.session.commit()

        resp = client.get('/note/new')
        html = resp.data.decode()
        assert 'Normal Project' in html
        assert 'Hidden Copilot' not in html
