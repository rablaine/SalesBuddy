"""Tests for FY27 Data Connect Goals calculations."""
from datetime import date, datetime, timezone
import json
from unittest.mock import Mock

from bs4 import BeautifulSoup
import pytest

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


def _goals_dashboard_page(client) -> BeautifulSoup:
    """Render a normal dashboard copied from the Connect Goals template."""
    response = client.post('/api/reports/dashboards/from-template/connect-goals', json={})
    assert response.status_code == 201
    return BeautifulSoup(
        client.get(f'/reports/dashboards/{response.json["id"]}').data, 'html.parser',
    )


def _confirm_scope() -> None:
    """Confirm FY27 Databases and Fabric priorities for a test."""
    preference = UserPreference.query.first()
    preference.compensated_buckets = json.dumps(['Databases', 'Fabric'])
    preference.compensated_buckets_fiscal_year = 'FY27'
    preference.compensated_buckets_confirmed_taxonomy_version = (
        preference.bucket_taxonomy_version
    )


def _seed_revenue_buckets(customer_id: int | None = None) -> None:
    """Seed example revenue buckets used by the Data goal report."""
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


def test_team_and_hva_share_one_scoped_milestone_load(app, monkeypatch):
    """Reuse the population, not results from a different customer selection."""
    from app.services.request_cache import shared_read_scope

    with app.app_context():
        customer = Customer(name='Shared milestone scope', tpid=98920101)
        db.session.add(customer)
        db.session.flush()
        db.session.add_all([
            _milestone(customer.id, number='SHARED-TEAM', on_team=True, monthly_usage=100),
            _milestone(customer.id, number='SHARED-OFF', on_team=False, monthly_usage=300),
        ])
        db.session.commit()
        reference, scope = date(2026, 10, 21), (customer.id,)
        expected = [
            provider(reference, customer_ids=scope) for provider in (
                connect_goals.get_team_coverage_widget, connect_goals.get_hva_coverage_widget,
            )
        ]
        query = Mock(wraps=connect_goals._milestone_scope)
        monkeypatch.setattr(connect_goals, '_milestone_scope', query)
        with app.test_request_context(), shared_read_scope():
            assert connect_goals.get_team_coverage_widget(
                reference, customer_ids=scope,
            ) == expected[0]
            assert connect_goals.get_hva_coverage_widget(
                reference, customer_ids=scope,
            ) == expected[1]
            assert query.call_count == 1
            assert connect_goals.get_hva_coverage_widget(
                reference, customer_ids=(),
            )['total_count'] == 0
            assert query.call_count == 2
        with app.test_request_context(), shared_read_scope():
            assert connect_goals.get_team_coverage_widget(
                reference, customer_ids=scope,
            ) == expected[0]
        assert query.call_count == 3


@pytest.mark.parametrize('selected', [
    ['Databases'], ['Fabric'], ['Infra'], ['Infra', 'AI'],
])
def test_setup_accepts_any_confirmed_compensation_buckets(
    client, app, selected, dashboard_sources_ready,
):
    """Compensation choices need not contain either of the Data report's goal buckets."""
    with app.app_context():
        imported = RevenueImport(filename='setup-test')
        db.session.add(imported)
        db.session.flush()
        for bucket in selected:
            db.session.add(CustomerRevenueData(
                customer_name='Setup Customer',
                bucket=bucket,
                fiscal_month='FY27-Jul',
                month_date=date(2026, 7, 1),
                revenue=1,
                last_import_id=imported.id,
            ))
        db.session.commit()

    response = client.post('/api/revenue/compensated-buckets', json={
        'buckets': selected, 'confirm_for_fiscal_year': True,
    })
    assert response.status_code == 200
    with app.app_context():
        state = connect_goals.get_setup_state(date(2026, 9, 23))
        assert state['ready'] is True
        assert state['selected_buckets'] == selected
        assert state['reasons'] == []
        assert json.loads(UserPreference.query.first().compensated_buckets) == selected
        preference = UserPreference.query.first()
        assert preference.compensated_buckets_fiscal_year == state['fiscal_year']
        assert preference.compensated_buckets_confirmed_taxonomy_version == (
            preference.bucket_taxonomy_version
        )
    page = _goals_dashboard_page(client)
    assert len(page.select('.db-widget .sb-goals-section')) == 4
    assert page.select_one('[data-widget-requirement]') is None
    picker = BeautifulSoup(client.get(
        '/api/reports/dashboards/requirements/compensation_buckets',
    ).json['html'], 'html.parser')
    assert {item['value'] for item in picker.select('input:checked')} == (
        set(selected)
    )


