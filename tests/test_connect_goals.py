"""Tests for FY27 Data Connect Goals calculations."""
from datetime import date, datetime, timezone
import json

from app.models import (
    Customer,
    CustomerRevenueData,
    Milestone,
    MsxTask,
    RevenueImport,
    U2CSnapshot,
    U2CSnapshotItem,
    U2CSnapshotVersion,
    U2CSnapshotVersionItem,
    UserPreference,
    db,
)
from app.services import connect_goals


def _confirm_scope() -> None:
    """Confirm FY27 Databases and Fabric priorities for a test."""
    preference = UserPreference.query.first()
    preference.compensated_buckets = json.dumps(['Databases', 'Fabric'])
    preference.compensated_buckets_fiscal_year = 'FY27'
    preference.compensated_buckets_confirmed_taxonomy_version = (
        preference.bucket_taxonomy_version
    )


def _seed_required_buckets(customer_id: int | None = None) -> None:
    """Seed the authoritative FY27 Data buckets needed by the setup gate."""
    imported = RevenueImport(filename='connect-scope-test')
    db.session.add(imported)
    db.session.flush()
    for bucket in ('Databases', 'Fabric'):
        db.session.add(CustomerRevenueData(
            customer_name='Scope Customer',
            customer_id=customer_id,
            bucket=bucket,
            fiscal_month='FY27-Jul',
            month_date=date(2026, 7, 1),
            revenue=1,
            last_import_id=imported.id,
        ))


def _milestone(customer_id: int, *, number: str, on_team: bool,
               monthly_usage: float) -> Milestone:
    """Build an in-scope FY27 Data milestone."""
    return Milestone(
        msx_milestone_id=f'msx-{number}',
        milestone_number=number,
        url=f'https://example.test/{number}',
        title=f'Milestone {number}',
        msx_status='On Track',
        due_date=datetime(2027, 3, 15),
        workload='Data: SQL Modernization',
        monthly_usage=monthly_usage,
        on_my_team=on_team,
        customer_id=customer_id,
    )


def test_setup_requires_confirmed_fy27_buckets(app):
    """The page remains gated until both required buckets are confirmed."""
    with app.app_context():
        preference = UserPreference.query.first()
        preference.compensated_buckets = json.dumps(['Databases'])
        preference.compensated_buckets_fiscal_year = 'FY27'
        preference.compensated_buckets_confirmed_taxonomy_version = (
            preference.bucket_taxonomy_version
        )
        imported = RevenueImport(filename='setup-test')
        db.session.add(imported)
        db.session.flush()
        for bucket in ('Databases', 'Fabric'):
            db.session.add(CustomerRevenueData(
                customer_name='Setup Customer',
                bucket=bucket,
                fiscal_month='FY27-Jul',
                month_date=date(2026, 7, 1),
                revenue=1,
                last_import_id=imported.id,
            ))
        db.session.commit()

        state = connect_goals.get_setup_state(date(2026, 9, 23))

        assert state['ready'] is False
        assert state['missing_required'] == ['Fabric']


def test_connect_goals_route_shows_setup_gate(client):
    """The report explains annual setup instead of rendering fake values."""
    response = client.get('/reports/connect-goals')

    assert response.status_code == 200
    assert b'Confirm your FY27 Data priorities' in response.data


def test_connect_goals_route_renders_four_metrics(client, app):
    """A confirmed FY27 scope renders the fixed v1 metric definitions."""
    with app.app_context():
        _seed_required_buckets()
        _confirm_scope()
        db.session.commit()

    response = client.get('/reports/connect-goals')

    assert response.status_code == 200
    assert b'Quarterly U2C progression' in response.data
    assert b'Milestone team coverage' in response.data
    assert b'Milestone HoK coverage' in response.data
    assert b'Fabric and database whitespace wins' in response.data
    assert b'No matched Databases or Fabric revenue history is available.' in response.data


def test_team_coverage_is_acr_weighted(app):
    """Team coverage uses milestone ACR rather than milestone count."""
    with app.app_context():
        customer = Customer(name='Coverage Customer', tpid='coverage-123')
        db.session.add(customer)
        db.session.flush()
        db.session.add_all([
            _milestone(customer.id, number='7-TEAM', on_team=True,
                       monthly_usage=41),
            _milestone(customer.id, number='7-OFF', on_team=False,
                       monthly_usage=59),
        ])
        db.session.commit()

        widget = connect_goals.get_team_coverage_widget(date(2026, 9, 23))

        assert widget['value'] == 41.0
        assert widget['goal_ratio'] == 0.82
        assert widget['status_tone'] == 'warning'
        assert widget['status_label'] == 'Near goal'
        assert widget['on_team_count'] == 1
        assert widget['total_count'] == 2
        assert widget['destination_filter']['status'] == [
            'On Track', 'At Risk', 'Blocked'
        ]
        assert widget['destination_filter']['seller'] == ''
        assert widget['destination_filter']['urgency'] == ''


