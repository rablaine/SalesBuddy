"""
Tests for the Milestone Tracker feature.

Tests the sync service, tracker routes, model additions, and template rendering.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


class TestMilestoneModel:
    """Test the new Milestone model properties."""
    
    def test_is_active_on_track(self, app, sample_data):
        """Milestones with 'On Track' status should be active."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms1',
                msx_status='On Track',
            )
            assert ms.is_active is True
    
    def test_is_active_at_risk(self, app, sample_data):
        """Milestones with 'At Risk' status should be active."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms2',
                msx_status='At Risk',
            )
            assert ms.is_active is True
    
    def test_is_active_blocked(self, app, sample_data):
        """Milestones with 'Blocked' status should be active."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms3',
                msx_status='Blocked',
            )
            assert ms.is_active is True
    
    def test_is_not_active_completed(self, app, sample_data):
        """Milestones with 'Completed' status should not be active."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms4',
                msx_status='Completed',
            )
            assert ms.is_active is False
    
    def test_is_not_active_cancelled(self, app, sample_data):
        """Milestones with 'Cancelled' status should not be active."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms5',
                msx_status='Cancelled',
            )
            assert ms.is_active is False
    
    def test_due_date_urgency_past_due(self, app, sample_data):
        """Milestones with past due date should show 'past_due'."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms6',
                due_date=datetime.now(timezone.utc) - timedelta(days=5),
            )
            assert ms.due_date_urgency == 'past_due'
    
    def test_due_date_urgency_this_week(self, app, sample_data):
        """Milestones due within 7 days should show 'this_week'."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms7',
                due_date=datetime.now(timezone.utc) + timedelta(days=3),
            )
            assert ms.due_date_urgency == 'this_week'
    
    def test_due_date_urgency_this_month(self, app, sample_data):
        """Milestones due within 30 days should show 'this_month'."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms8',
                due_date=datetime.now(timezone.utc) + timedelta(days=20),
            )
            assert ms.due_date_urgency == 'this_month'
    
    def test_due_date_urgency_future(self, app, sample_data):
        """Milestones due beyond 30 days should show 'future'."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms9',
                due_date=datetime.now(timezone.utc) + timedelta(days=60),
            )
            assert ms.due_date_urgency == 'future'
    
    def test_due_date_urgency_no_date(self, app, sample_data):
        """Milestones without due date should show 'no_date'."""
        with app.app_context():
            from app.models import Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/ms10',
                due_date=None,
            )
            assert ms.due_date_urgency == 'no_date'
    
    def test_new_fields_persist(self, app, sample_data):
        """New tracker fields (due_date, dollar_value, etc.) should save and load."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            
            due = datetime(2026, 6, 30)
            ms = Milestone(
                url='https://example.com/persist-test',
                msx_milestone_id='test-guid-persist',
                title='Persist Test',
                msx_status='On Track',
                due_date=due,
                dollar_value=50000.0,
                workload='Azure Data',
                monthly_usage=1234.56,
                last_synced_at=datetime.now(timezone.utc),
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()
            
            loaded = Milestone.query.filter_by(msx_milestone_id='test-guid-persist').first()
            assert loaded is not None
            assert loaded.due_date == due
            assert loaded.dollar_value == 50000.0
            assert loaded.workload == 'Azure Data'
            assert loaded.monthly_usage == 1234.56
            assert loaded.last_synced_at is not None
            
            # Cleanup
            db.session.delete(loaded)
            db.session.commit()


class TestMilestoneSyncService:
    """Test the milestone sync service."""
    
    @pytest.fixture(autouse=True)
    def mock_opp_sync(self):
        """Mock get_opportunities_by_account for all sync tests."""
        with patch('app.services.milestone_sync.get_opportunities_by_account') as mock:
            mock.return_value = {'success': True, 'opportunities': [], 'count': 0}
            yield mock

    def _create_test_customer_with_tpid_url(self, app, sample_data):
        """Helper to ensure we have a customer with a proper MSX tpid_url."""
        with app.app_context():
            from app.models import db, Customer
            customer = db.session.get(Customer, sample_data['customer1_id'])
            # Set a proper MSX URL with a GUID
            customer.tpid_url = (
                'https://microsoftsales.crm.dynamics.com/main.aspx'
                '?appid=fe0c3504&pagetype=entityrecord&etn=account'
                '&id=aaaabbbb-1111-2222-3333-444455556666'
            )
            db.session.commit()
            return customer.id
    
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_customer_milestones_creates_new(self, mock_get, app, sample_data):
        """Sync should create new milestones from MSX data."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)
        
        mock_get.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-111",
                    "name": "Deploy Azure SQL",
                    "number": "7-100001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "Acme Cloud Migration",
                    "workload": "Azure SQL",
                    "monthly_usage": 5000.0,
                    "due_date": "2026-03-15T00:00:00Z",
                    "dollar_value": 120000.0,
                    "url": "https://microsoftsales.crm.dynamics.com/main.aspx?id=ms-guid-111",
                },
            ],
            "count": 1,
        }
        
        with app.app_context():
            from app.models import db, Customer, Milestone, User
            from app.services.milestone_sync import sync_customer_milestones
            
            customer = db.session.get(Customer, customer_id)
            user = User.query.first()
            
            result = sync_customer_milestones(customer)
            
            assert result["success"] is True
            assert result["created"] == 1
            assert result["updated"] == 0
            
            # Verify sync passes open_opportunities_only and current_fy_only
            mock_get.assert_called_once()
            call_kwargs = mock_get.call_args
            assert call_kwargs == (
                (mock_get.call_args[0][0],),  # account_id positional arg
                {'open_opportunities_only': True, 'current_fy_only': True},
            )
            
            # Verify milestone was created
            ms = Milestone.query.filter_by(msx_milestone_id="ms-guid-111").first()
            assert ms is not None
            assert ms.title == "Deploy Azure SQL"
            assert ms.dollar_value == 120000.0
            assert ms.due_date is not None
            assert ms.customer_id == customer_id
            assert ms.workload == "Azure SQL"
            
            # Cleanup
            db.session.delete(ms)
            if ms.opportunity:
                db.session.delete(ms.opportunity)
            db.session.commit()

    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_enriches_opportunity_from_expand(self, mock_get, app, sample_data):
        """Sync should populate opportunity fields from expanded milestone data."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        mock_get.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-enrich",
                    "name": "Enrichment Test Milestone",
                    "number": "7-900001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "msx_opportunity_id": "opp-guid-enrich-111",
                    "opportunity_name": "Enriched Cloud Deal",
                    "opportunity_number": "7-ENRICH01",
                    "opportunity_statecode": 0,
                    "opportunity_state": "Open",
                    "opportunity_status_reason": "In Progress",
                    "opportunity_estimated_value": 750000.0,
                    "opportunity_estimated_close_date": "2026-09-30",
                    "opportunity_owner": "Jane Doe",
                    "workload": "Azure Core",
                    "monthly_usage": 3000.0,
                    "due_date": "2026-06-15T00:00:00Z",
                    "dollar_value": 100000.0,
                    "url": "https://example.com/ms-enrich",
                },
            ],
            "count": 1,
        }

        with app.app_context():
            from app.models import db, Customer, Milestone, Opportunity
            from app.services.milestone_sync import sync_customer_milestones

            customer = db.session.get(Customer, customer_id)
            result = sync_customer_milestones(customer)

            assert result["success"] is True
            assert result["created"] == 1
            assert result["opportunities_created"] == 1

            # Verify opportunity was created with enriched fields
            opp = Opportunity.query.filter_by(
                msx_opportunity_id="opp-guid-enrich-111"
            ).first()
            assert opp is not None
            assert opp.name == "Enriched Cloud Deal"
            assert opp.opportunity_number == "7-ENRICH01"
            assert opp.statecode == 0
            assert opp.state == "Open"
            assert opp.status_reason == "In Progress"
            assert opp.estimated_value == 750000.0
            assert opp.estimated_close_date == "2026-09-30"
            assert opp.owner_name == "Jane Doe"
            assert opp.msx_url is not None
            assert "opp-guid-enrich-111" in opp.msx_url

            # Cleanup
            ms = Milestone.query.filter_by(msx_milestone_id="ms-guid-enrich").first()
            if ms:
                db.session.delete(ms)
            db.session.delete(opp)
            db.session.commit()

    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_customer_milestones_updates_existing(self, mock_get, app, sample_data):
        """Sync should update existing milestones with fresh MSX data."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)
        
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            
            # Create an existing milestone
            existing = Milestone(
                msx_milestone_id="ms-guid-update",
                url="https://old-url.com",
                title="Old Title",
                msx_status="On Track",
                dollar_value=50000.0,
                customer_id=customer_id,
            )
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id
        
        mock_get.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-update",
                    "name": "Updated Title",
                    "number": "7-200002",
                    "status": "At Risk",
                    "status_code": 861980001,
                    "status_sort": 2,
                    "opportunity_name": "Updated Opp",
                    "workload": "Azure AI",
                    "monthly_usage": 8000.0,
                    "due_date": "2026-04-30T00:00:00Z",
                    "dollar_value": 200000.0,
                    "url": "https://new-url.com",
                },
            ],
            "count": 1,
        }
        
        with app.app_context():
            from app.models import db, Customer, Milestone, User
            from app.services.milestone_sync import sync_customer_milestones
            
            customer = db.session.get(Customer, customer_id)
            user = User.query.first()
            
            result = sync_customer_milestones(customer)
            
            assert result["success"] is True
            assert result["created"] == 0
            assert result["updated"] == 1
            
            ms = db.session.get(Milestone, existing_id)
            assert ms.title == "Updated Title"
            assert ms.msx_status == "At Risk"
            assert ms.dollar_value == 200000.0
            assert ms.workload == "Azure AI"
            assert ms.last_synced_at is not None
            
            # Cleanup
            db.session.delete(ms)
            db.session.commit()
    
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_deactivates_missing_milestones(self, mock_get, app, sample_data):
        """Milestones no longer in MSX results should keep their status, not be force-completed."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)
        
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            
            # Create a milestone that won't be returned by MSX
            disappearing = Milestone(
                msx_milestone_id="ms-guid-gone",
                url="https://gone.com",
                title="Gone Milestone",
                msx_status="On Track",
                customer_id=customer_id,
            )
            db.session.add(disappearing)
            db.session.commit()
            disappearing_id = disappearing.id
        
        # MSX returns empty list — our milestone is gone from the query
        mock_get.return_value = {
            "success": True,
            "milestones": [],
            "count": 0,
        }
        
        with app.app_context():
            from app.models import db, Customer, Milestone, User
            from app.services.milestone_sync import sync_customer_milestones
            
            customer = db.session.get(Customer, customer_id)
            user = User.query.first()
            
            result = sync_customer_milestones(customer)
            
            assert result["success"] is True
            assert result["deactivated"] == 1
            
            ms = db.session.get(Milestone, disappearing_id)
            # Status should be preserved, not force-set to "Completed"
            assert ms.msx_status == "On Track"
            assert ms.last_synced_at is not None
            
            # Cleanup
            db.session.delete(ms)
            db.session.commit()

    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_stale_opportunity_milestones(self, mock_get, app, sample_data):
        """Stale sync should refresh milestones on opportunities missed by the active sync."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        with app.app_context():
            from app.models import db, Milestone, Opportunity

            # Create a "stale" opportunity (closed, not in active sync results)
            stale_opp = Opportunity(
                msx_opportunity_id="stale-opp-guid-001",
                name="Closed Opp",
                customer_id=customer_id,
                statecode=1,
                state="Won",
            )
            db.session.add(stale_opp)
            db.session.flush()

            # Create a milestone on that stale opportunity with old status
            stale_ms = Milestone(
                msx_milestone_id="stale-ms-guid-001",
                url="https://stale.com",
                title="Stale Milestone",
                msx_status="On Track",
                customer_id=customer_id,
                opportunity_id=stale_opp.id,
            )
            db.session.add(stale_ms)
            db.session.commit()
            stale_ms_id = stale_ms.id
            stale_opp_id = stale_opp.id

        # The stale sync will call get_milestones_by_account without filters
        # and return the updated milestone data
        mock_get.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "stale-ms-guid-001",
                    "name": "Stale Milestone (Updated)",
                    "number": "7-999001",
                    "status": "Completed",
                    "status_code": 861980003,
                    "status_sort": 4,
                    "msx_opportunity_id": "stale-opp-guid-001",
                    "opportunity_name": "Closed Opp - Won",
                    "workload": "Azure SQL",
                    "monthly_usage": 500.0,
                    "due_date": "2025-12-31T00:00:00Z",
                    "dollar_value": 25000.0,
                    "url": "https://stale.com",
                    "opportunity_statecode": 1,
                    "opportunity_state": "Won",
                },
            ],
            "count": 1,
        }

        with app.app_context():
            from app.services.milestone_sync import (
                _sync_stale_opportunity_milestones,
            )

            # The active sync saw no opportunities (empty set)
            # so our stale opp should be picked up
            gen = _sync_stale_opportunity_milestones(set())
            try:
                while True:
                    next(gen)
            except StopIteration as stop:
                result = stop.value

            assert result["success"] is True
            assert result["milestones_updated"] == 1
            assert result["opportunities_refreshed"] == 1

            ms = db.session.get(Milestone, stale_ms_id)
            assert ms.msx_status == "Completed"
            assert ms.title == "Stale Milestone (Updated)"
            assert ms.last_synced_at is not None

            # Cleanup
            db.session.delete(ms)
            opp = db.session.get(Opportunity, stale_opp_id)
            if opp:
                db.session.delete(opp)
            db.session.commit()

    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_stale_skips_seen_opportunities(self, mock_get, app, sample_data):
        """Stale sync should not re-fetch opportunities already covered by the active sync."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        with app.app_context():
            from app.models import db, Milestone, Opportunity

            # Create an opportunity that WAS seen in the active sync
            seen_opp = Opportunity(
                msx_opportunity_id="seen-opp-guid-001",
                name="Active Opp",
                customer_id=customer_id,
                statecode=0,
                state="Open",
            )
            db.session.add(seen_opp)
            db.session.flush()

            ms = Milestone(
                msx_milestone_id="seen-ms-guid-001",
                url="https://active.com",
                title="Active Milestone",
                msx_status="On Track",
                customer_id=customer_id,
                opportunity_id=seen_opp.id,
            )
            db.session.add(ms)
            db.session.commit()
            ms_id = ms.id
            opp_id = seen_opp.id

        with app.app_context():
            from app.services.milestone_sync import (
                _sync_stale_opportunity_milestones,
            )

            # This opp was seen in the active sync, so stale sync should skip it
            gen = _sync_stale_opportunity_milestones({"seen-opp-guid-001"})
            try:
                while True:
                    next(gen)
            except StopIteration as stop:
                result = stop.value

            # Nothing should have been updated
            assert result["milestones_updated"] == 0
            assert result["opportunities_refreshed"] == 0

            # MSX API should not have been called
            mock_get.assert_not_called()

            # Cleanup
            ms = db.session.get(Milestone, ms_id)
            if ms:
                db.session.delete(ms)
            opp = db.session.get(Opportunity, opp_id)
            if opp:
                db.session.delete(opp)
            db.session.commit()
    
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_handles_msx_error(self, mock_get, app, sample_data):
        """Sync should handle MSX API errors gracefully."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)
        
        mock_get.return_value = {
            "success": False,
            "error": "Not authenticated. Run 'az login' first.",
        }
        
        with app.app_context():
            from app.models import db, Customer, User
            from app.services.milestone_sync import sync_customer_milestones
            
            customer = db.session.get(Customer, customer_id)
            user = User.query.first()
            
            result = sync_customer_milestones(customer)
            
            assert result["success"] is False
            assert "authenticated" in result["error"]
    
    def test_sync_customer_without_tpid_url(self, app, sample_data):
        """Sync should fail for customers without tpid_url."""
        with app.app_context():
            from app.models import db, Customer, User
            from app.services.milestone_sync import sync_customer_milestones
            
            # customer2 has no tpid_url
            customer = db.session.get(Customer, sample_data['customer2_id'])
            user = User.query.first()
            
            result = sync_customer_milestones(customer)
            
            assert result["success"] is False
            assert "account ID" in result["error"]
    
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_all_customer_milestones(self, mock_get, app, sample_data):
        """Full sync should process all customers with tpid_url."""
        # Only customer1 has tpid_url in sample_data
        self._create_test_customer_with_tpid_url(app, sample_data)
        
        mock_get.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-all-sync",
                    "name": "Full Sync Test",
                    "number": "7-300001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "Test Opp",
                    "workload": "Azure VM",
                    "monthly_usage": None,
                    "due_date": None,
                    "dollar_value": 75000.0,
                    "url": "https://test.com",
                },
            ],
            "count": 1,
        }
        
        with app.app_context():
            from app.models import db, Milestone, User
            from app.services.milestone_sync import sync_all_customer_milestones
            
            user = User.query.first()
            results = sync_all_customer_milestones()
            
            assert results["success"] is True
            assert results["customers_synced"] >= 1
            assert results["milestones_created"] >= 1
            assert results["duration_seconds"] >= 0
            
            # Cleanup
            ms = Milestone.query.filter_by(msx_milestone_id="ms-guid-all-sync").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()

    @patch('app.services.milestone_sync.get_tasks_for_milestones')
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_creates_tasks_from_msx(self, mock_get_ms, mock_get_tasks, app, sample_data):
        """Sync should create MsxTask records for user's tasks in MSX."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        mock_get_ms.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-task-test",
                    "name": "Task Test Milestone",
                    "number": "7-400001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "Task Opp",
                    "workload": "Azure SQL",
                    "monthly_usage": 1000.0,
                    "due_date": "2026-04-01T00:00:00Z",
                    "dollar_value": 50000.0,
                    "url": "https://example.com/ms-task-test",
                },
            ],
            "count": 1,
        }
        mock_get_tasks.return_value = {
            "success": True,
            "tasks": [
                {
                    "task_id": "task-guid-001",
                    "subject": "ADS Session",
                    "description": "Architecture Design Session for SQL migration",
                    "task_category": 861980004,
                    "task_category_name": "Architecture Design Session",
                    "is_hok": True,
                    "duration_minutes": 120,
                    "due_date": "2026-03-20T00:00:00Z",
                    "milestone_msx_id": "ms-guid-task-test",
                    "task_url": "https://example.com/task-001",
                },
            ],
        }

        with app.app_context():
            from app.models import db, Customer, Milestone, MsxTask
            from app.services.milestone_sync import sync_customer_milestones

            customer = db.session.get(Customer, customer_id)
            result = sync_customer_milestones(customer)

            assert result["success"] is True
            assert result["created"] == 1
            assert result["tasks_created"] == 1
            assert result["tasks_updated"] == 0

            # Verify task was created with correct fields
            task = MsxTask.query.filter_by(msx_task_id="task-guid-001").first()
            assert task is not None
            assert task.subject == "ADS Session"
            assert task.description == "Architecture Design Session for SQL migration"
            assert task.task_category == 861980004
            assert task.task_category_name == "Architecture Design Session"
            assert task.is_hok is True
            assert task.duration_minutes == 120
            assert task.msx_task_url == "https://example.com/task-001"
            assert task.note_id is None  # Synced tasks aren't linked to notes

            # Verify task is linked to the correct milestone
            ms = Milestone.query.filter_by(msx_milestone_id="ms-guid-task-test").first()
            assert task.milestone_id == ms.id

            # Cleanup
            db.session.delete(task)
            db.session.delete(ms)
            db.session.commit()

    @patch('app.services.milestone_sync.get_tasks_for_milestones')
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_updates_existing_tasks(self, mock_get_ms, mock_get_tasks, app, sample_data):
        """Sync should update existing MsxTask records with fresh MSX data."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        # Create milestone and existing task first
        with app.app_context():
            from app.models import db, Milestone, MsxTask

            ms = Milestone(
                msx_milestone_id="ms-guid-update-task",
                url="https://example.com/ms-update",
                title="Update Task Milestone",
                msx_status="On Track",
                customer_id=customer_id,
            )
            db.session.add(ms)
            db.session.flush()

            existing_task = MsxTask(
                msx_task_id="task-guid-update",
                subject="Old Subject",
                description="Old description",
                task_category=861980002,
                task_category_name="Demo",
                is_hok=True,
                duration_minutes=30,
                milestone_id=ms.id,
            )
            db.session.add(existing_task)
            db.session.commit()
            ms_id = ms.id
            task_id = existing_task.id

        mock_get_ms.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-update-task",
                    "name": "Update Task Milestone",
                    "number": "7-500001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "Update Opp",
                    "workload": "Azure AI",
                    "monthly_usage": 2000.0,
                    "due_date": "2026-05-01T00:00:00Z",
                    "dollar_value": 75000.0,
                    "url": "https://example.com/ms-update",
                },
            ],
            "count": 1,
        }
        mock_get_tasks.return_value = {
            "success": True,
            "tasks": [
                {
                    "task_id": "task-guid-update",
                    "subject": "Updated Subject",
                    "description": "Updated description",
                    "task_category": 861980004,
                    "task_category_name": "Architecture Design Session",
                    "is_hok": True,
                    "duration_minutes": 90,
                    "due_date": "2026-04-15T00:00:00Z",
                    "milestone_msx_id": "ms-guid-update-task",
                    "task_url": "https://example.com/task-updated",
                },
            ],
        }

        with app.app_context():
            from app.models import db, Customer, Milestone, MsxTask
            from app.services.milestone_sync import sync_customer_milestones

            customer = db.session.get(Customer, customer_id)
            result = sync_customer_milestones(customer)

            assert result["success"] is True
            assert result["tasks_created"] == 0
            assert result["tasks_updated"] == 1

            # Verify task was updated
            task = db.session.get(MsxTask, task_id)
            assert task.subject == "Updated Subject"
            assert task.description == "Updated description"
            assert task.task_category == 861980004
            assert task.task_category_name == "Architecture Design Session"
            assert task.duration_minutes == 90
            assert task.msx_task_url == "https://example.com/task-updated"

            # Cleanup
            db.session.delete(task)
            ms = db.session.get(Milestone, ms_id)
            db.session.delete(ms)
            db.session.commit()

    @patch('app.services.milestone_sync.get_tasks_for_milestones')
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_with_no_tasks(self, mock_get_ms, mock_get_tasks, app, sample_data):
        """Sync should succeed when user has no tasks in MSX."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        mock_get_ms.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-no-tasks",
                    "name": "No Tasks Milestone",
                    "number": "7-600001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "No Task Opp",
                    "workload": "Azure VM",
                    "monthly_usage": 500.0,
                    "due_date": "2026-06-01T00:00:00Z",
                    "dollar_value": 25000.0,
                    "url": "https://example.com/ms-no-tasks",
                },
            ],
            "count": 1,
        }
        mock_get_tasks.return_value = {
            "success": True,
            "tasks": [],
        }

        with app.app_context():
            from app.models import db, Customer, Milestone
            from app.services.milestone_sync import sync_customer_milestones

            customer = db.session.get(Customer, customer_id)
            result = sync_customer_milestones(customer)

            assert result["success"] is True
            assert result["created"] == 1
            assert result["tasks_created"] == 0
            assert result["tasks_updated"] == 0

            # Cleanup
            ms = Milestone.query.filter_by(msx_milestone_id="ms-guid-no-tasks").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()

    @patch('app.services.milestone_sync.get_tasks_for_milestones')
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_task_fetch_failure_graceful(self, mock_get_ms, mock_get_tasks, app, sample_data):
        """Milestone sync should succeed even if task fetch fails."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        mock_get_ms.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-task-fail",
                    "name": "Task Fail Milestone",
                    "number": "7-700001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "Task Fail Opp",
                    "workload": "Azure Storage",
                    "monthly_usage": 800.0,
                    "due_date": "2026-07-01T00:00:00Z",
                    "dollar_value": 30000.0,
                    "url": "https://example.com/ms-task-fail",
                },
            ],
            "count": 1,
        }
        mock_get_tasks.return_value = {
            "success": False,
            "tasks": [],
            "error": "Task API unavailable",
        }

        with app.app_context():
            from app.models import db, Customer, Milestone
            from app.services.milestone_sync import sync_customer_milestones

            customer = db.session.get(Customer, customer_id)
            result = sync_customer_milestones(customer)

            # Milestone sync should still succeed
            assert result["success"] is True
            assert result["created"] == 1
            # Task counts should be 0 since fetch failed
            assert result["tasks_created"] == 0
            assert result["tasks_updated"] == 0

            # Cleanup
            ms = Milestone.query.filter_by(msx_milestone_id="ms-guid-task-fail").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()

    @patch('app.services.milestone_sync.get_tasks_for_milestones')
    @patch('app.services.milestone_sync.get_milestones_by_account')
    def test_sync_links_tasks_to_correct_milestones(self, mock_get_ms, mock_get_tasks, app, sample_data):
        """Tasks should be linked to the correct local milestone by MSX ID."""
        customer_id = self._create_test_customer_with_tpid_url(app, sample_data)

        mock_get_ms.return_value = {
            "success": True,
            "milestones": [
                {
                    "id": "ms-guid-link-a",
                    "name": "Milestone A",
                    "number": "7-800001",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "Link Test Opp",
                    "workload": "Azure SQL",
                    "monthly_usage": 1000.0,
                    "due_date": "2026-08-01T00:00:00Z",
                    "dollar_value": 40000.0,
                    "url": "https://example.com/ms-link-a",
                },
                {
                    "id": "ms-guid-link-b",
                    "name": "Milestone B",
                    "number": "7-800002",
                    "status": "On Track",
                    "status_code": 861980000,
                    "status_sort": 1,
                    "opportunity_name": "Link Test Opp",
                    "workload": "Azure AI",
                    "monthly_usage": 2000.0,
                    "due_date": "2026-09-01T00:00:00Z",
                    "dollar_value": 60000.0,
                    "url": "https://example.com/ms-link-b",
                },
            ],
            "count": 2,
        }
        mock_get_tasks.return_value = {
            "success": True,
            "tasks": [
                {
                    "task_id": "task-for-a",
                    "subject": "Task for Milestone A",
                    "description": None,
                    "task_category": 861980002,
                    "task_category_name": "Demo",
                    "is_hok": True,
                    "duration_minutes": 60,
                    "due_date": "2026-08-15T00:00:00Z",
                    "milestone_msx_id": "ms-guid-link-a",
                    "task_url": "https://example.com/task-a",
                },
                {
                    "task_id": "task-for-b",
                    "subject": "Task for Milestone B",
                    "description": None,
                    "task_category": 861980004,
                    "task_category_name": "Architecture Design Session",
                    "is_hok": True,
                    "duration_minutes": 120,
                    "due_date": "2026-09-15T00:00:00Z",
                    "milestone_msx_id": "ms-guid-link-b",
                    "task_url": "https://example.com/task-b",
                },
            ],
        }

        with app.app_context():
            from app.models import db, Customer, Milestone, MsxTask
            from app.services.milestone_sync import sync_customer_milestones

            customer = db.session.get(Customer, customer_id)
            result = sync_customer_milestones(customer)

            assert result["success"] is True
            assert result["created"] == 2
            assert result["tasks_created"] == 2

            # Verify tasks are linked to correct milestones
            ms_a = Milestone.query.filter_by(msx_milestone_id="ms-guid-link-a").first()
            ms_b = Milestone.query.filter_by(msx_milestone_id="ms-guid-link-b").first()

            task_a = MsxTask.query.filter_by(msx_task_id="task-for-a").first()
            task_b = MsxTask.query.filter_by(msx_task_id="task-for-b").first()

            assert task_a.milestone_id == ms_a.id
            assert task_a.subject == "Task for Milestone A"

            assert task_b.milestone_id == ms_b.id
            assert task_b.subject == "Task for Milestone B"

            # Cleanup
            db.session.delete(task_a)
            db.session.delete(task_b)
            db.session.delete(ms_a)
            db.session.delete(ms_b)
            db.session.commit()


