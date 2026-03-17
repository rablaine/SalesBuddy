"""
Tests for milestone functionality.
"""
import json
import pytest
from datetime import datetime
from unittest.mock import patch
from app.models import db, Milestone, Note


class TestMilestoneModel:
    """Tests for Milestone model."""
    
    def test_milestone_creation(self, app, db_session, sample_user):
        """Test creating a milestone."""
        milestone = Milestone(
            url='https://msxsalesplatform.dynamics.com/milestone/123',
            title='Q2 Deployment',
        )
        db_session.add(milestone)
        db_session.commit()
        
        assert milestone.id is not None
        assert milestone.url == 'https://msxsalesplatform.dynamics.com/milestone/123'
        assert milestone.title == 'Q2 Deployment'
        assert milestone.display_text == 'Q2 Deployment'
    
    def test_milestone_display_text_without_title(self, app, db_session, sample_user):
        """Test display_text property when title is None."""
        milestone = Milestone(
            url='https://msxsalesplatform.dynamics.com/milestone/456',
            title=None,
        )
        db_session.add(milestone)
        db_session.commit()
        
        assert milestone.display_text == 'View in MSX'
    
    def test_milestone_unique_msx_id(self, app, db_session, sample_user):
        """Test that MSX milestone IDs must be unique."""
        milestone1 = Milestone(
            msx_milestone_id='12345678-1234-1234-1234-123456789abc',
            url='https://msxsalesplatform.dynamics.com/milestone/unique',
        )
        db_session.add(milestone1)
        db_session.commit()
        
        milestone2 = Milestone(
            msx_milestone_id='12345678-1234-1234-1234-123456789abc',
            url='https://msxsalesplatform.dynamics.com/milestone/unique2',
        )
        db_session.add(milestone2)
        
        with pytest.raises(Exception):  # SQLAlchemy IntegrityError
            db_session.commit()