def test_team_coverage_excludes_unmatched_milestones(app):
    """The widget population matches the milestones available in its drill-down."""
    with app.app_context():
        customer = Customer(name='Matched Customer', tpid='matched-123')
        db.session.add(customer)
        db.session.flush()
        matched = _milestone(
            customer.id, number='7-MATCHED', on_team=True, monthly_usage=25
        )
        unmatched = Milestone(
            msx_milestone_id='msx-unmatched',
            milestone_number='7-UNMATCHED',
            url='https://example.test/unmatched',
            title='Unmatched milestone',
            msx_status='On Track',
            due_date=datetime(2027, 3, 15),
            workload='Data: Fabric',
            monthly_usage=75,
            on_my_team=False,
        )
        db.session.add_all([matched, unmatched])
        db.session.commit()

        widget = connect_goals.get_team_coverage_widget(date(2026, 9, 23))

        assert widget['value'] == 100.0
        assert widget['total_count'] == 1


def test_hok_coverage_and_validation_counts(app):
    """HoK coverage respects the scope and reports validation categories."""
    with app.app_context():
        customer = Customer(name='HoK Customer', tpid='hok-123')
        db.session.add(customer)
        db.session.flush()
        covered = _milestone(
            customer.id, number='7-COVERED', on_team=True, monthly_usage=50
        )
        uncovered = _milestone(
            customer.id, number='7-UNCOVERED', on_team=True, monthly_usage=50
        )
        db.session.add_all([covered, uncovered])
        db.session.flush()
        db.session.add(MsxTask(
            msx_task_id='task-demo',
            subject='Customer demo',
            task_category=861980002,
            task_category_name='Demo',
            is_hok=True,
            due_date=datetime(2026, 8, 1),
            milestone_id=covered.id,
        ))
        db.session.commit()

        widget = connect_goals.get_hok_coverage_widget(date(2026, 9, 23))

        assert widget['value'] == 50.0
        assert widget['covered'] == 1
        assert widget['uncovered'] == 1
        assert widget['validation_counts']['Demo'] == 1


def test_whitespace_win_uses_rolling_three_months(app, client):
    """A first positive month qualifies after three complete zero months."""
    with app.app_context():
        customer = Customer(
            name='Whitespace Customer',
            tpid='123',
            favicon_b64='ZmFrZQ==',
        )
        imported = RevenueImport(filename='whitespace-test')
        db.session.add_all([customer, imported])
        db.session.flush()
        months = [
            (date(2026, 2, 1), 0),
            (date(2026, 3, 1), 0),
            (date(2026, 4, 1), 0),
            (date(2026, 5, 1), 0),
            (date(2026, 6, 1), 0),
            (date(2026, 7, 1), 0),
            (date(2026, 8, 1), 0),
            (date(2026, 9, 1), 250),
        ]
        for month, revenue in months:
            db.session.add(CustomerRevenueData(
                customer_name=customer.name,
                tpid=customer.tpid,
                customer_id=customer.id,
                bucket='Fabric',
                fiscal_month=month.strftime('FY27-%b'),
                month_date=month,
                revenue=revenue,
                last_import_id=imported.id,
            ))
        db.session.add(CustomerRevenueData(
            customer_name=customer.name,
            tpid=customer.tpid,
            customer_id=customer.id,
            bucket='Databases',
            fiscal_month='FY27-Sep',
            month_date=date(2026, 9, 1),
            revenue=0,
            last_import_id=imported.id,
        ))
        _confirm_scope()
        db.session.commit()

        widget = connect_goals.get_whitespace_widget(date(2026, 9, 23))

        fabric = next(
            item for item in widget['bucket_progress']
            if item['bucket'] == 'Fabric'
        )
        assert fabric['wins'] == 1
        win = widget['wins']['Fabric'][0]
        assert win['first_positive_month'] == '2026-09-01'
        assert [point['label'] for point in win['history']] == [
            'Feb26', 'Mar26', 'Apr26', 'May26',
            'Jun26', 'Jul26', 'Aug26', 'Sep26',
        ]
        assert [point['revenue'] for point in win['history']] == [
            0, 0, 0, 0, 0, 0, 0, 250,
        ]

        response = client.get('/reports/connect-goals')
        assert response.status_code == 200
        assert b'Whitespace Customer' in response.data
        assert f'/customer/{customer.id}'.encode() in response.data
        assert b'data:image/png;base64,ZmFrZQ==' in response.data
        assert b'Fabric monthly ACR' not in response.data
        assert b'Feb26' in response.data
        assert b'First positive' not in response.data