def test_connect_goals_dashboard_shows_widget_setup_gate(client, dashboard_sources_ready):
    """The template uses widget-level setup rather than rendering fake values."""
    page = _goals_dashboard_page(client)
    assert len(page.select('.db-widget [data-widget-requirement]')) == 4
    assert 'Confirm your FY27 compensation buckets' in page.get_text()


@pytest.mark.parametrize('specialty', ['data', 'infra', 'apps_ai', 'other'])
def test_seller_setup_does_not_require_se_choices(
    client, app, specialty, dashboard_sources_ready,
):
    """Sellers use all available buckets regardless of a saved SE specialty or stale scope."""
    with app.app_context():
        pref = UserPreference.query.first()
        original = (
            pref.user_role, pref.se_specialty, pref.compensated_buckets,
            pref.compensated_buckets_fiscal_year,
            pref.compensated_buckets_confirmed_taxonomy_version,
        )
        pref.user_role = 'dss'
        pref.se_specialty = specialty
        pref.compensated_buckets = json.dumps(['Retired bucket'])
        pref.compensated_buckets_fiscal_year = 'FY26'
        pref.compensated_buckets_confirmed_taxonomy_version = None
        db.session.commit()
        try:
            setup = connect_goals.get_setup_state(date(2026, 10, 23))
            assert setup['ready'] is True
            assert setup['reasons'] == []
            assert setup['selected_buckets'] == setup['available_buckets']
            assert pref.compensated_buckets == json.dumps(['Retired bucket'])
            assert pref.se_specialty == specialty
            assert client.get(
                '/api/reports/dashboards/requirements/compensation_buckets',
            ).json['setup']['ready'] is True
            page = _goals_dashboard_page(client)
            assert page.select_one('[data-widget-requirement]') is None
            assert len(page.select('.db-widget .sb-goals-section')) == 4
        finally:
            (
                pref.user_role, pref.se_specialty, pref.compensated_buckets,
                pref.compensated_buckets_fiscal_year,
                pref.compensated_buckets_confirmed_taxonomy_version,
            ) = original
            db.session.commit()


def test_connect_goals_template_renders_action_metrics(client, app, dashboard_sources_ready):
    """The normal dashboard preserves all shared metric and drill-through content."""
    with app.app_context():
        _seed_revenue_buckets()
        _confirm_scope()
        db.session.commit()

    page = _goals_dashboard_page(client)
    headings = [
        heading.get_text(strip=True)
        for heading in page.select('.db-widget h2')
    ]
    assert headings == [
        'Quarterly U2C progression', 'Milestone team coverage',
        'Milestone HVA coverage', 'Fabric and database whitespace wins',
    ]
    assert len(page.select('.sb-goals-section:not(.is-supporting)')) == 3
    assert page.select_one('.sb-goals-section.is-supporting')
    assert not page.select('.sb-connect-goals [data-bs-toggle], .sb-connect-goals .btn')
    assert page.select_one('body.sb-page-frame')
    assert page.select_one('.db-dashboard-viewport > [data-sb-page-surface]')
    assert page.select_one('#dashboardReading[data-sb-page-body]')
    assert page.select_one('body > footer.sb-page-status')['aria-label'] == 'Dashboard status'
    assert not page.select('#main-content main')
    config = next(
        json.loads(script.string) for script in page.select('.db-canvas script')
        if json.loads(script.string).get('teamFilter')
    )
    assert config['teamFilter']['area'] == ['Data']
    assert config['teamFilter']['myTeam'] == 'off'
    assert config['teamFilter']['quarters'] == [
        'FY27 Q1', 'FY27 Q2', 'FY27 Q3', 'FY27 Q4',
        'FY28 Q1', 'FY28 Q2', 'FY28 Q3', 'FY28 Q4',
    ]
    assert config['teamFilter']['status'] == list(connect_goals.ACTIVE_MILESTONE_STATUSES)
    assert config['teamFilter']['favoritesOnly'] is False
    assert 'No matched Databases or Fabric revenue history is available.' in page.get_text()


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