class TestMilestoneTrackerData:
    """Test the tracker data retrieval function."""
    
    def _create_tracker_milestones(self, app, sample_data):
        """Create test milestones for tracker data tests."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            
            ms1 = Milestone(
                msx_milestone_id="tracker-ms-1",
                url="https://tracker1.com",
                title="High Value Past Due",
                msx_status="On Track",
                dollar_value=500000.0,
                monthly_usage=50000.0,
                due_date=datetime.now(timezone.utc) - timedelta(days=10),
                workload="Data: SQL Modernization to Azure SQL DB",
                customer_id=sample_data['customer1_id'],
                last_synced_at=datetime.now(timezone.utc),
            )
            ms2 = Milestone(
                msx_milestone_id="tracker-ms-2",
                url="https://tracker2.com",
                title="Low Value This Week",
                msx_status="At Risk",
                dollar_value=10000.0,
                monthly_usage=1000.0,
                due_date=datetime.now(timezone.utc) + timedelta(days=3),
                workload="Infra: Windows",
                customer_id=sample_data['customer1_id'],
                last_synced_at=datetime.now(timezone.utc),
            )
            ms3 = Milestone(
                msx_milestone_id="tracker-ms-3",
                url="https://tracker3.com",
                title="No Dollar Value",
                msx_status="Blocked",
                dollar_value=None,
                monthly_usage=None,
                due_date=datetime.now(timezone.utc) + timedelta(days=45),
                workload="AI: Foundry Models - OpenAI",
                customer_id=sample_data['customer1_id'],
            )
            # Completed milestone — should NOT appear in tracker
            ms4 = Milestone(
                msx_milestone_id="tracker-ms-4",
                url="https://tracker4.com",
                title="Completed One",
                msx_status="Completed",
                dollar_value=100000.0,
                monthly_usage=10000.0,
                customer_id=sample_data['customer1_id'],
            )
            db.session.add_all([ms1, ms2, ms3, ms4])
            db.session.commit()
            return [ms1.id, ms2.id, ms3.id, ms4.id]
    
    def test_tracker_data_includes_all_statuses(self, app, sample_data):
        """Tracker should include milestones of all statuses."""
        ids = self._create_tracker_milestones(app, sample_data)
        
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import get_milestone_tracker_data
            
            data = get_milestone_tracker_data()
            
            titles = [m["title"] for m in data["milestones"]]
            assert "Completed One" in titles
            assert "High Value Past Due" in titles
            assert "Low Value This Week" in titles
            assert "No Dollar Value" in titles
            
            # Cleanup
            for mid in ids:
                ms = db.session.get(Milestone, mid)
                if ms:
                    db.session.delete(ms)
            db.session.commit()
    
    def test_tracker_data_sorted_by_monthly_usage_desc(self, app, sample_data):
        """Tracker should sort by monthly_usage descending by default."""
        ids = self._create_tracker_milestones(app, sample_data)
        
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import get_milestone_tracker_data
            
            data = get_milestone_tracker_data()
            milestones = data["milestones"]
            
            # Sorted by monthly_usage desc: 50k, 10k, 1k, None(0)
            assert milestones[0]["title"] == "High Value Past Due"
            assert milestones[0]["monthly_usage"] == 50000.0
            assert milestones[1]["title"] == "Completed One"
            assert milestones[1]["monthly_usage"] == 10000.0
            assert milestones[2]["title"] == "Low Value This Week"
            assert milestones[2]["monthly_usage"] == 1000.0
            assert milestones[3]["monthly_usage"] is None
            
            # Cleanup
            for mid in ids:
                ms = db.session.get(Milestone, mid)
                if ms:
                    db.session.delete(ms)
            db.session.commit()
    
    def test_tracker_summary_totals(self, app, sample_data):
        """Summary should have correct counts and totals."""
        ids = self._create_tracker_milestones(app, sample_data)
        
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import get_milestone_tracker_data
            
            data = get_milestone_tracker_data()
            summary = data["summary"]
            
            assert summary["total_count"] == 4  # all milestones including completed
            assert summary["total_monthly_usage"] == 61000.0  # 50k + 10k + 1k
            assert summary["past_due_count"] == 1
            assert summary["this_week_count"] == 1
            
            # Cleanup
            for mid in ids:
                ms = db.session.get(Milestone, mid)
                if ms:
                    db.session.delete(ms)
            db.session.commit()
    
    def test_tracker_includes_seller_info(self, app, sample_data):
        """Tracker data should include seller info from customer relationship."""
        ids = self._create_tracker_milestones(app, sample_data)
        
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import get_milestone_tracker_data
            
            data = get_milestone_tracker_data()
            
            # customer1 has seller1 (Alice Smith) in sample_data
            for ms in data["milestones"]:
                assert ms["seller"] is not None
                assert ms["seller"]["name"] == "Alice Smith"
            
            # Cleanup
            for mid in ids:
                ms = db.session.get(Milestone, mid)
                if ms:
                    db.session.delete(ms)
            db.session.commit()
    
    def test_tracker_extracts_workload_areas(self, app, sample_data):
        """Tracker should extract area prefix from workload strings."""
        ids = self._create_tracker_milestones(app, sample_data)
        
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import get_milestone_tracker_data
            
            data = get_milestone_tracker_data()
            
            # Check workload_area is correctly extracted
            areas_in_data = {ms["workload_area"] for ms in data["milestones"]}
            assert "Data" in areas_in_data
            assert "Infra" in areas_in_data
            assert "AI" in areas_in_data
            
            # Check areas list for dropdown
            assert "Data" in data["areas"]
            assert "Infra" in data["areas"]
            assert "AI" in data["areas"]
            
            # Cleanup
            for mid in ids:
                ms = db.session.get(Milestone, mid)
                if ms:
                    db.session.delete(ms)
            db.session.commit()


class TestMilestoneTrackerRoutes:
    """Test the milestone tracker route handlers."""
    
    def test_tracker_page_loads(self, client, app, sample_data):
        """Milestone tracker page should load successfully."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'Milestone Tracker' in response.data
    
    def test_tracker_page_shows_empty_state(self, client, app, sample_data):
        """Tracker should show empty state when no milestones."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'No Active Milestones' in response.data or b'Milestone Tracker' in response.data
    
    def test_tracker_page_shows_milestones(self, client, app, sample_data):
        """Tracker should display milestones when they exist."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            
            ms = Milestone(
                msx_milestone_id="route-test-ms",
                url="https://route-test.com",
                title="Route Test Milestone",
                msx_status="On Track",
                dollar_value=75000.0,
                monthly_usage=7500.0,
                due_date=datetime.now(timezone.utc) + timedelta(days=5),
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()
        
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'Route Test Milestone' in response.data
        assert b'$7,500' in response.data
        
        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone.query.filter_by(msx_milestone_id="route-test-ms").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()
    
    @patch('app.services.milestone_sync.sync_all_customer_milestones')
    def test_sync_api_endpoint(self, mock_sync, client, app, sample_data):
        """POST /api/milestone-tracker/sync should trigger sync."""
        mock_sync.return_value = {
            "success": True,
            "customers_synced": 5,
            "customers_skipped": 2,
            "customers_failed": 0,
            "milestones_created": 10,
            "milestones_updated": 3,
            "milestones_deactivated": 1,
            "errors": [],
            "duration_seconds": 4.2,
        }
        
        response = client.post('/api/milestone-tracker/sync')
        assert response.status_code == 202
        
        data = response.get_json()
        assert data["success"] is True
        assert data["async"] is True
    
    @patch('app.services.milestone_sync.sync_all_customer_milestones')
    def test_sync_api_partial_failure(self, mock_sync, client, app, sample_data):
        """Sync with partial failures should return 207."""
        mock_sync.return_value = {
            "success": False,
            "customers_synced": 0,
            "customers_failed": 3,
            "milestones_created": 0,
            "milestones_updated": 0,
            "milestones_deactivated": 0,
            "errors": ["Auth failed"],
            "duration_seconds": 1.0,
        }
        
        response = client.post('/api/milestone-tracker/sync')
        assert response.status_code == 202
        data = response.get_json()
        assert data["async"] is True
    
    def test_tracker_page_has_sync_button(self, client, app, sample_data):
        """Tracker page should have a sync button."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'Sync from MSX' in response.data
    
    def test_tracker_page_has_filters(self, client, app, sample_data):
        """Tracker page should have filter controls."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'sellerFilter' in response.data
        assert b'statusDropdownBtn' in response.data
        assert b'areaDropdownBtn' in response.data
    
    def test_tracker_page_has_sortable_columns(self, client, app, sample_data):
        """Tracker page should have sortable column headers."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            ms = Milestone(
                msx_milestone_id="sort-test-ms",
                url="https://sort-test.com",
                title="Sort Test",
                msx_status="On Track",
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'sortTable' in response.data
        assert b'data-sort="customer"' in response.data
        assert b'data-sort="seller"' in response.data
        assert b'data-sort="status"' in response.data
        assert b'data-sort="due-date"' in response.data
        assert b'data-sort="monthly"' in response.data

        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone.query.filter_by(msx_milestone_id="sort-test-ms").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()


class TestMilestoneSyncDateParsing:
    """Test the date parsing utility in the sync service."""
    
    def test_parse_iso_date_with_z(self, app):
        """Should parse ISO 8601 date with Z suffix."""
        with app.app_context():
            from app.services.milestone_sync import _parse_msx_date
            result = _parse_msx_date("2026-06-30T00:00:00Z")
            assert result is not None
            assert result.year == 2026
            assert result.month == 6
            assert result.day == 30
    
    def test_parse_iso_date_without_z(self, app):
        """Should parse ISO 8601 date without Z suffix."""
        with app.app_context():
            from app.services.milestone_sync import _parse_msx_date
            result = _parse_msx_date("2026-03-15T00:00:00")
            assert result is not None
            assert result.year == 2026
            assert result.month == 3
    
    def test_parse_none_returns_none(self, app):
        """Should return None for None input."""
        with app.app_context():
            from app.services.milestone_sync import _parse_msx_date
            assert _parse_msx_date(None) is None
    
    def test_parse_empty_returns_none(self, app):
        """Should return None for empty string."""
        with app.app_context():
            from app.services.milestone_sync import _parse_msx_date
            assert _parse_msx_date("") is None
    
    def test_parse_invalid_returns_none(self, app):
        """Should return None for garbage input."""
        with app.app_context():
            from app.services.milestone_sync import _parse_msx_date
            assert _parse_msx_date("not-a-date") is None


class TestMilestoneTrackerNav:
    """Test that the milestone tracker is accessible from navigation."""
    
    def test_nav_has_milestone_tracker_link(self, client, app, sample_data):
        """Main nav should have a link to the milestone tracker."""
        response = client.get('/')
        assert response.status_code == 200
        assert b'milestone-tracker' in response.data
        assert b'Milestones' in response.data


class TestSyncCustomerEndpoint:
    """Test the single-customer sync endpoint."""
    
    @patch('app.services.milestone_sync.sync_customer_milestones')
    def test_sync_single_customer(self, mock_sync, client, app, sample_data):
        """Should sync milestones for a single customer."""
        # customer1 has tpid_url
        mock_sync.return_value = {
            "success": True,
            "created": 2,
            "updated": 1,
            "deactivated": 0,
            "error": "",
        }
        
        response = client.post(
            f'/api/milestone-tracker/sync-customer/{sample_data["customer1_id"]}'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
    
    def test_sync_customer_without_tpid_url(self, client, app, sample_data):
        """Should fail if customer has no tpid_url."""
        response = client.post(
            f'/api/milestone-tracker/sync-customer/{sample_data["customer2_id"]}'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
    
    def test_sync_nonexistent_customer(self, client, app, sample_data):
        """Should return 404 for nonexistent customer."""
        response = client.post('/api/milestone-tracker/sync-customer/99999')
        assert response.status_code == 404


class TestSSESync:
    """Test the Server-Sent Events streaming sync."""

    def test_sse_event_format(self, app):
        """_sse_event should produce valid SSE format."""
        with app.app_context():
            from app.services.milestone_sync import _sse_event
            result = _sse_event('progress', {'current': 1, 'total': 5})
            assert result.startswith('event: progress\n')
            assert 'data: {' in result
            assert result.endswith('\n\n')

    def test_sse_event_json_payload(self, app):
        """_sse_event data field should be valid JSON."""
        import json
        with app.app_context():
            from app.services.milestone_sync import _sse_event
            result = _sse_event('complete', {'success': True, 'count': 42})
            data_line = [l for l in result.split('\n') if l.startswith('data: ')][0]
            payload = json.loads(data_line[6:])
            assert payload['success'] is True
            assert payload['count'] == 42

    @patch('app.services.milestone_sync.batch_get_milestones')
    @patch('app.services.milestone_sync.batch_get_opportunities')
    @patch('app.services.milestone_sync._update_team_memberships')
    def test_stream_yields_start_progress_complete(self, mock_teams,
                                                    mock_batch_opps,
                                                    mock_batch_ms, app,
                                                    sample_data):
        """Streaming sync should yield start, progress, and complete events."""
        # batch_get_opportunities returns one opp for the test account
        mock_batch_opps.return_value = {
            'success': True,
            'by_account': {
                'aaaabbbb-1111-2222-3333-444455556666': [{
                    'id': 'opp-stream-1',
                    'name': 'Stream Opp',
                    'number': '7-999',
                    'state': 'Open',
                    'statecode': 0,
                    'status_reason': 'In Progress',
                    'estimated_value': None,
                    'estimated_close_date': None,
                    'owner': '',
                    'url': 'https://test.com/opp',
                    'customer_need': '',
                    'description': '',
                    'compete_threat': '',
                }],
            },
        }
        # batch_get_milestones returns one milestone for that opp
        mock_batch_ms.return_value = {
            'success': True,
            'by_opportunity': {
                'opp-stream-1': [{
                    'id': 'stream-test-ms-1',
                    'name': 'Stream Test',
                    'number': '7-999',
                    'status': 'On Track',
                    'status_code': 861980000,
                    'status_sort': 0,
                    'customer_commitment': '',
                    'msx_opportunity_id': 'opp-stream-1',
                    'workload': '',
                    'monthly_usage': None,
                    'due_date': None,
                    'dollar_value': None,
                    'url': 'https://test.com',
                    'committed_on': None,
                    'completed_on': None,
                    'comments_json': None,
                    'created_on': None,
                    'modified_on': None,
                }],
            },
        }
        # Ensure at least one customer has a tpid_url
        with app.app_context():
            from app.models import db, Customer
            customer = db.session.get(Customer, sample_data['customer1_id'])
            customer.tpid_url = (
                'https://microsoftsales.crm.dynamics.com/main.aspx'
                '?appid=fe0c3504&pagetype=entityrecord&etn=account'
                '&id=aaaabbbb-1111-2222-3333-444455556666'
            )
            db.session.commit()

        with app.app_context():
            from app.services.milestone_sync import sync_all_customer_milestones_stream
            from app.models import User
            user = User.query.first()

            events = list(sync_all_customer_milestones_stream())

        # Parse events
        event_types = []
        for evt in events:
            for line in evt.split('\n'):
                if line.startswith('event: '):
                    event_types.append(line[7:])

        assert event_types[0] == 'start'
        assert 'progress' in event_types
        assert event_types[-1] == 'complete'

    def test_sync_api_sse_returns_event_stream(self, client, app, sample_data):
        """POST with Accept: text/event-stream should return SSE content type."""
        with patch('app.services.milestone_sync.sync_all_customer_milestones_stream') as mock_stream:
            mock_stream.return_value = iter([
                'event: start\ndata: {"total": 1}\n\n',
                'event: complete\ndata: {"success": true}\n\n',
            ])
            response = client.post(
                '/api/milestone-tracker/sync',
                headers={'Accept': 'text/event-stream'},
            )
            assert response.status_code == 200
            assert 'text/event-stream' in response.content_type

    def test_sync_api_json_fallback(self, client, app, sample_data):
        """POST without SSE accept header should return JSON."""
        with patch('app.services.milestone_sync.sync_all_customer_milestones') as mock_sync:
            mock_sync.return_value = {
                "success": True,
                "customers_synced": 1,
                "customers_failed": 0,
                "milestones_created": 3,
                "milestones_updated": 0,
                "milestones_deactivated": 0,
                "errors": [],
                "duration_seconds": 0.5,
            }
            response = client.post(
                '/api/milestone-tracker/sync',
                headers={'Accept': 'application/json'},
            )
            assert response.status_code == 202
            data = response.get_json()
            assert data["success"] is True
            assert data["async"] is True

    def test_tracker_page_has_progress_bar_html(self, client, app, sample_data):
        """Tracker page should have the progress bar container."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'syncProgressBar' in response.data
        assert b'syncProgressWrap' in response.data

    def test_tracker_page_has_area_filter(self, client, app, sample_data):
        """Tracker page should have the area filter dropdown."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'areaDropdownBtn' in response.data


class TestFiscalYearFilter:
    """Tests for the fiscal-year date filter on milestone queries."""

    def _setup_mock_request(self, mock_request):
        """Configure mock _msx_request to return empty milestone response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'value': []}
        mock_request.return_value = mock_resp

    @patch('app.services.msx_api._msx_request')
    def test_fy_filter_builds_correct_odata(self, mock_request):
        """current_fy_only should add msp_milestonedate range to OData $filter."""
        from app.services.msx_api import get_milestones_by_account
        self._setup_mock_request(mock_request)

        get_milestones_by_account('acct-id', current_fy_only=True)

        url = mock_request.call_args[0][1]  # _msx_request('GET', url)
        assert 'msp_milestonedate ge' in url
        assert 'msp_milestonedate le' in url
        assert '-07-01' in url
        assert '-06-30' in url

    @patch('app.services.msx_api._msx_request')
    def test_fy_filter_disabled_by_default(self, mock_request):
        """Without current_fy_only, no date filter should be present."""
        from app.services.msx_api import get_milestones_by_account
        self._setup_mock_request(mock_request)

        get_milestones_by_account('acct-id')

        url = mock_request.call_args[0][1]
        # msp_milestonedate appears in $select but should NOT appear in $filter
        assert 'msp_milestonedate ge' not in url
        assert 'msp_milestonedate le' not in url

    @patch('app.services.msx_api._msx_request')
    def test_fy_boundary_second_half(self, mock_request):
        """In Oct 2025 (month >= 7), FY starts July 2025 and ends June 2026."""
        from app.services.msx_api import get_milestones_by_account
        self._setup_mock_request(mock_request)

        with patch('app.services.msx_api.dt') as mock_dt:
            mock_dt.now.return_value = datetime(2025, 10, 15, tzinfo=timezone.utc)
            get_milestones_by_account('acct-id', current_fy_only=True)

        url = mock_request.call_args[0][1]
        assert '2025-07-01' in url
        assert '2026-06-30' in url

    @patch('app.services.msx_api._msx_request')
    def test_fy_boundary_first_half(self, mock_request):
        """In Mar 2026 (month < 7), FY starts July 2025 and ends June 2026."""
        from app.services.msx_api import get_milestones_by_account
        self._setup_mock_request(mock_request)

        with patch('app.services.msx_api.dt') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 15, tzinfo=timezone.utc)
            get_milestones_by_account('acct-id', current_fy_only=True)

        url = mock_request.call_args[0][1]
        assert '2025-07-01' in url
        assert '2026-06-30' in url


