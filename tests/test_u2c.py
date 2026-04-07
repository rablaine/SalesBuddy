"""Tests for U2C snapshot service and report."""
import pytest
from datetime import datetime, timezone, date, timedelta

from app.models import (
    db, Customer, Milestone, Opportunity, U2CSnapshot, U2CSnapshotItem,
)
from app.services.u2c_snapshot import (
    create_snapshot, current_fiscal_quarter, fiscal_quarter_date_range,
    get_attainment, get_workload_prefixes, is_snapshot_due,
)


@pytest.fixture
def u2c_data(app):
    """Create milestones on open opportunities for U2C snapshot testing."""
    with app.app_context():
        customer = Customer(name='Test Corp', tpid=9999)
        db.session.add(customer)
        db.session.flush()

        opp = Opportunity(
            msx_opportunity_id='opp-001',
            name='Test Opportunity',
            statecode=0,  # Open
            state='Open',
            customer_id=customer.id,
        )
        db.session.add(opp)
        db.session.flush()

        # Determine a due date in the current fiscal quarter
        fq = current_fiscal_quarter()
        q_start, q_end = fiscal_quarter_date_range(fq)
        mid_quarter = datetime.combine(
            q_start + timedelta(days=45), datetime.min.time()
        )

        ms1 = Milestone(
            url='https://example.com/ms1',
            title='Deploy Fabric',
            msx_status='On Track',
            customer_commitment='Uncommitted',
            workload='Data: Analytics - Fabric - New Analytics',
            monthly_usage=5000.0,
            due_date=mid_quarter,
            customer_id=customer.id,
            opportunity_id=opp.id,
        )
        ms2 = Milestone(
            url='https://example.com/ms2',
            title='Migrate SQL',
            msx_status='At Risk',
            customer_commitment='Uncommitted',
            workload='Data: SQL Modernization to Azure SQL DB with AI (PaaS)',
            monthly_usage=3000.0,
            due_date=mid_quarter,
            customer_id=customer.id,
            opportunity_id=opp.id,
        )
        ms3 = Milestone(
            url='https://example.com/ms3',
            title='Setup AVD',
            msx_status='On Track',
            customer_commitment='Uncommitted',
            workload='Infra: AVD (Native AVD)',
            monthly_usage=2000.0,
            due_date=mid_quarter,
            customer_id=customer.id,
            opportunity_id=opp.id,
        )
        # Committed milestone - should NOT be in snapshot
        ms4 = Milestone(
            url='https://example.com/ms4',
            title='Already Committed',
            msx_status='On Track',
            customer_commitment='Committed',
            workload='Data: Cosmos DB (Migrate & Modernize)',
            monthly_usage=1000.0,
            due_date=mid_quarter,
            customer_id=customer.id,
            opportunity_id=opp.id,
        )
        # Milestone on closed opportunity - should NOT be in snapshot
        closed_opp = Opportunity(
            msx_opportunity_id='opp-002',
            name='Lost Opportunity',
            statecode=2,  # Lost
            state='Lost',
            customer_id=customer.id,
        )
        db.session.add(closed_opp)
        db.session.flush()
        ms5 = Milestone(
            url='https://example.com/ms5',
            title='Lost Milestone',
            msx_status='On Track',
            customer_commitment='Uncommitted',
            workload='Infra: Windows',
            monthly_usage=4000.0,
            due_date=mid_quarter,
            customer_id=customer.id,
            opportunity_id=closed_opp.id,
        )

        db.session.add_all([ms1, ms2, ms3, ms4, ms5])
        db.session.commit()

        return {
            'customer_id': customer.id,
            'opp_id': opp.id,
            'ms1_id': ms1.id,
            'ms2_id': ms2.id,
            'ms3_id': ms3.id,
            'ms4_id': ms4.id,
            'ms5_id': ms5.id,
        }


