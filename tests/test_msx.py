"""
Tests for MSX integration functionality.

Tests the MSX API client, milestone picker, and task creation features.
Note: These tests mock the actual MSX API calls to avoid external dependencies.
"""
import pytest
from unittest.mock import patch, MagicMock
from app.models import db, Milestone, MsxTask, CallLog, Customer
from app.services.msx_api import (
    extract_account_id_from_url,
    build_milestone_url,
    build_task_url,
    TASK_CATEGORIES,
    HOK_TASK_CATEGORIES,
    MILESTONE_STATUS_ORDER,
)


class TestMsxApiHelpers:
    """Tests for MSX API helper functions."""
    
    def test_extract_account_id_from_url_valid(self):
        """Test extracting account ID from a valid MSX URL."""
        url = 'https://microsoftsales.crm.dynamics.com/main.aspx?appid=abc&pagetype=entityrecord&etn=account&id=12345678-1234-1234-1234-123456789abc'
        result = extract_account_id_from_url(url)
        assert result == '12345678-1234-1234-1234-123456789abc'
    
    def test_extract_account_id_from_url_no_id(self):
        """Test extracting account ID when URL has no id parameter."""
        url = 'https://microsoftsales.crm.dynamics.com/main.aspx?appid=abc&pagetype=entityrecord'
        result = extract_account_id_from_url(url)
        assert result is None
    
    def test_extract_account_id_from_url_empty(self):
        """Test extracting account ID from empty string."""
        result = extract_account_id_from_url('')
        assert result is None
    
    def test_extract_account_id_from_url_none(self):
        """Test extracting account ID from None."""
        result = extract_account_id_from_url(None)
        assert result is None
    
    def test_build_milestone_url(self):
        """Test building MSX milestone URL from ID."""
        milestone_id = 'abc123-def456'
        url = build_milestone_url(milestone_id)
        assert 'msp_engagementmilestone' in url
        assert milestone_id in url
    
    def test_build_task_url(self):
        """Test building MSX task URL from ID."""
        task_id = 'task-789'
        url = build_task_url(task_id)
        assert 'task' in url.lower()
        assert task_id in url


class TestTaskCategories:
    """Tests for task category constants."""
    
    def test_task_categories_not_empty(self):
        """Test that task categories list is populated."""
        assert len(TASK_CATEGORIES) > 0
    
    def test_task_categories_have_required_fields(self):
        """Test that each category has required fields."""
        for cat in TASK_CATEGORIES:
            assert 'value' in cat
            assert 'label' in cat
            assert 'is_hok' in cat
            assert isinstance(cat['value'], int)
            assert isinstance(cat['label'], str)
            assert isinstance(cat['is_hok'], bool)
    
    def test_hok_categories_exist(self):
        """Test that HOK categories are defined."""
        assert len(HOK_TASK_CATEGORIES) > 0
        
        # Verify HOK categories are in the main list
        hok_codes = [c['value'] for c in TASK_CATEGORIES if c['is_hok']]
        assert len(hok_codes) == len(HOK_TASK_CATEGORIES)
    
    def test_milestone_status_order(self):
        """Test that milestone statuses have sort order defined."""
        # Active statuses should sort before completed/cancelled
        assert MILESTONE_STATUS_ORDER.get('On Track', 99) < MILESTONE_STATUS_ORDER.get('Completed', 99)
        assert MILESTONE_STATUS_ORDER.get('Blocked', 99) < MILESTONE_STATUS_ORDER.get('Cancelled', 99)


class TestMsxTaskModel:
    """Tests for MsxTask model."""
    
    def test_msx_task_creation(self, app, db_session, sample_user, sample_customer):
        """Test creating an MsxTask."""
        with app.app_context():
            from app.models import User, Customer
            user = User.query.first()
            customer = Customer.query.first()
            
            # Create milestone first
            milestone = Milestone(
                msx_milestone_id='milestone-123',
                url='https://example.com/milestone/123',
                msx_status='On Track',
                user_id=user.id,
                customer_id=customer.id
            )
            db.session.add(milestone)
            
            # Create call log
            from datetime import date
            call_log = CallLog(
                customer_id=customer.id,
                call_date=date.today(),
                content='<p>Test content</p>',
                user_id=user.id
            )
            db.session.add(call_log)
            db.session.commit()
            
            # Create task
            task = MsxTask(
                msx_task_id='task-456',
                msx_task_url='https://example.com/task/456',
                subject='Test Task',
                description='Test description',
                task_category=861980004,
                task_category_name='Azure Workshop',
                duration_minutes=60,
                is_hok=True,
                call_log_id=call_log.id,
                milestone_id=milestone.id
            )
            db.session.add(task)
            db.session.commit()
            
            assert task.id is not None
            assert task.msx_task_id == 'task-456'
            assert task.is_hok is True
            assert task.milestone == milestone
            assert task.call_log == call_log
    
    def test_msx_task_relationships(self, app, db_session, sample_user, sample_customer):
        """Test MsxTask relationships to milestone and call_log."""
        with app.app_context():
            from app.models import User, Customer
            from datetime import date
            
            user = User.query.first()
            customer = Customer.query.first()
            
            milestone = Milestone(
                msx_milestone_id='rel-milestone',
                url='https://example.com/milestone',
                user_id=user.id
            )
            db.session.add(milestone)
            
            call_log = CallLog(
                customer_id=customer.id,
                call_date=date.today(),
                content='<p>Content</p>',
                user_id=user.id
            )
            call_log.milestones.append(milestone)
            db.session.add(call_log)
            db.session.commit()
            
            task = MsxTask(
                msx_task_id='rel-task',
                msx_task_url='https://example.com/task',
                subject='Relationship Test',
                task_category=1,
                task_category_name='Test',
                call_log_id=call_log.id,
                milestone_id=milestone.id
            )
            db.session.add(task)
            db.session.commit()
            
            # Test relationships work both ways
            assert task in milestone.tasks
            assert task in call_log.msx_tasks