class TestMilestoneCalendarAPI:
    """Tests for the milestone calendar API endpoint."""

    def test_calendar_returns_json(self, client, app, sample_data):
        """GET /api/milestones/calendar should return JSON with expected keys."""
        response = client.get('/api/milestones/calendar?year=2026&month=3')
        assert response.status_code == 200
        data = response.get_json()
        assert data['year'] == 2026
        assert data['month'] == 3
        assert data['month_name'] == 'March'
        assert 'days' in data
        assert 'days_in_month' in data

    def test_calendar_defaults_to_current_month(self, client, app, sample_data):
        """Without params, should default to current month."""
        response = client.get('/api/milestones/calendar')
        assert response.status_code == 200
        data = response.get_json()
        assert 'year' in data
        assert 'month' in data

    def test_calendar_includes_active_milestones(self, client, app, sample_data):
        """Calendar should include active milestones with due dates in range."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            ms = Milestone(
                msx_milestone_id="cal-test-ms",
                url="https://cal-test.com",
                title="Calendar Test Milestone",
                msx_status="On Track",
                monthly_usage=5000.0,
                due_date=datetime(2026, 3, 15),
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get('/api/milestones/calendar?year=2026&month=3')
        data = response.get_json()
        assert '15' in data['days'] or 15 in data['days']
        day_entries = data['days'].get('15', data['days'].get(15, []))
        assert len(day_entries) >= 1
        titles = [e['title'] for e in day_entries]
        assert 'Calendar Test Milestone' in titles

        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone.query.filter_by(msx_milestone_id="cal-test-ms").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()

    def test_calendar_includes_completed_milestones(self, client, app, sample_data):
        """Completed milestones should appear on the calendar."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            ms = Milestone(
                msx_milestone_id="cal-done-ms",
                url="https://cal-done.com",
                title="Completed Milestone",
                msx_status="Completed",
                due_date=datetime(2026, 3, 20),
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get('/api/milestones/calendar?year=2026&month=3')
        data = response.get_json()
        day_entries = data['days'].get('20', data['days'].get(20, []))
        titles = [e['title'] for e in day_entries]
        assert 'Completed Milestone' in titles

        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone.query.filter_by(msx_milestone_id="cal-done-ms").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()

    def test_calendar_entry_has_expected_fields(self, client, app, sample_data):
        """Each calendar entry should have title, status, customer_name, url."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            ms = Milestone(
                msx_milestone_id="cal-fields-ms",
                url="https://cal-fields.com",
                title="Fields Test",
                msx_status="At Risk",
                monthly_usage=8000.0,
                workload="Data: SQL",
                due_date=datetime(2026, 4, 10),
                customer_id=sample_data['customer1_id'],
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get('/api/milestones/calendar?year=2026&month=4')
        data = response.get_json()
        day_entries = data['days'].get('10', data['days'].get(10, []))
        assert len(day_entries) >= 1
        entry = [e for e in day_entries if e['title'] == 'Fields Test'][0]
        assert entry['status'] == 'At Risk'
        assert entry['monthly_usage'] == 8000.0
        assert entry['workload'] == 'Data: SQL'
        assert entry['url'] == 'https://cal-fields.com'
        assert entry['customer_name'] is not None

        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone.query.filter_by(msx_milestone_id="cal-fields-ms").first()
            if ms:
                db.session.delete(ms)
                db.session.commit()


class TestMilestoneCalendarTab:
    """Tests for the milestone calendar on the tracker page."""

    def _mark_milestones_synced(self, app):
        """Mark milestones as synced and create a milestone so the tab renders."""
        with app.app_context():
            from app.models import db, SyncStatus, Milestone, Customer
            from datetime import datetime, date
            status = SyncStatus.query.filter_by(sync_type='milestones').first()
            if not status:
                status = SyncStatus(sync_type='milestones')
                db.session.add(status)
            status.started_at = datetime(2026, 1, 1)
            status.completed_at = datetime(2026, 1, 1)
            status.success = True
            # Ensure at least one milestone exists (has_milestones checks DB)
            if not Milestone.query.first():
                customer = Customer.query.first()
                if not customer:
                    customer = Customer(name='Cal Test Customer', tpid=99999)
                    db.session.add(customer)
                    db.session.flush()
                ms = Milestone(
                    title='Calendar Test Milestone',
                    customer_id=customer.id,
                    msx_status='On Track',
                    url='https://example.com/milestone/1',
                    due_date=date(2026, 4, 1),
                )
                db.session.add(ms)
            db.session.commit()

    def test_tracker_has_calendar_view(self, client, app, sample_data):
        """Tracker page should have the calendar container."""
        self._mark_milestones_synced(app)
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'msCalendarWrap' in response.data
        assert b'msCalendarTable' in response.data

    def test_index_does_not_have_calendar(self, client, app, sample_data):
        """Front page should not have the milestone calendar (moved to tracker)."""
        response = client.get('/')
        assert response.status_code == 200
        assert b'msCalendarTable' not in response.data

    def test_tracker_calendar_has_full_week(self, client, app, sample_data):
        """Milestone calendar should have 7-day week headers (Sun-Sat)."""
        self._mark_milestones_synced(app)
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'msCalendarTable' in response.data
        assert b'<th>Sun</th>' in response.data
        assert b'<th>Sat</th>' in response.data

    def test_tracker_has_view_tabs(self, client, app, sample_data):
        """Tracker page JS should set up list/calendar view tabs."""
        self._mark_milestones_synced(app)
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'data-ms-view="list"' in response.data
        assert b'data-ms-view="calendar"' in response.data


class TestOnMyTeamModel:
    """Test the on_my_team field on the Milestone model."""

    def test_on_my_team_defaults_false(self, app, sample_data):
        """New milestones should default to on_my_team=False."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/team-test',
                msx_status='On Track',
            )
            db.session.add(ms)
            db.session.commit()
            assert ms.on_my_team is False

    def test_on_my_team_can_be_set_true(self, app, sample_data):
        """on_my_team can be set to True."""
        with app.app_context():
            from app.models import db, Milestone, User
            user = User.query.first()
            ms = Milestone(
                url='https://example.com/team-test2',
                msx_status='On Track',
                on_my_team=True,
            )
            db.session.add(ms)
            db.session.commit()
            assert ms.on_my_team is True