class TestFiscalQuarter:
    """Test fiscal quarter helper functions."""

    def test_current_fiscal_quarter_q3(self):
        """January-March should be Q3."""
        assert current_fiscal_quarter(date(2026, 1, 15)) == 'FY26 Q3'
        assert current_fiscal_quarter(date(2026, 3, 31)) == 'FY26 Q3'

    def test_current_fiscal_quarter_q4(self):
        """April-June should be Q4."""
        assert current_fiscal_quarter(date(2026, 4, 6)) == 'FY26 Q4'
        assert current_fiscal_quarter(date(2026, 6, 30)) == 'FY26 Q4'

    def test_current_fiscal_quarter_q1(self):
        """July-September should be Q1 of next FY."""
        assert current_fiscal_quarter(date(2026, 7, 1)) == 'FY27 Q1'
        assert current_fiscal_quarter(date(2026, 9, 30)) == 'FY27 Q1'

    def test_current_fiscal_quarter_q2(self):
        """October-December should be Q2."""
        assert current_fiscal_quarter(date(2026, 10, 1)) == 'FY27 Q2'
        assert current_fiscal_quarter(date(2026, 12, 31)) == 'FY27 Q2'

    def test_fiscal_quarter_date_range_q4(self):
        """FY26 Q4 should be Apr 1 - Jun 30, 2026."""
        start, end = fiscal_quarter_date_range('FY26 Q4')
        assert start == date(2026, 4, 1)
        assert end == date(2026, 6, 30)

    def test_fiscal_quarter_date_range_q1(self):
        """FY27 Q1 should be Jul 1 - Sep 30, 2026."""
        start, end = fiscal_quarter_date_range('FY27 Q1')
        assert start == date(2026, 7, 1)
        assert end == date(2026, 9, 30)

    def test_fiscal_quarter_date_range_q3(self):
        """FY26 Q3 should be Jan 1 - Mar 31, 2026."""
        start, end = fiscal_quarter_date_range('FY26 Q3')
        assert start == date(2026, 1, 1)
        assert end == date(2026, 3, 31)


class TestCreateSnapshot:
    """Test U2C snapshot creation."""

    def test_create_snapshot_captures_uncommitted(self, app, u2c_data):
        """Snapshot should capture only uncommitted milestones on open opps."""
        with app.app_context():
            result = create_snapshot()
            assert result['success'] is True
            assert result['total_items'] == 3  # ms1, ms2, ms3
            # ms4 (committed) and ms5 (closed opp) excluded
            assert result['total_monthly_acr'] == 10000.0  # 5000+3000+2000

    def test_create_snapshot_prevents_duplicates(self, app, u2c_data):
        """Cannot create two snapshots for the same fiscal quarter."""
        with app.app_context():
            result1 = create_snapshot()
            assert result1['success'] is True
            result2 = create_snapshot()
            assert result2['success'] is False
            assert 'already exists' in result2['error']

    def test_snapshot_stores_frozen_data(self, app, u2c_data):
        """Snapshot items should store milestone data at snapshot time."""
        with app.app_context():
            create_snapshot()
            snapshot = U2CSnapshot.query.first()
            items = snapshot.items.all()
            assert len(items) == 3
            titles = {i.milestone_title for i in items}
            assert 'Deploy Fabric' in titles
            assert 'Migrate SQL' in titles
            assert 'Setup AVD' in titles
            assert 'Already Committed' not in titles

    def test_snapshot_records_customer_name(self, app, u2c_data):
        """Items should have the customer name frozen."""
        with app.app_context():
            create_snapshot()
            item = U2CSnapshotItem.query.first()
            assert item.customer_name == 'Test Corp'


