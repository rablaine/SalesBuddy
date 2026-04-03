"""
Tests for creating tasks directly from the milestone view page.

Covers:
- MsxTask model nullable note_id
- Milestone view showing tasks section
- POST /milestone/<id>/tasks endpoint
- Task creation modal behavior
"""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone

from app.models import db, MsxTask, Milestone, Customer, Note, User


@pytest.fixture
def sample_user(app):
    """Get the test user."""
    with app.app_context():
        return User.query.first()


class TestMsxTaskNullableNote:
    """Tests that MsxTask can be created without a note_id."""

    def test_create_task_without_note(self, app, client, db_session, sample_user):
        """MsxTask should allow null note_id for tasks created from milestone view."""
        milestone = Milestone(
            msx_milestone_id='ms-nullable-test',
            url='https://example.com/ms-nullable',
            title='Nullable Test Milestone',
        )
        db_session.add(milestone)
        db_session.flush()

        task = MsxTask(
            msx_task_id='task-no-calllog-001',
            subject='Standalone Task',
            task_category=861980004,
            task_category_name='Architecture Design Session',
            duration_minutes=60,
            is_hok=True,
            note_id=None,
            milestone_id=milestone.id,
        )
        db_session.add(task)
        db_session.commit()

        saved = MsxTask.query.filter_by(msx_task_id='task-no-calllog-001').first()
        assert saved is not None
        assert saved.note_id is None
        assert saved.milestone_id == milestone.id
        assert saved.subject == 'Standalone Task'
        assert saved.is_hok is True

    def test_create_task_with_note_still_works(self, app, client, db_session, sample_user):
        """MsxTask with note_id should still work (backward compatibility)."""
        customer = Customer(
            name='Task Compat Customer', tpid=8801,
        )
        db_session.add(customer)
        db_session.flush()

        milestone = Milestone(
            msx_milestone_id='ms-compat-test',
            url='https://example.com/ms-compat',
            title='Compat Test Milestone',
        )
        db_session.add(milestone)
        db_session.flush()

        note = Note(
            customer_id=customer.id,
            content='Test note',
            call_date=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )
        db_session.add(note)
        db_session.flush()

        task = MsxTask(
            msx_task_id='task-with-calllog-001',
            subject='Linked Task',
            task_category=861980002,
            task_category_name='Demo',
            duration_minutes=30,
            is_hok=True,
            note_id=note.id,
            milestone_id=milestone.id,
        )
        db_session.add(task)
        db_session.commit()

        saved = MsxTask.query.filter_by(msx_task_id='task-with-calllog-001').first()
        assert saved is not None
        assert saved.note_id == note.id
        assert saved.milestone_id == milestone.id