class TestUpdateTeamMemberships:
    """Test the _update_team_memberships sync helper."""

    @patch('app.services.milestone_sync.get_my_milestone_team_ids')
    def test_updates_matching_milestones(self, mock_get_teams, app, sample_data):
        """Should set on_my_team=True for matching milestone IDs."""
        with app.app_context():
            from app.models import db, Milestone, User
            from app.services.milestone_sync import _update_team_memberships

            user = User.query.first()
            ms1 = Milestone(
                url='https://example.com/t1',
                msx_milestone_id='aaa-111',
                msx_status='On Track',
            )
            ms2 = Milestone(
                url='https://example.com/t2',
                msx_milestone_id='bbb-222',
                msx_status='On Track',
            )
            db.session.add_all([ms1, ms2])
            db.session.commit()

            mock_get_teams.return_value = {
                'success': True,
                'milestone_ids': {'aaa-111'},
                'team_count': 5,
            }

            _update_team_memberships()

            db.session.refresh(ms1)
            db.session.refresh(ms2)
            assert ms1.on_my_team is True
            assert ms2.on_my_team is False

    @patch('app.services.milestone_sync.get_my_milestone_team_ids')
    def test_clears_old_memberships(self, mock_get_teams, app, sample_data):
        """Should set on_my_team=False for milestones no longer on team."""
        with app.app_context():
            from app.models import db, Milestone, User
            from app.services.milestone_sync import _update_team_memberships

            user = User.query.first()
            ms = Milestone(
                url='https://example.com/t3',
                msx_milestone_id='ccc-333',
                msx_status='On Track',
                on_my_team=True,
            )
            db.session.add(ms)
            db.session.commit()

            mock_get_teams.return_value = {
                'success': True,
                'milestone_ids': set(),
                'team_count': 5,
            }

            _update_team_memberships()

            db.session.refresh(ms)
            assert ms.on_my_team is False

    @patch('app.services.milestone_sync.get_my_milestone_team_ids')
    def test_handles_api_failure_gracefully(self, mock_get_teams, app, sample_data):
        """Should not crash if team API fails."""
        with app.app_context():
            from app.models import db, Milestone, User
            from app.services.milestone_sync import _update_team_memberships

            user = User.query.first()
            ms = Milestone(
                url='https://example.com/t4',
                msx_milestone_id='ddd-444',
                msx_status='On Track',
                on_my_team=True,
            )
            db.session.add(ms)
            db.session.commit()

            mock_get_teams.return_value = {
                'success': False,
                'error': 'API down',
                'milestone_ids': set(),
            }

            # Should not raise and should preserve existing values
            _update_team_memberships()

            db.session.refresh(ms)
            assert ms.on_my_team is True  # Unchanged because API failed


    @patch('app.services.milestone_sync.get_my_milestone_team_ids')
    def test_partial_pagination_only_adds_never_removes(self, mock_get_teams, app, sample_data):
        """When pagination is incomplete, only set True — never clear existing flags."""
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import _update_team_memberships

            ms_on_team = Milestone(
                url='https://example.com/t5',
                msx_milestone_id='eee-555',
                msx_status='On Track',
                on_my_team=True,
            )
            ms_new = Milestone(
                url='https://example.com/t6',
                msx_milestone_id='fff-666',
                msx_status='On Track',
                on_my_team=False,
            )
            db.session.add_all([ms_on_team, ms_new])
            db.session.commit()

            # API returned partial data (pagination broke) — only fff-666 came back
            mock_get_teams.return_value = {
                'success': True,
                'milestone_ids': {'fff-666'},
                'team_count': 3,
                'pagination_complete': False,
            }

            _update_team_memberships()

            db.session.refresh(ms_on_team)
            db.session.refresh(ms_new)
            # eee-555 was already True and should NOT be cleared
            assert ms_on_team.on_my_team is True
            # fff-666 was False and should be set to True
            assert ms_new.on_my_team is True

    @patch('app.services.milestone_sync.get_my_milestone_team_ids')
    def test_complete_pagination_clears_unmatched(self, mock_get_teams, app, sample_data):
        """When pagination is complete, milestones not in the set should be cleared."""
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import _update_team_memberships

            ms = Milestone(
                url='https://example.com/t7',
                msx_milestone_id='ggg-777',
                msx_status='On Track',
                on_my_team=True,
            )
            db.session.add(ms)
            db.session.commit()

            mock_get_teams.return_value = {
                'success': True,
                'milestone_ids': set(),
                'team_count': 5,
                'pagination_complete': True,
            }

            _update_team_memberships()

            db.session.refresh(ms)
            assert ms.on_my_team is False

    @patch('app.services.milestone_sync.get_my_milestone_team_ids')
    def test_sync_does_not_touch_local_only_milestones(self, mock_get_teams, app, sample_data):
        """Milestones without MSX IDs should not have on_my_team cleared by sync."""
        with app.app_context():
            from app.models import db, Milestone
            from app.services.milestone_sync import _update_team_memberships

            ms_local = Milestone(
                url='https://example.com/local',
                msx_milestone_id=None,
                msx_status='On Track',
                on_my_team=True,
            )
            db.session.add(ms_local)
            db.session.commit()

            mock_get_teams.return_value = {
                'success': True,
                'milestone_ids': set(),
                'team_count': 5,
                'pagination_complete': True,
            }

            _update_team_memberships()

            db.session.refresh(ms_local)
            assert ms_local.on_my_team is True