def test_hva_coverage_and_validation_counts(app):
    """HVA coverage respects the scope and reports validation categories."""
    with app.app_context():
        customer = Customer(name='HVA Customer', tpid='hva-123')
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
            is_hva=True,
            due_date=datetime(2026, 8, 1),
            milestone_id=covered.id,
        ))
        db.session.commit()

        widget = connect_goals.get_hva_coverage_widget(date(2026, 9, 23))

        assert widget['value'] == 50.0
        assert widget['covered'] == 1
        assert widget['uncovered'] == 1
        assert widget['validation_counts']['Demo'] == 1


def test_whitespace_win_requires_sustained_consumption(app, client):
    """Three positive months confirm a win after three complete zero months."""
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
            (date(2026, 7, 1), 20),
            (date(2026, 8, 1), 75),
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
        assert win['activation_month'] == '2026-07-01'
        assert win['confirmation_month'] == '2026-09-01'
        assert win['positive_months'] == 3
        assert win['state'] == 'confirmed'
        assert [point['label'] for point in win['history']] == [
            'Feb26', 'Mar26', 'Apr26', 'May26',
            'Jun26', 'Jul26', 'Aug26', 'Sep26',
        ]
        assert [point['revenue'] for point in win['history']] == [
            0, 0, 0, 0, 0, 20, 75, 250,
        ]

        page = _goals_dashboard_page(client)
        assert 'Whitespace Customer' in page.get_text()
        assert page.select_one(f'a[href="/customer/{customer.id}"]')
        assert page.select_one('img[src="data:image/png;base64,ZmFrZQ=="]')
        assert 'Fabric monthly ACR' not in page.get_text()
        assert 'Feb26' in page.get_text()
        assert 'First positive' not in page.get_text()
        assert 'Sustained' in page.get_text()
        history = page.select_one('.sb-goals-history-table')
        assert len(history.select('td.is-danger')) == 5
        assert len(history.select('td.is-success')) == 3
        assert not page.select('.sb-connect-goals .text-success, .sb-connect-goals .text-danger')
        assert page.select_one('[data-goals-history-key]')['data-goals-history-key'] == (
            f'Fabric:{customer.id}'
        )


def test_whitespace_tracks_ramping_and_excludes_lapsed_activation(app):
    """Current activations ramp while a three-month run that stops does not count."""
    with app.app_context():
        ramping_customer = Customer(name='Ramping Customer', tpid='ramping-123')
        lapsed_customer = Customer(name='Lapsed Customer', tpid='lapsed-123')
        imported = RevenueImport(filename='ramping-whitespace-test')
        db.session.add_all([ramping_customer, lapsed_customer, imported])
        db.session.flush()

        ramping_values = [0, 0, 0, 0, 0, 0, 125]
        lapsed_values = [0, 0, 0, 50, 75, 100, 0]
        months = [
            date(2026, month, 1)
            for month in range(4, 11)
        ]
        for customer, values in (
            (ramping_customer, ramping_values),
            (lapsed_customer, lapsed_values),
        ):
            for month, revenue in zip(months, values):
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
        db.session.commit()

        widget = connect_goals.get_whitespace_widget(date(2026, 10, 23))

        fabric = next(
            item for item in widget['bucket_progress']
            if item['bucket'] == 'Fabric'
        )
        assert fabric['wins'] == 0
        assert fabric['ramping'] == 1
        assert widget['wins']['Fabric'] == []
        assert len(widget['ramping']['Fabric']) == 1
        candidate = widget['ramping']['Fabric'][0]
        assert candidate['customer_name'] == 'Ramping Customer'
        assert candidate['positive_months'] == 1
        assert candidate['state_label'] == 'Ramping · 1 of 3 months'


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


def test_ready_connect_goals_api_has_four_shared_widgets(client, app):
    """The page, API and tool provider share the same four measurable goals."""
    with app.app_context():
        _seed_revenue_buckets()
        _confirm_scope()
        db.session.commit()

    data = client.get('/api/reports/connect-goals').get_json()
    assert data['setup']['ready'] is True
    assert [widget['key'] for widget in data['widgets']] == [
        'u2c', 'team_coverage', 'hva_coverage', 'whitespace',
    ]