class TestMsxRoutes:
    """Tests for MSX API routes."""
    
    def test_task_categories_endpoint(self, client, app):
        """Test GET /api/msx/task-categories returns categories."""
        response = client.get('/api/msx/task-categories')
        assert response.status_code == 200
        
        data = response.get_json()
        assert data['success'] is True
        assert 'categories' in data
        assert len(data['categories']) > 0
    
    def test_task_categories_have_hok_flags(self, client, app):
        """Test that task categories include HOK flags."""
        response = client.get('/api/msx/task-categories')
        data = response.get_json()
        
        hok_categories = [c for c in data['categories'] if c['is_hok']]
        non_hok_categories = [c for c in data['categories'] if not c['is_hok']]
        
        assert len(hok_categories) > 0
        assert len(non_hok_categories) > 0
    
    def test_milestones_for_customer_no_tpid(self, client, app, db_session, sample_customer):
        """Test milestones endpoint when customer has no TPID URL."""
        # Ensure customer has no tpid_url
        with app.app_context():
            customer = db.session.get(Customer, sample_customer.id)
            customer.tpid_url = None
            db.session.commit()
        
        response = client.get(f'/api/msx/milestones-for-customer/{sample_customer.id}')
        assert response.status_code == 200
        
        data = response.get_json()
        assert data['success'] is False
        assert data['needs_tpid'] is True
    
    def test_milestones_for_customer_not_found(self, client, app):
        """Test milestones endpoint with invalid customer ID."""
        response = client.get('/api/msx/milestones-for-customer/99999')
        assert response.status_code == 200
        
        data = response.get_json()
        assert data['success'] is False
        assert 'not found' in data['error'].lower()
    
    @patch('app.routes.msx.get_milestones_by_account')
    def test_milestones_for_customer_success(self, mock_get_milestones, client, app, db_session, sample_customer):
        """Test milestones endpoint returns MSX milestones."""
        # Set up customer with valid TPID URL (needs proper GUID format)
        with app.app_context():
            customer = db.session.get(Customer, sample_customer.id)
            customer.tpid_url = 'https://microsoftsales.crm.dynamics.com/main.aspx?id=12345678-1234-1234-1234-123456789abc'
            db.session.commit()
        
        # Mock the MSX API response
        mock_get_milestones.return_value = {
            'success': True,
            'milestones': [
                {
                    'msp_engagementmilestoneid': 'ms-1',
                    'msp_name': 'Test Milestone',
                    'msp_status': 'On Track',
                    'msp_milestonenumber': 'MS-001'
                }
            ]
        }
        
        response = client.get(f'/api/msx/milestones-for-customer/{sample_customer.id}')
        assert response.status_code == 200
        
        data = response.get_json()
        assert data['success'] is True
        assert len(data['milestones']) == 1
        assert data['milestones'][0]['msp_name'] == 'Test Milestone'
    
    @patch('app.routes.msx.get_my_milestone_team_ids')
    @patch('app.routes.msx.get_milestones_by_account')
    def test_milestones_for_customer_on_my_team_from_msx(
        self, mock_get_milestones, mock_get_team_ids, client, app, db_session, sample_customer
    ):
        """Test milestone dropdown shows on_my_team from live MSX team membership."""
        with app.app_context():
            customer = db.session.get(Customer, sample_customer.id)
            customer.tpid_url = 'https://microsoftsales.crm.dynamics.com/main.aspx?id=12345678-1234-1234-1234-123456789abc'
            db.session.commit()
        
        ms_id_on_team = 'aaaa1111-2222-3333-4444-555566667777'
        ms_id_not_on_team = 'bbbb1111-2222-3333-4444-555566667777'
        
        mock_get_milestones.return_value = {
            'success': True,
            'milestones': [
                {'id': ms_id_on_team, 'name': 'Fabric F64 SKU', 'status': 'On Track'},
                {'id': ms_id_not_on_team, 'name': 'Other Milestone', 'status': 'Completed'},
            ]
        }
        mock_get_team_ids.return_value = {
            'success': True,
            'milestone_ids': {ms_id_on_team},
        }
        
        response = client.get(f'/api/msx/milestones-for-customer/{sample_customer.id}')
        data = response.get_json()
        
        assert data['success'] is True
        assert len(data['milestones']) == 2
        assert data['milestones'][0]['on_my_team'] is True
        assert data['milestones'][1]['on_my_team'] is False
    
    @patch('app.routes.msx.get_my_milestone_team_ids')
    @patch('app.routes.msx.get_milestones_by_account')
    def test_milestones_for_customer_team_check_failure_falls_back(
        self, mock_get_milestones, mock_get_team_ids, client, app, db_session, sample_customer
    ):
        """Test that if MSX team check fails, it falls back to local DB record."""
        with app.app_context():
            customer = db.session.get(Customer, sample_customer.id)
            customer.tpid_url = 'https://microsoftsales.crm.dynamics.com/main.aspx?id=12345678-1234-1234-1234-123456789abc'
            db.session.commit()
        
        mock_get_milestones.return_value = {
            'success': True,
            'milestones': [
                {'id': 'ms-no-local-record', 'name': 'New Milestone', 'status': 'On Track'},
            ]
        }
        # Simulate team check failure
        mock_get_team_ids.return_value = {
            'success': False,
            'error': 'Request timed out',
            'milestone_ids': set(),
        }
        
        response = client.get(f'/api/msx/milestones-for-customer/{sample_customer.id}')
        data = response.get_json()
        
        assert data['success'] is True
        # No local record + team check failed = on_my_team should be False
        assert data['milestones'][0]['on_my_team'] is False

    def test_create_task_requires_json(self, client, app):
        """Test POST /api/msx/tasks requires JSON body."""
        response = client.post('/api/msx/tasks', data='not json')
        assert response.status_code == 400
    
    def test_create_task_requires_fields(self, client, app):
        """Test POST /api/msx/tasks validates required fields."""
        response = client.post('/api/msx/tasks', 
            json={},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'milestone_id' in data['error'] or 'required' in data['error']
    
    @patch('app.routes.msx.create_task')
    def test_create_task_success(self, mock_create_task, client, app):
        """Test successful task creation."""
        mock_create_task.return_value = {
            'success': True,
            'task_id': 'new-task-123',
            'task_url': 'https://example.com/task/new-task-123'
        }
        
        response = client.post('/api/msx/tasks',
            json={
                'milestone_id': 'milestone-abc',
                'subject': 'Test Task',
                'task_category': 861980004,
                'duration_minutes': 60,
                'description': 'Test description'
            },
            content_type='application/json'
        )
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['task_id'] == 'new-task-123'
        assert 'task_url' in data


class TestMilestoneStatusSorting:
    """Tests for milestone status sort order."""
    
    def test_status_sort_order_property(self, app, db_session, sample_user):
        """Test Milestone.status_sort_order property."""
        with app.app_context():
            from app.models import User
            user = User.query.first()
            
            milestone = Milestone(
                msx_milestone_id='sort-test',
                url='https://example.com/milestone',
                msx_status='On Track',
                user_id=user.id
            )
            db.session.add(milestone)
            db.session.commit()
            
            # On Track should have low sort order (appears first)
            assert milestone.status_sort_order < 10
    
    def test_status_sort_order_unknown(self, app, db_session, sample_user):
        """Test sort order for unknown status."""
        with app.app_context():
            from app.models import User
            user = User.query.first()
            
            milestone = Milestone(
                msx_milestone_id='unknown-status',
                url='https://example.com/milestone',
                msx_status='Unknown Status',
                user_id=user.id
            )
            db.session.add(milestone)
            db.session.commit()
            
            # Unknown status should sort last
            assert milestone.status_sort_order >= 99


class TestCallLogMilestoneTaskFlow:
    """Tests for the full call log -> milestone -> task flow."""
    
    def test_call_log_save_with_milestone_and_task_fields(self, client, app, db_session, sample_customer):
        """Test saving call log with milestone and task creation fields."""
        msx_milestone_id = 'flow-test-milestone'
        
        # Note: Task creation won't succeed without MSX auth, 
        # but the milestone should still be linked
        response = client.post(f'/call-log/new?customer_id={sample_customer.id}', data={
            'customer_id': sample_customer.id,
            'call_date': '2026-02-19',
            'content': '<p>Test call with milestone and task</p>',
            'milestone_msx_id': msx_milestone_id,
            'milestone_url': 'https://example.com/milestone',
            'milestone_name': 'Flow Test Milestone',
            'milestone_status': 'On Track',
            'milestone_status_code': '1',
            'task_subject': 'Flow Test Task',
            'task_category': '861980004',
            'task_duration': '60',
            'task_description': 'Test task description'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify milestone was created and linked
        with app.app_context():
            milestone = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).first()
            assert milestone is not None
            assert milestone.msx_status == 'On Track'
            
            call_log = CallLog.query.filter_by(customer_id=sample_customer.id).first()
            assert call_log is not None
            assert milestone in call_log.milestones