class TestOnMyTeamInTracker:
    """Test on_my_team display in the tracker page."""

    def test_tracker_has_my_team_filter(self, client, app, sample_data):
        """Tracker page should have the My Team filter toggle."""
        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'myTeamFilter' in response.data
        assert b'On Team' in response.data

    def test_tracker_shows_team_icon(self, client, app, sample_data):
        """Milestones on my team should show the people icon."""
        with app.app_context():
            from app.models import db, Milestone, User, Customer
            user = User.query.first()
            customer = Customer.query.first()
            ms = Milestone(
                url='https://example.com/team-icon-test',
                title='Team Icon Test MS',
                msx_milestone_id='team-icon-test-id',
                msx_status='On Track',
                customer_id=customer.id,
                on_my_team=True,
                monthly_usage=1000.0,
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get('/reports/milestone-tracker')
        assert response.status_code == 200
        assert b'bi-people-fill' in response.data
        assert b'data-on-my-team="true"' in response.data

    def test_tracker_data_includes_on_my_team(self, app, sample_data):
        """get_milestone_tracker_data should include on_my_team field."""
        with app.app_context():
            from app.models import db, Milestone, User, Customer
            from app.services.milestone_sync import get_milestone_tracker_data

            user = User.query.first()
            customer = Customer.query.first()
            ms = Milestone(
                url='https://example.com/tracker-data-test',
                title='Tracker Data Test',
                msx_milestone_id='tracker-data-test-id',
                msx_status='On Track',
                customer_id=customer.id,
                on_my_team=True,
            )
            db.session.add(ms)
            db.session.commit()

            data = get_milestone_tracker_data()
            my_ms = [m for m in data['milestones'] if m['title'] == 'Tracker Data Test']
            assert len(my_ms) == 1
            assert my_ms[0]['on_my_team'] is True


class TestOnMyTeamInCalendar:
    """Test on_my_team in the milestone calendar API."""

    def test_calendar_api_includes_on_my_team(self, client, app, sample_data):
        """Calendar API should include on_my_team field in entries."""
        with app.app_context():
            from app.models import db, Milestone, User, Customer

            user = User.query.first()
            customer = Customer.query.first()
            ms = Milestone(
                url='https://example.com/cal-team-test',
                title='Calendar Team Test',
                msx_milestone_id='cal-team-test-id',
                msx_status='On Track',
                customer_id=customer.id,
                on_my_team=True,
                due_date=datetime(2026, 2, 15),
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get('/api/milestones/calendar?year=2026&month=2')
        assert response.status_code == 200
        data = response.get_json()
        day_entries = data['days'].get('15', [])
        team_entries = [e for e in day_entries if e.get('on_my_team')]
        assert len(team_entries) >= 1


# ---------------------------------------------------------------------------
# SyncStatus heartbeat tests
# ---------------------------------------------------------------------------

class TestSyncStatusHeartbeat:
    """Tests for the heartbeat-based in_progress detection."""

    def test_fresh_heartbeat_shows_in_progress(self, app):
        """A sync with a recent heartbeat should report state='in_progress'."""
        from app.models import SyncStatus
        with app.app_context():
            SyncStatus.mark_started('milestones')
            SyncStatus.update_heartbeat('milestones')
            status = SyncStatus.get_status('milestones')
            assert status['state'] == 'in_progress'

    def test_stale_heartbeat_shows_incomplete(self, app):
        """A sync with a stale heartbeat should report state='incomplete'."""
        from app.models import SyncStatus, utc_now, db
        from datetime import timedelta
        with app.app_context():
            SyncStatus.mark_started('milestones')
            # Manually set heartbeat to 2 minutes ago (past the 60s threshold)
            row = SyncStatus.query.filter_by(sync_type='milestones').first()
            row.heartbeat_at = utc_now() - timedelta(seconds=120)
            db.session.commit()
            status = SyncStatus.get_status('milestones')
            assert status['state'] == 'incomplete'

    def test_no_heartbeat_shows_incomplete(self, app):
        """A sync with no heartbeat at all should report state='incomplete'."""
        from app.models import SyncStatus, db
        with app.app_context():
            SyncStatus.mark_started('milestones')
            # Clear heartbeat to simulate old data without the column
            row = SyncStatus.query.filter_by(sync_type='milestones').first()
            row.heartbeat_at = None
            db.session.commit()
            status = SyncStatus.get_status('milestones')
            assert status['state'] == 'incomplete'

    def test_completed_sync_ignores_heartbeat(self, app):
        """A completed sync should report 'complete' regardless of heartbeat age."""
        from app.models import SyncStatus
        with app.app_context():
            SyncStatus.mark_started('milestones')
            SyncStatus.mark_completed('milestones', success=True, items_synced=5)
            status = SyncStatus.get_status('milestones')
            assert status['state'] == 'complete'

    def test_in_progress_banner_on_tracker_page(self, app, client):
        """Milestone tracker should show 'sync is running' when heartbeat is fresh."""
        from app.models import SyncStatus, Customer, db
        with app.app_context():
            if not Customer.query.first():
                db.session.add(Customer(name='Banner Test', tpid=88888))
                db.session.commit()
            SyncStatus.mark_started('milestones')
            SyncStatus.update_heartbeat('milestones')
        resp = client.get('/reports/milestone-tracker')
        assert resp.status_code == 200
        assert b'Milestone sync is running' in resp.data
        assert b'spinner-border' in resp.data

    def test_incomplete_banner_on_tracker_page(self, app, client):
        """Milestone tracker should show 'didn\\'t finish' when heartbeat is stale."""
        from app.models import SyncStatus, Customer, utc_now, db
        from datetime import timedelta
        with app.app_context():
            if not Customer.query.first():
                db.session.add(Customer(name='Banner Test', tpid=88888))
                db.session.commit()
            SyncStatus.mark_started('milestones')
            row = SyncStatus.query.filter_by(sync_type='milestones').first()
            row.heartbeat_at = utc_now() - timedelta(seconds=120)
            db.session.commit()
        resp = client.get('/reports/milestone-tracker')
        assert resp.status_code == 200
        assert b"didn&#39;t finish" in resp.data or b"didn't finish" in resp.data


# ---------------------------------------------------------------------------
# Comment sync tests
# ---------------------------------------------------------------------------

class TestSyncTeamMilestoneComments:
    """Test syncing forecast comments for milestones the user is on the team for."""

    def test_syncs_comments_for_team_milestones(self, app, sample_data):
        """Should fetch and cache comments for on_my_team milestones."""
        with app.app_context():
            from app.models import db, Milestone, Customer
            from app.services.milestone_sync import _sync_team_milestone_comments

            customer = Customer.query.first()
            ms = Milestone(
                url='https://example.com/comment-sync-test',
                title='Comment Sync Test MS',
                msx_milestone_id='comment-sync-guid-1',
                msx_status='On Track',
                customer_id=customer.id,
                on_my_team=True,
            )
            db.session.add(ms)
            db.session.commit()

            mock_comments = [
                {"userId": "user-1", "modifiedOn": "2026-03-20", "comment": "Looking good"},
                {"userId": "user-2", "modifiedOn": "2026-03-21", "comment": "Agreed"},
            ]

            with patch(
                'app.services.milestone_sync.get_milestone_comments'
            ) as mock_get:
                mock_get.return_value = {
                    'success': True,
                    'comments': mock_comments,
                }
                gen = _sync_team_milestone_comments()
                # Drain the generator
                try:
                    while True:
                        next(gen)
                except StopIteration as stop:
                    result = stop.value

            assert result['success'] is True
            assert result['comments_synced'] == 1
            assert result['comments_failed'] == 0

            db.session.refresh(ms)
            import json
            cached = json.loads(ms.cached_comments_json)
            assert len(cached) == 2
            assert cached[0]['comment'] == 'Looking good'
            assert ms.details_fetched_at is not None

    def test_skips_milestones_not_on_team(self, app, sample_data):
        """Should not fetch comments for milestones where on_my_team=False."""
        with app.app_context():
            from app.models import db, Milestone, Customer
            from app.services.milestone_sync import _sync_team_milestone_comments

            customer = Customer.query.first()
            ms = Milestone(
                url='https://example.com/not-team-test',
                title='Not On Team MS',
                msx_milestone_id='not-team-guid-1',
                msx_status='On Track',
                customer_id=customer.id,
                on_my_team=False,
            )
            db.session.add(ms)
            db.session.commit()

            with patch(
                'app.services.milestone_sync.get_milestone_comments'
            ) as mock_get:
                gen = _sync_team_milestone_comments()
                try:
                    while True:
                        next(gen)
                except StopIteration as stop:
                    result = stop.value

            mock_get.assert_not_called()
            assert result['comments_synced'] == 0

    def test_handles_comment_fetch_failure(self, app, sample_data):
        """Should count failures but continue syncing other milestones."""
        with app.app_context():
            from app.models import db, Milestone, Customer
            from app.services.milestone_sync import _sync_team_milestone_comments

            customer = Customer.query.first()
            ms1 = Milestone(
                url='https://example.com/fail-test-1',
                title='Fail Test 1',
                msx_milestone_id='fail-guid-1',
                msx_status='On Track',
                customer_id=customer.id,
                on_my_team=True,
            )
            ms2 = Milestone(
                url='https://example.com/fail-test-2',
                title='Fail Test 2',
                msx_milestone_id='fail-guid-2',
                msx_status='On Track',
                customer_id=customer.id,
                on_my_team=True,
            )
            db.session.add_all([ms1, ms2])
            db.session.commit()

            def mock_get_comments(ms_id):
                if ms_id == 'fail-guid-1':
                    return {'success': False, 'comments': [], 'error': 'Timeout'}
                return {
                    'success': True,
                    'comments': [{"userId": "u", "modifiedOn": "d", "comment": "ok"}],
                }

            with patch(
                'app.services.milestone_sync.get_milestone_comments',
                side_effect=mock_get_comments,
            ):
                gen = _sync_team_milestone_comments()
                try:
                    while True:
                        next(gen)
                except StopIteration as stop:
                    result = stop.value

            assert result['comments_synced'] == 1
            assert result['comments_failed'] == 1

    def test_stream_includes_comment_sync_events(self, app, sample_data):
        """Streaming sync should emit comment_sync_start and comment_sync_end events."""
        import json
        with app.app_context():
            from app.models import db, Customer
            customer = db.session.get(Customer, sample_data['customer1_id'])
            customer.tpid_url = (
                'https://microsoftsales.crm.dynamics.com/main.aspx'
                '?appid=fe0c3504&pagetype=entityrecord&etn=account'
                '&id=aaaabbbb-1111-2222-3333-444455556666'
            )
            db.session.commit()

        with app.app_context():
            from app.services.milestone_sync import sync_all_customer_milestones_stream

            with patch('app.services.milestone_sync.get_milestones_by_account') as mock_get, \
                 patch('app.services.milestone_sync._update_team_memberships'), \
                 patch('app.services.milestone_sync.get_milestone_comments') as mock_comments, \
                 patch('app.services.milestone_sync.batch_get_opportunities') as mock_batch_opps:
                mock_batch_opps.return_value = {'success': True, 'by_account': {}}
                mock_get.return_value = {
                    'success': True,
                    'milestones': [{
                        'id': 'comment-stream-ms-1',
                        'name': 'Comment Stream Test',
                        'number': '7-888',
                        'status': 'On Track',
                        'status_code': 861980000,
                        'msx_opportunity_id': None,
                        'opportunity_name': '',
                        'workload': '',
                        'monthly_usage': None,
                        'due_date': None,
                        'dollar_value': None,
                        'url': 'https://test.com',
                    }],
                    'count': 1,
                }
                mock_comments.return_value = {
                    'success': True,
                    'comments': [{"userId": "u", "modifiedOn": "d", "comment": "test"}],
                }

                events = list(sync_all_customer_milestones_stream())

            event_types = []
            event_data = []
            for evt in events:
                for line in evt.split('\n'):
                    if line.startswith('event: '):
                        event_types.append(line[7:])
                    elif line.startswith('data: '):
                        event_data.append(json.loads(line[6:]))

            assert 'comment_sync_start' in event_types
            assert 'comment_sync_end' in event_types

            # comment_sync_end should have counts
            end_idx = event_types.index('comment_sync_end')
            end_data = event_data[end_idx]
            assert 'comments_synced' in end_data

            # complete event should include comments_synced
            complete_idx = event_types.index('complete')
            complete_data = event_data[complete_idx]
            assert 'comments_synced' in complete_data