class TestMilestoneCRUD:
    """Tests for milestone CRUD operations."""
    
    def test_milestones_list_empty(self, client, app):
        """Test milestones list page with no milestones."""
        response = client.get('/milestones')
        assert response.status_code == 200
        assert b'No milestones yet' in response.data
    
    def test_milestones_list_with_data(self, client, app, db_session, sample_user):
        """Test milestones list page with milestones."""
        milestone = Milestone(
            url='https://example.com/milestone/1',
            title='Test Milestone',
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.get('/milestones')
        assert response.status_code == 200
        assert b'Test Milestone' in response.data
    
    def test_milestone_create_form(self, client, app):
        """Test milestone create form loads."""
        response = client.get('/milestone/new')
        assert response.status_code == 200
        assert b'New Milestone' in response.data
        assert b'MSX URL' in response.data
    
    def test_milestone_create_post(self, client, app, db_session):
        """Test creating a milestone via POST."""
        response = client.post('/milestone/new', data={
            'url': 'https://msxsalesplatform.dynamics.com/test/123',
            'title': 'New Test Milestone'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Milestone created successfully' in response.data
        
        # Verify in database
        milestone = Milestone.query.filter_by(url='https://msxsalesplatform.dynamics.com/test/123').first()
        assert milestone is not None
        assert milestone.title == 'New Test Milestone'
    
    def test_milestone_create_requires_url(self, client, app):
        """Test that URL is required."""
        response = client.post('/milestone/new', data={
            'url': '',
            'title': 'No URL Milestone'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'URL is required' in response.data
    
    def test_milestone_view(self, client, app, db_session, sample_user):
        """Test viewing a milestone."""
        milestone = Milestone(
            url='https://example.com/view/test',
            title='View Test',
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.get(f'/milestone/{milestone.id}')
        assert response.status_code == 200
        assert b'View Test' in response.data
        assert b'https://example.com/view/test' in response.data
    
    def test_milestone_edit_form(self, client, app, db_session, sample_user):
        """Test milestone edit form loads."""
        milestone = Milestone(
            url='https://example.com/edit/test',
            title='Edit Test',
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.get(f'/milestone/{milestone.id}/edit')
        assert response.status_code == 200
        assert b'Edit Milestone' in response.data
        assert b'Edit Test' in response.data
    
    def test_milestone_edit_post(self, client, app, db_session, sample_user):
        """Test editing a milestone via POST."""
        milestone = Milestone(
            url='https://example.com/original',
            title='Original Title',
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.post(f'/milestone/{milestone.id}/edit', data={
            'url': 'https://example.com/updated',
            'title': 'Updated Title'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Milestone updated successfully' in response.data
        
        # Verify in database
        db_session.refresh(milestone)
        assert milestone.url == 'https://example.com/updated'
        assert milestone.title == 'Updated Title'
    
    def test_milestone_delete(self, client, app, db_session, sample_user):
        """Test deleting a milestone."""
        milestone = Milestone(
            url='https://example.com/delete/test',
            title='Delete Me',
        )
        db_session.add(milestone)
        db_session.commit()
        milestone_id = milestone.id
        
        response = client.post(f'/milestone/{milestone_id}/delete', follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Milestone deleted successfully' in response.data
        
        # Verify deletion
        deleted = db.session.get(Milestone, milestone_id)
        assert deleted is None

    def test_milestone_delete_blocked_when_linked_to_note(self, client, app, db_session, sample_user, sample_customer):
        """Test that deleting a milestone linked to a call log is blocked."""
        milestone = Milestone(
            url='https://example.com/linked/test',
            title='Linked Milestone',
        )
        db_session.add(milestone)
        db_session.flush()

        note = Note(
            customer_id=sample_customer.id,
            call_date=datetime(2026, 2, 25),
            content='<p>Test call</p>',
        )
        note.milestones = [milestone]
        db_session.add(note)
        db_session.commit()
        milestone_id = milestone.id

        response = client.post(f'/milestone/{milestone_id}/delete', follow_redirects=True)

        assert response.status_code == 200
        assert b'Cannot delete this milestone' in response.data

        # Verify milestone still exists
        still_exists = db.session.get(Milestone, milestone_id)
        assert still_exists is not None


class TestNoteMilestoneIntegration:
    """Tests for milestone integration with call logs."""
    
    def test_note_with_msx_milestone_creates_milestone(self, client, app, db_session, sample_customer):
        """Test that adding MSX milestone to call log creates a new milestone."""
        msx_milestone_id = 'test-msx-id-12345678'
        milestone_url = 'https://msxsalesplatform.dynamics.com/new/milestone'
        
        response = client.post(f'/note/new?customer_id={sample_customer.id}', data={
            'customer_id': sample_customer.id,
            'call_date': '2026-01-30',
            'content': '<p>Test call log with milestone</p>',
            'milestone_msx_id': msx_milestone_id,
            'milestone_url': milestone_url,
            'milestone_name': 'Test Milestone',
            'milestone_number': 'MS-001',
            'milestone_status': 'On Track',
            'milestone_status_code': '1',
            'milestone_opportunity_name': 'Test Opportunity'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify milestone was created
        with app.app_context():
            from app.models import Milestone, Note
            milestone = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).first()
            assert milestone is not None
            assert milestone.url == milestone_url
            assert milestone.msx_status == 'On Track'
            
            # Verify call log is linked to milestone
            note = Note.query.filter_by(customer_id=sample_customer.id).first()
            assert note is not None
            assert milestone in note.milestones
    
    def test_note_with_existing_msx_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that adding existing MSX milestone links to existing milestone."""
        msx_milestone_id = 'existing-msx-id-12345'
        
        # Create existing milestone
        with app.app_context():
            from app.models import db, Milestone, User, Note
            test_user = User.query.first()
            
            existing_milestone = Milestone(
                msx_milestone_id=msx_milestone_id,
                url='https://msxsalesplatform.dynamics.com/existing/milestone',
                msx_status='On Track',
            )
            db.session.add(existing_milestone)
            db.session.commit()
            existing_id = existing_milestone.id
        
        response = client.post(f'/note/new?customer_id={sample_customer.id}', data={
            'customer_id': sample_customer.id,
            'call_date': '2026-01-30',
            'content': '<p>Test call log linking to existing milestone</p>',
            'milestone_msx_id': msx_milestone_id,
            'milestone_url': 'https://msxsalesplatform.dynamics.com/existing/milestone',
            'milestone_name': 'Existing Milestone',
            'milestone_status': 'Blocked',
            'milestone_status_code': '3'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Should not create duplicate milestone
        with app.app_context():
            from app.models import Milestone, Note
            milestones = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).all()
            assert len(milestones) == 1
            
            # Milestone should be updated with new status
            milestone = milestones[0]
            assert milestone.msx_status == 'Blocked'
            
            # Call log should be linked to existing milestone
            note = Note.query.filter_by(customer_id=sample_customer.id).first()
            assert len([m for m in note.milestones if m.id == existing_id]) == 1
    
    def test_note_view_shows_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that call log view shows associated milestone."""
        from datetime import date
        
        with app.app_context():
            from app.models import db, Milestone, Note, User
            test_user = User.query.first()
            
            milestone = Milestone(
                url='https://example.com/show/milestone',
                title='Visible Milestone',
            )
            db.session.add(milestone)
            
            note = Note(
                customer_id=sample_customer.id,
                call_date=date(2026, 1, 30),
                content='<p>Test content</p>',
            )
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.commit()
            note_id = note.id
        
        response = client.get(f'/note/{note_id}')
        assert response.status_code == 200
        assert b'Visible Milestone' in response.data
    
    def test_note_edit_updates_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that editing call log can change milestone."""
        from datetime import date
        
        with app.app_context():
            from app.models import db, Milestone, Note, User
            test_user = User.query.first()
            
            # Create call log with initial milestone
            old_milestone = Milestone(
                msx_milestone_id='old-msx-id-12345',
                url='https://example.com/old/milestone',
            )
            db.session.add(old_milestone)
            
            note = Note(
                customer_id=sample_customer.id,
                call_date=date(2026, 1, 30),
                content='<p>Original content</p>',
            )
            note.milestones.append(old_milestone)
            db.session.add(note)
            db.session.commit()
            note_id = note.id
        
        # Edit with new MSX milestone
        response = client.post(f'/note/{note_id}/edit', data={
            'customer_id': sample_customer.id,
            'call_date': '2026-01-30',
            'content': '<p>Updated content</p>',
            'milestone_msx_id': 'new-msx-id-67890',
            'milestone_url': 'https://example.com/new/milestone',
            'milestone_name': 'New Milestone',
            'milestone_status': 'On Track'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify new milestone was created and linked
        with app.app_context():
            from app.models import db, Note
            note = db.session.get(Note, note_id)
            assert len(note.milestones) == 1
            assert note.milestones[0].msx_milestone_id == 'new-msx-id-67890'
            assert note.milestones[0].url == 'https://example.com/new/milestone'


class TestMilestoneAPI:
    """Tests for milestone API endpoints."""
    
    def test_find_or_create_milestone_creates_new(self, client, app, db_session):
        """Test API creates new milestone when URL doesn't exist."""
        response = client.post('/api/milestones/find-or-create',
            json={'url': 'https://api.test/new/milestone'},
            content_type='application/json'
        )
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['url'] == 'https://api.test/new/milestone'
        assert data['id'] is not None
    
    def test_find_or_create_milestone_finds_existing(self, client, app, db_session, sample_user):
        """Test API finds existing milestone."""
        with app.app_context():
            from app.models import db, Milestone, User
            test_user = User.query.first()
            
            existing = Milestone(
                url='https://api.test/existing',
                title='Existing',
            )
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id
        
        response = client.post('/api/milestones/find-or-create',
            json={'url': 'https://api.test/existing'},
            content_type='application/json'
        )
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['id'] == existing_id
        assert data['title'] == 'Existing'
    
    def test_find_or_create_milestone_requires_url(self, client, app):
        """Test API returns error when URL is missing."""
        response = client.post('/api/milestones/find-or-create',
            json={'url': ''},
            content_type='application/json'
        )
        
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data


class TestMilestoneViewOverhaul:
    """Tests for the overhauled milestone view page."""

    def _create_milestone(self, app, **kwargs):
        """Helper to create a milestone with defaults."""
        with app.app_context():
            defaults = {
                'url': 'https://msxsalesplatform.dynamics.com/milestone/test',
                'title': 'Test Milestone',
                'msx_milestone_id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                'msx_status': 'On Track',
                'dollar_value': 50000,
                'monthly_usage': 5000,
                'workload': 'Azure Synapse',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    def test_view_shows_engagements_in_nav(self, client, app, db_session, sample_user, sample_customer):
        """Engagements linked to the milestone appear in the Navigation card."""
        from app.models import Engagement
        with app.app_context():
            ms = Milestone(
                url='https://test/ms', title='Nav Test',
                msx_milestone_id='nav-test-1234',
                customer_id=sample_customer.id,
            )
            db.session.add(ms)
            db.session.flush()
            eng = Engagement(
                customer_id=sample_customer.id,
                title='Fabric Migration',
                status='Active',
            )
            db.session.add(eng)
            db.session.flush()
            eng.milestones.append(ms)
            db.session.commit()
            ms_id = ms.id

        resp = client.get(f'/milestone/{ms_id}')
        assert resp.status_code == 200
        assert b'Fabric Migration' in resp.data
        assert b'bi-lightning-charge' in resp.data

    def test_view_shows_cached_comments(self, client, app, db_session, sample_user):
        """Cached comments render immediately in the template."""
        comments = [
            {"userId": "abc", "displayName": "Jane Doe",
             "modifiedOn": "2026-03-17T10:00:00Z",
             "comment": "Looking good on track"},
        ]
        ms_id = self._create_milestone(
            app, cached_comments_json=json.dumps(comments),
        )
        resp = client.get(f'/milestone/{ms_id}')
        assert resp.status_code == 200
        assert b'Jane Doe' in resp.data
        assert b'Looking good on track' in resp.data

    def test_view_no_msx_id_hides_spinners(self, client, app, db_session, sample_user):
        """Milestone without MSX ID should not show loading spinners."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.get(f'/milestone/{ms_id}')
        assert resp.status_code == 200
        assert b'detailsSpinner' not in resp.data
        assert b'commentsSpinner' not in resp.data

    def test_tasks_in_left_column(self, client, app, db_session, sample_user):
        """Tasks card should be in the left column (col-md-4)."""
        ms_id = self._create_milestone(app)
        resp = client.get(f'/milestone/{ms_id}')
        html = resp.data.decode()
        # Tasks header should appear before the right column starts
        left_col_start = html.index('col-md-4')
        right_col_start = html.index('col-md-8')
        tasks_pos = html.index('Tasks</h5>')
        assert left_col_start < tasks_pos < right_col_start

    def test_notes_between_details_and_tasks(self, client, app, db_session, sample_user):
        """Associated Notes card appears between Details and Tasks in left column."""
        ms_id = self._create_milestone(app)
        resp = client.get(f'/milestone/{ms_id}')
        html = resp.data.decode()
        # Use card header text to find positions
        details_pos = html.index('Details</h5>')
        notes_pos = html.index('Associated Notes</h5>')
        tasks_pos = html.index('Tasks</h5>')
        assert details_pos < notes_pos < tasks_pos


class TestMilestoneMsxDetailsAPI:
    """Tests for the lazy-load MSX details endpoint."""

    def _create_milestone(self, app, **kwargs):
        with app.app_context():
            defaults = {
                'url': 'https://test/ms',
                'title': 'API Test',
                'msx_milestone_id': 'aaaaaaaa-1111-2222-3333-444444444444',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    @patch('app.services.msx_api.get_milestone_details')
    def test_msx_details_success_caches_data(self, mock_get, client, app, db_session, sample_user):
        """Successful MSX fetch caches comments and details_fetched_at."""
        ms_id = self._create_milestone(app)
        mock_get.return_value = {
            'success': True,
            'milestone': {
                'title': 'Updated Title',
                'milestone_number': '7-999',
                'msx_status': 'At Risk',
                'msx_status_code': 861980001,
                'due_date': '2026-06-30T00:00:00Z',
                'dollar_value': 75000,
                'monthly_usage': 8000,
                'workload': 'Azure Data Factory',
                'comments': [
                    {'userId': 'u1', 'displayName': 'Bob',
                     'modifiedOn': '2026-03-17', 'comment': 'Test comment'},
                ],
            },
        }
        resp = client.get(f'/api/milestone/{ms_id}/msx-details')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['milestone']['msx_status'] == 'At Risk'

        with app.app_context():
            ms = db.session.get(Milestone, ms_id)
            assert ms.title == 'Updated Title'
            assert ms.msx_status == 'At Risk'
            assert ms.cached_comments_json is not None
            assert ms.details_fetched_at is not None

    @patch('app.services.msx_api.get_milestone_details')
    def test_msx_details_failure_returns_error(self, mock_get, client, app, db_session, sample_user):
        """Failed MSX fetch returns error JSON."""
        ms_id = self._create_milestone(app)
        mock_get.return_value = {
            'success': False,
            'error': 'Connection timeout',
            'vpn_blocked': False,
        }
        resp = client.get(f'/api/milestone/{ms_id}/msx-details')
        data = resp.get_json()
        assert data['success'] is False
        assert 'Connection timeout' in data['error']

    def test_msx_details_no_msx_id(self, client, app, db_session, sample_user):
        """Milestone without MSX ID returns error."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.get(f'/api/milestone/{ms_id}/msx-details')
        data = resp.get_json()
        assert data['success'] is False
        assert 'No MSX ID' in data['error']


class TestMilestoneCommentAPI:
    """Tests for the milestone comment endpoints."""

    def _create_milestone(self, app, **kwargs):
        with app.app_context():
            defaults = {
                'url': 'https://test/ms',
                'title': 'Comment Test',
                'msx_milestone_id': 'cccccccc-1111-2222-3333-444444444444',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    @patch('app.services.msx_api.add_milestone_comment')
    def test_post_comment_form(self, mock_add, client, app, db_session, sample_user):
        """Form-based comment post redirects on success."""
        ms_id = self._create_milestone(app)
        mock_add.return_value = {'success': True, 'comment_count': 3}
        resp = client.post(
            f'/milestone/{ms_id}/comment',
            data={'comment': 'Great progress'},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        mock_add.assert_called_once()

    @patch('app.services.msx_api.add_milestone_comment')
    def test_post_comment_api(self, mock_add, client, app, db_session, sample_user):
        """JSON API comment post returns success."""
        ms_id = self._create_milestone(app)
        mock_add.return_value = {'success': True, 'comment_count': 5}
        resp = client.post(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': 'Looking great'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_post_comment_api_empty(self, client, app, db_session, sample_user):
        """Empty comment returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.post(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': '  '},
        )
        assert resp.status_code == 400

    def test_post_comment_no_msx_id(self, client, app, db_session, sample_user):
        """Milestone without MSX ID returns error for API comment."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.post(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': 'test'},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'No MSX ID' in data['error']


class TestMilestoneCommentEditDelete:
    """Tests for editing and deleting milestone comments."""

    def _create_milestone(self, app, **kwargs):
        with app.app_context():
            defaults = {
                'url': 'https://test/ms-ed',
                'title': 'Edit Delete Test',
                'msx_milestone_id': 'dddddddd-1111-2222-3333-444444444444',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    @patch('app.services.msx_api.edit_milestone_comment')
    def test_edit_comment_success(self, mock_edit, client, app, db_session, sample_user):
        """PUT comment returns success when MSX edit succeeds."""
        ms_id = self._create_milestone(app)
        mock_edit.return_value = {'success': True}
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={
                'modifiedOn': '2026-03-01T10:00:00.000Z',
                'userId': 'Alex via Sales Buddy',
                'comment': 'Updated text',
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        mock_edit.assert_called_once()

    def test_edit_comment_empty(self, client, app, db_session, sample_user):
        """PUT with empty comment returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x', 'userId': 'y', 'comment': '  '},
        )
        assert resp.status_code == 400

    def test_edit_comment_missing_id(self, client, app, db_session, sample_user):
        """PUT without modifiedOn or userId returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': 'hello'},
        )
        assert resp.status_code == 400
        assert 'Missing comment identifier' in resp.get_json()['error']

    def test_edit_comment_no_msx_id(self, client, app, db_session, sample_user):
        """PUT on milestone without MSX ID returns 400."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x', 'userId': 'y', 'comment': 'hello'},
        )
        assert resp.status_code == 400
        assert 'No MSX ID' in resp.get_json()['error']

    @patch('app.services.msx_api.delete_milestone_comment')
    def test_delete_comment_success(self, mock_delete, client, app, db_session, sample_user):
        """DELETE comment returns success when MSX delete succeeds."""
        ms_id = self._create_milestone(app)
        mock_delete.return_value = {'success': True}
        resp = client.delete(
            f'/api/milestone/{ms_id}/comment',
            json={
                'modifiedOn': '2026-03-01T10:00:00.000Z',
                'userId': 'Alex via Sales Buddy',
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        mock_delete.assert_called_once()

    def test_delete_comment_missing_id(self, client, app, db_session, sample_user):
        """DELETE without modifiedOn or userId returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.delete(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x'},
        )
        assert resp.status_code == 400
        assert 'Missing comment identifier' in resp.get_json()['error']

    def test_delete_comment_no_msx_id(self, client, app, db_session, sample_user):
        """DELETE on milestone without MSX ID returns 400."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.delete(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x', 'userId': 'y'},
        )
        assert resp.status_code == 400
        assert 'No MSX ID' in resp.get_json()['error']