def test_setup_form_uses_native_scope_choices(client, app):
    """Annual setup offers available buckets without marking any specific choice mandatory."""
    with app.app_context():
        _seed_revenue_buckets()
        db.session.commit()

    response = client.get('/api/reports/dashboards/requirements/compensation_buckets')
    page = BeautifulSoup(response.json['html'], 'html.parser')
    assert {item['value'] for item in page.select('input[type="checkbox"]')} == {
        'Databases', 'Fabric',
    }
    assert {item.get_text(' ', strip=True) for item in page.select('label')} == {
        'Databases', 'Fabric',
    }
    assert response.json['setup']['ready'] is False


def test_connect_goals_widgets_do_not_own_the_dashboard_shell(client, app):
    """Widget fragments leave window and footer ownership to their dashboard."""
    with app.app_context():
        _seed_revenue_buckets()
        _confirm_scope()
        db.session.commit()

    page = _goals_dashboard_page(client)
    assert all(widget['data-embedded'] == 'true' for widget in page.select('.sb-connect-goals'))
    assert len(page.select('body > footer.sb-page-status')) == 1


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
            total_committed_acr=0,
            total_converted_acr=0,
        )
        db.session.add(version)
        db.session.flush()
        db.session.add(U2CSnapshotVersionItem(
            version_id=version.id,
            milestone_number=milestone.milestone_number,
            workload=milestone.workload,
            starting_acr=100,
            converted_acr=0,
            commitment='Uncommitted',
            status='On Track',
        ))
        from app.services.u2c_snapshot import record_version_totals
        record_version_totals(snapshot, '20260815', [{
            'milestone_number': milestone.milestone_number,
            'workload': milestone.workload,
            'starting_acr': 100,
            'current_commitment': 'Uncommitted',
            'current_status': 'On Track',
        }])
        db.session.commit()

        widget = connect_goals.get_u2c_widget(date(2026, 8, 15))

        assert widget['available'] is True
        assert widget['value'] == 100.0
        assert widget['starting_acr'] == 100
        assert len(widget['trend']) == 3
        assert widget['trend'][0]['u2c_pct'] == 0.0
        assert widget['trend'][-2]['date'] == '2026-08-15'
        assert widget['trend'][-2]['u2c_pct'] == 0.0
        assert widget['trend'][-1]['u2c_pct'] == widget['value'] == 100.0
        assert widget['trend'][-1]['date'] == '2026-08-15'
        assert widget['trend'][-1]['source'] == 'live'
        assert widget['trend'][-1]['label'] == 'Today (Aug 15, live)'
        assert version.total_committed_acr == 0


def test_u2c_widget_limits_history_to_current_quarter(app, monkeypatch):
    """Exclude other-quarter publishes without dropping either quarter boundary."""
    with app.app_context():
        snapshot = U2CSnapshot(
            fiscal_quarter='FY27 Q2',
            snapshot_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
            total_items=0,
            total_monthly_acr=0,
        )
        db.session.add(snapshot)
        db.session.commit()
        points = [
            {'date': '2026-08-04', 'label': 'Aug 04', 'u2c_pct': 10.0},
            {'date': '2026-09-30', 'label': 'Sep 30', 'u2c_pct': 20.0},
            {'date': '2026-10-01', 'label': 'Oct 01', 'u2c_pct': 0.0},
            {'date': '2026-10-06', 'label': 'Oct 06 (live)', 'u2c_pct': 0.0,
             'source': 'live'},
            {'date': '2026-12-31', 'label': 'Dec 31', 'u2c_pct': 40.0},
            {'date': '2027-01-01', 'label': 'Jan 01', 'u2c_pct': 50.0},
        ]
        monkeypatch.setattr(connect_goals, 'get_attainment_trend', lambda *a, **kw: points)

        widget = connect_goals.get_u2c_widget(date(2026, 10, 6))

        assert widget['period_start'] == '2026-10-01'
        assert widget['period_end'] == '2026-12-31'
        assert widget['trend'] == points[2:5]
        assert widget['value'] == 0.0
        assert len(points) == 6