def test_whitespace_history_centers_or_shifts_eight_month_window():
    """History centers the trigger unless unavailable future months shift it back."""
    centered = connect_goals._whitespace_history_bounds(
        date(2026, 7, 1),
        date(2026, 1, 1),
        date(2026, 11, 1),
    )
    current_month = connect_goals._whitespace_history_bounds(
        date(2026, 9, 1),
        date(2026, 1, 1),
        date(2026, 9, 1),
    )

    assert centered == (date(2026, 2, 1), date(2026, 9, 1))
    assert current_month == (date(2026, 2, 1), date(2026, 9, 1))


def test_whitespace_win_requires_complete_global_history(app):
    """Positive ACR does not count when any lookback month is absent."""
    with app.app_context():
        customer = Customer(name='New History Customer', tpid='history-123')
        imported = RevenueImport(filename='incomplete-whitespace-test')
        db.session.add_all([customer, imported])
        db.session.flush()
        db.session.add(CustomerRevenueData(
            customer_name=customer.name,
            tpid=customer.tpid,
            customer_id=customer.id,
            bucket='Databases',
            fiscal_month='FY27-Sep',
            month_date=date(2026, 9, 1),
            revenue=250,
            last_import_id=imported.id,
        ))
        db.session.commit()

        widget = connect_goals.get_whitespace_widget(date(2026, 9, 23))

        databases = next(
            item for item in widget['bucket_progress']
            if item['bucket'] == 'Databases'
        )
        assert databases['wins'] == 0


def test_connect_goals_api_serializes_shared_data(client):
    """The JSON endpoint should serialize setup and widget data."""
    response = client.get('/api/reports/connect-goals')

    assert response.status_code == 200
    assert set(response.get_json()) == {'scope', 'setup', 'widgets'}


def test_u2c_widget_uses_data_snapshot(app):
    """U2C widget uses the official Data-scoped snapshot and stored trend."""
    with app.app_context():
        customer = Customer(name='U2C Customer', tpid='u2c-123')
        db.session.add(customer)
        db.session.flush()
        milestone = Milestone(
            msx_milestone_id='msx-u2c',
            milestone_number='7-U2C',
            url='https://example.test/u2c',
            title='U2C milestone',
            msx_status='On Track',
            customer_commitment='Committed',
            due_date=datetime(2026, 9, 15),
            workload='Data: Fabric',
            monthly_usage=100,
            on_my_team=True,
            customer_id=customer.id,
        )
        snapshot = U2CSnapshot(
            fiscal_quarter='FY27 Q1',
            snapshot_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
            total_items=1,
            total_monthly_acr=100,
        )
        db.session.add_all([milestone, snapshot])
        db.session.flush()
        db.session.add(U2CSnapshotItem(
            snapshot_id=snapshot.id,
            milestone_id=milestone.id,
            customer_id=customer.id,
            customer_name=customer.name,
            milestone_title=milestone.title,
            milestone_number=milestone.milestone_number,
            workload=milestone.workload,
            due_date=milestone.due_date,
            monthly_acr=100,
            msx_status='On Track',
        ))
        version = U2CSnapshotVersion(
            snapshot_id=snapshot.id,
            msxi_version='20260811',
            version_date=date(2026, 8, 11),
            total_items=1,
            total_starting_acr=100,
            total_committed_acr=100,
            total_converted_acr=100,
        )
        db.session.add(version)
        db.session.flush()
        db.session.add(U2CSnapshotVersionItem(
            version_id=version.id,
            milestone_number=milestone.milestone_number,
            workload=milestone.workload,
            starting_acr=100,
            converted_acr=100,
            commitment='Committed',
            status='On Track',
        ))
        db.session.commit()

        widget = connect_goals.get_u2c_widget(date(2026, 8, 15))

        assert widget['available'] is True
        assert widget['value'] == 100.0
        assert widget['starting_acr'] == 100
        assert widget['trend'][0]['u2c_pct'] == 100.0