class TestAttainment:
    """Test U2C attainment calculation."""

    def test_attainment_all_uncommitted(self, app, u2c_data):
        """When nothing is committed, attainment should be 0%."""
        with app.app_context():
            create_snapshot()
            snapshot = U2CSnapshot.query.first()
            result = get_attainment(snapshot.id)
            assert result['success'] is True
            assert result['attainment_pct'] == 0.0
            assert result['committed_count'] == 0
            assert result['remaining_count'] == 3

    def test_attainment_after_commit(self, app, u2c_data):
        """Committing a milestone should increase attainment."""
        with app.app_context():
            create_snapshot()
            snapshot = U2CSnapshot.query.first()

            # Commit ms1 (5000 ACR)
            ms1 = Milestone.query.get(u2c_data['ms1_id'])
            ms1.customer_commitment = 'Committed'
            db.session.commit()

            result = get_attainment(snapshot.id)
            assert result['committed_count'] == 1
            assert result['committed_total'] == 5000.0
            assert result['remaining_count'] == 2
            assert result['attainment_pct'] == 50.0  # 5000/10000

    def test_attainment_completed_counts(self, app, u2c_data):
        """Completed milestones should count as committed."""
        with app.app_context():
            create_snapshot()
            snapshot = U2CSnapshot.query.first()

            # Mark ms2 as completed
            ms2 = Milestone.query.get(u2c_data['ms2_id'])
            ms2.msx_status = 'Completed'
            db.session.commit()

            result = get_attainment(snapshot.id)
            assert result['committed_count'] == 1
            assert result['committed_total'] == 3000.0

    def test_attainment_workload_filter(self, app, u2c_data):
        """Workload filter should scope to matching milestones only."""
        with app.app_context():
            create_snapshot()
            snapshot = U2CSnapshot.query.first()

            result = get_attainment(snapshot.id, workload_prefix='Data')
            # Only ms1 and ms2 are Data: workloads
            assert result['total_in_scope'] == 2
            assert result['target_total'] == 8000.0  # 5000+3000

    def test_remaining_sorted_by_acr(self, app, u2c_data):
        """Remaining items should be sorted by monthly ACR descending."""
        with app.app_context():
            create_snapshot()
            snapshot = U2CSnapshot.query.first()
            result = get_attainment(snapshot.id)
            acrs = [i['monthly_acr'] for i in result['remaining_items']]
            assert acrs == sorted(acrs, reverse=True)


class TestWorkloadPrefixes:
    """Test workload prefix extraction."""

    def test_get_workload_prefixes(self, app, u2c_data):
        """Should return distinct workload prefixes."""
        with app.app_context():
            create_snapshot()
            snapshot = U2CSnapshot.query.first()
            prefixes = get_workload_prefixes(snapshot.id)
            assert 'Data' in prefixes
            assert 'Infra' in prefixes


class TestIsSnapshotDue:
    """Test auto-snapshot date detection."""

    def test_not_due_on_wrong_day(self, app):
        """Should not be due on a random day."""
        with app.app_context():
            from unittest.mock import patch
            with patch('app.services.u2c_snapshot.date') as mock_date:
                mock_date.today.return_value = date(2026, 4, 15)
                mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
                assert is_snapshot_due() is False


class TestReportRoute:
    """Test the U2C report web route."""

    def test_report_page_loads(self, client):
        """Report page should load without errors."""
        response = client.get('/reports/u2c')
        assert response.status_code == 200
        assert b'U2C Attainment' in response.data

    def test_report_with_snapshot(self, client, app, u2c_data):
        """Report should show attainment when a snapshot exists."""
        with app.app_context():
            create_snapshot()
        fq = current_fiscal_quarter()
        response = client.get(f'/reports/u2c?fq={fq}')
        assert response.status_code == 200
        assert b'Attainment' in response.data

    def test_create_snapshot_api(self, client, app, u2c_data):
        """POST to create snapshot should work."""
        response = client.post(
            '/api/reports/u2c/create-snapshot',
            json={},
            content_type='application/json',
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['total_items'] == 3

    def test_create_snapshot_api_duplicate(self, client, app, u2c_data):
        """Duplicate snapshot creation should return 409."""
        client.post(
            '/api/reports/u2c/create-snapshot',
            json={},
            content_type='application/json',
        )
        response = client.post(
            '/api/reports/u2c/create-snapshot',
            json={},
            content_type='application/json',
        )
        assert response.status_code == 409


class TestSalesIQTool:
    """Test the U2C SalesIQ tool."""

    def test_u2c_tool_exists(self):
        """U2C attainment tool should be registered."""
        from app.services.salesiq_tools import TOOLS
        names = {t['name'] for t in TOOLS}
        assert 'get_u2c_attainment' in names

    def test_u2c_tool_no_snapshot(self, app):
        """Tool should return helpful message when no snapshot exists."""
        with app.app_context():
            from app.services.salesiq_tools import execute_tool
            result = execute_tool('get_u2c_attainment', {})
            assert 'No U2C snapshot' in result.get('message', '')

    def test_u2c_tool_with_data(self, app, u2c_data):
        """Tool should return attainment data when snapshot exists."""
        with app.app_context():
            create_snapshot()
            from app.services.salesiq_tools import execute_tool
            result = execute_tool('get_u2c_attainment', {})
            assert result['success'] is True
            assert 'attainment_pct' in result
