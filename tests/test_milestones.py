"""
Tests for milestone functionality.
"""
import pytest
from datetime import datetime
from app.models import db, Milestone, CallLog


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

    def test_milestone_delete_blocked_when_linked_to_call_log(self, client, app, db_session, sample_user, sample_customer):
        """Test that deleting a milestone linked to a call log is blocked."""
        milestone = Milestone(
            url='https://example.com/linked/test',
            title='Linked Milestone',
        )
        db_session.add(milestone)
        db_session.flush()

        call_log = CallLog(
            customer_id=sample_customer.id,
            call_date=datetime(2026, 2, 25),
            content='<p>Test call</p>',
        )
        call_log.milestones = [milestone]
        db_session.add(call_log)
        db_session.commit()
        milestone_id = milestone.id

        response = client.post(f'/milestone/{milestone_id}/delete', follow_redirects=True)

        assert response.status_code == 200
        assert b'Cannot delete this milestone' in response.data

        # Verify milestone still exists
        still_exists = db.session.get(Milestone, milestone_id)
        assert still_exists is not None


class TestCallLogMilestoneIntegration:
    """Tests for milestone integration with call logs."""
    
    def test_call_log_with_msx_milestone_creates_milestone(self, client, app, db_session, sample_customer):
        """Test that adding MSX milestone to call log creates a new milestone."""
        msx_milestone_id = 'test-msx-id-12345678'
        milestone_url = 'https://msxsalesplatform.dynamics.com/new/milestone'
        
        response = client.post(f'/call-log/new?customer_id={sample_customer.id}', data={
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
            from app.models import Milestone, CallLog
            milestone = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).first()
            assert milestone is not None
            assert milestone.url == milestone_url
            assert milestone.msx_status == 'On Track'
            
            # Verify call log is linked to milestone
            call_log = CallLog.query.filter_by(customer_id=sample_customer.id).first()
            assert call_log is not None
            assert milestone in call_log.milestones
    
    def test_call_log_with_existing_msx_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that adding existing MSX milestone links to existing milestone."""
        msx_milestone_id = 'existing-msx-id-12345'
        
        # Create existing milestone
        with app.app_context():
            from app.models import db, Milestone, User, CallLog
            test_user = User.query.first()
            
            existing_milestone = Milestone(
                msx_milestone_id=msx_milestone_id,
                url='https://msxsalesplatform.dynamics.com/existing/milestone',
                msx_status='On Track',
            )
            db.session.add(existing_milestone)
            db.session.commit()
            existing_id = existing_milestone.id
        
        response = client.post(f'/call-log/new?customer_id={sample_customer.id}', data={
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
            from app.models import Milestone, CallLog
            milestones = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).all()
            assert len(milestones) == 1
            
            # Milestone should be updated with new status
            milestone = milestones[0]
            assert milestone.msx_status == 'Blocked'
            
            # Call log should be linked to existing milestone
            call_log = CallLog.query.filter_by(customer_id=sample_customer.id).first()
            assert len([m for m in call_log.milestones if m.id == existing_id]) == 1
    
    def test_call_log_view_shows_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that call log view shows associated milestone."""
        from datetime import date
        
        with app.app_context():
            from app.models import db, Milestone, CallLog, User
            test_user = User.query.first()
            
            milestone = Milestone(
                url='https://example.com/show/milestone',
                title='Visible Milestone',
            )
            db.session.add(milestone)
            
            call_log = CallLog(
                customer_id=sample_customer.id,
                call_date=date(2026, 1, 30),
                content='<p>Test content</p>',
            )
            call_log.milestones.append(milestone)
            db.session.add(call_log)
            db.session.commit()
            call_log_id = call_log.id
        
        response = client.get(f'/call-log/{call_log_id}')
        assert response.status_code == 200
        assert b'Visible Milestone' in response.data
    
    def test_call_log_edit_updates_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that editing call log can change milestone."""
        from datetime import date
        
        with app.app_context():
            from app.models import db, Milestone, CallLog, User
            test_user = User.query.first()
            
            # Create call log with initial milestone
            old_milestone = Milestone(
                msx_milestone_id='old-msx-id-12345',
                url='https://example.com/old/milestone',
            )
            db.session.add(old_milestone)
            
            call_log = CallLog(
                customer_id=sample_customer.id,
                call_date=date(2026, 1, 30),
                content='<p>Original content</p>',
            )
            call_log.milestones.append(old_milestone)
            db.session.add(call_log)
            db.session.commit()
            call_log_id = call_log.id
        
        # Edit with new MSX milestone
        response = client.post(f'/call-log/{call_log_id}/edit', data={
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
            from app.models import db, CallLog
            call_log = db.session.get(CallLog, call_log_id)
            assert len(call_log.milestones) == 1
            assert call_log.milestones[0].msx_milestone_id == 'new-msx-id-67890'
            assert call_log.milestones[0].url == 'https://example.com/new/milestone'


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