class TestMilestoneViewTasks:
    """Tests for the tasks section on the milestone view page."""

    def test_milestone_view_shows_tasks_section(self, app, client, db_session, sample_user):
        """Milestone view should show a Tasks card."""
        milestone = Milestone(
            msx_milestone_id='ms-tasks-view',
            url='https://example.com/ms-tasks-view',
            title='Tasks View Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.get(f'/milestone/{milestone.id}')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'Tasks' in html
        assert 'New Task' in html

    def test_milestone_view_shows_existing_tasks(self, app, client, db_session, sample_user):
        """Milestone view should display existing tasks."""
        milestone = Milestone(
            msx_milestone_id='ms-show-tasks',
            url='https://example.com/ms-show-tasks',
            title='Show Tasks Milestone',
        )
        db_session.add(milestone)
        db_session.flush()

        task = MsxTask(
            msx_task_id='task-display-001',
            msx_task_url='https://example.com/task-display',
            subject='Display Me Task',
            task_category=861980004,
            task_category_name='Architecture Design Session',
            duration_minutes=90,
            is_hok=True,
            note_id=None,
            milestone_id=milestone.id,
        )
        db_session.add(task)
        db_session.commit()

        response = client.get(f'/milestone/{milestone.id}')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'Display Me Task' in html
        assert 'Architecture Design Session' in html
        assert 'HoK' in html

    def test_milestone_view_shows_task_linked_note(self, app, client, db_session, sample_user):
        """Tasks linked to a note should show a link to that note."""
        customer = Customer(
            name='Task Link Customer', tpid=8802,
        )
        db_session.add(customer)
        db_session.flush()

        milestone = Milestone(
            msx_milestone_id='ms-task-link',
            url='https://example.com/ms-task-link',
            title='Task Link Milestone',
        )
        db_session.add(milestone)
        db_session.flush()

        note = Note(
            customer_id=customer.id,
            content='Link test note',
            call_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        db_session.add(note)
        db_session.flush()

        task = MsxTask(
            msx_task_id='task-linked-cl-001',
            subject='Linked CL Task',
            task_category=861980002,
            task_category_name='Demo',
            duration_minutes=60,
            is_hok=True,
            note_id=note.id,
            milestone_id=milestone.id,
        )
        db_session.add(task)
        db_session.commit()

        response = client.get(f'/milestone/{milestone.id}')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'Linked CL Task' in html
        assert 'Linked Note' in html
        assert f'/note/{note.id}' in html

    def test_milestone_view_no_new_task_without_msx_id(self, app, client, db_session, sample_user):
        """Milestone without MSX ID should not show the New Task button."""
        milestone = Milestone(
            msx_milestone_id=None,
            url='https://example.com/no-msx-id',
            title='No MSX ID Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.get(f'/milestone/{milestone.id}')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'Tasks' in html
        # Should NOT show New Task button or modal
        assert 'newTaskModal' not in html
        assert 'Tasks cannot be created' in html

    def test_milestone_view_empty_tasks(self, app, client, db_session, sample_user):
        """Milestone with MSX ID but no tasks shows helpful message."""
        milestone = Milestone(
            msx_milestone_id='ms-empty-tasks',
            url='https://example.com/ms-empty-tasks',
            title='Empty Tasks Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.get(f'/milestone/{milestone.id}')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'No tasks created yet' in html
        assert 'New Task' in html


class TestMilestoneCreateTask:
    """Tests for the POST /milestone/<id>/tasks endpoint."""

    @patch('app.services.msx_api.create_task')
    def test_create_task_success(self, mock_create, app, client, db_session, sample_user):
        """Successfully create a task from milestone view."""
        mock_create.return_value = {
            'success': True,
            'task_id': 'msx-new-task-guid',
            'task_url': 'https://example.com/task/msx-new-task-guid',
        }

        milestone = Milestone(
            msx_milestone_id='ms-create-task',
            url='https://example.com/ms-create-task',
            title='Create Task Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            json={
                'subject': 'New Architecture Session',
                'task_category': 861980004,
                'duration_minutes': 60,
                'description': 'Planning session for the migration',
                'due_date': '2026-03-15T23:59:59Z',
            },
            content_type='application/json',
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['task_id'] == 'msx-new-task-guid'

        # Check local record was created
        task = MsxTask.query.filter_by(msx_task_id='msx-new-task-guid').first()
        assert task is not None
        assert task.subject == 'New Architecture Session'
        assert task.task_category == 861980004
        assert task.task_category_name == 'Architecture Design Session'
        assert task.is_hok is True
        assert task.duration_minutes == 60
        assert task.note_id is None
        assert task.milestone_id == milestone.id
        assert task.description == 'Planning session for the migration'
        assert task.due_date is not None

    @patch('app.services.msx_api.create_task')
    def test_create_task_non_hok(self, mock_create, app, client, db_session, sample_user):
        """Creating a non-HoK task should set is_hok=False."""
        mock_create.return_value = {
            'success': True,
            'task_id': 'msx-nonhok-guid',
            'task_url': 'https://example.com/task/msx-nonhok-guid',
        }

        milestone = Milestone(
            msx_milestone_id='ms-nonhok-task',
            url='https://example.com/ms-nonhok',
            title='NonHoK Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            json={
                'subject': 'Internal Task',
                'task_category': 861980012,  # Internal - non-HoK
                'duration_minutes': 30,
            },
            content_type='application/json',
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

        task = MsxTask.query.filter_by(msx_task_id='msx-nonhok-guid').first()
        assert task is not None
        assert task.is_hok is False
        assert task.task_category_name == 'Internal'

    def test_create_task_missing_subject(self, app, client, db_session, sample_user):
        """Should return error when subject is missing."""
        milestone = Milestone(
            msx_milestone_id='ms-no-subject',
            url='https://example.com/ms-no-subject',
            title='No Subject Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            json={'task_category': 861980004, 'duration_minutes': 60},
            content_type='application/json',
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'title' in data['error'].lower()

    def test_create_task_missing_category(self, app, client, db_session, sample_user):
        """Should return error when category is missing."""
        milestone = Milestone(
            msx_milestone_id='ms-no-cat',
            url='https://example.com/ms-no-cat',
            title='No Category Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            json={'subject': 'My Task', 'duration_minutes': 60},
            content_type='application/json',
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'category' in data['error'].lower()

    def test_create_task_no_msx_id(self, app, client, db_session, sample_user):
        """Should return error when milestone has no MSX ID."""
        milestone = Milestone(
            msx_milestone_id=None,
            url='https://example.com/no-msx',
            title='No MSX Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            json={
                'subject': 'Test Task',
                'task_category': 861980004,
                'duration_minutes': 60,
            },
            content_type='application/json',
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'MSX ID' in data['error']

    def test_create_task_not_json(self, app, client, db_session, sample_user):
        """Should return error when request is not JSON."""
        milestone = Milestone(
            msx_milestone_id='ms-not-json',
            url='https://example.com/ms-not-json',
            title='Not JSON Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            data='not json',
            content_type='text/plain',
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    @patch('app.services.msx_api.create_task')
    def test_create_task_msx_api_failure(self, mock_create, app, client, db_session, sample_user):
        """Should return error when MSX API call fails."""
        mock_create.return_value = {
            'success': False,
            'error': 'MSX API timeout',
        }

        milestone = Milestone(
            msx_milestone_id='ms-api-fail',
            url='https://example.com/ms-api-fail',
            title='API Fail Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            json={
                'subject': 'Will Fail Task',
                'task_category': 861980004,
                'duration_minutes': 60,
            },
            content_type='application/json',
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'MSX API timeout' in data['error']

        # No local record should be created
        task = MsxTask.query.filter_by(subject='Will Fail Task').first()
        assert task is None

    def test_create_task_milestone_not_found(self, app, client, db_session, sample_user):
        """Should return 404 for non-existent milestone."""
        response = client.post(
            '/milestone/99999/tasks',
            json={
                'subject': 'Ghost Task',
                'task_category': 861980004,
                'duration_minutes': 60,
            },
            content_type='application/json',
        )

        assert response.status_code == 404

    @patch('app.services.msx_api.create_task')
    def test_create_task_no_due_date(self, mock_create, app, client, db_session, sample_user):
        """Creating a task without a due date should work."""
        mock_create.return_value = {
            'success': True,
            'task_id': 'msx-no-due-guid',
            'task_url': 'https://example.com/task/msx-no-due-guid',
        }

        milestone = Milestone(
            msx_milestone_id='ms-no-due',
            url='https://example.com/ms-no-due',
            title='No Due Date Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        response = client.post(
            f'/milestone/{milestone.id}/tasks',
            json={
                'subject': 'No Due Date Task',
                'task_category': 861980001,  # Workshop - HoK
                'duration_minutes': 120,
            },
            content_type='application/json',
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

        task = MsxTask.query.filter_by(msx_task_id='msx-no-due-guid').first()
        assert task is not None
        assert task.due_date is None
        assert task.is_hok is True
        assert task.task_category_name == 'Workshop'
