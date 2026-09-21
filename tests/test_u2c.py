"""Tests for U2C snapshot service and report."""
import pytest
from datetime import datetime, timezone, date, timedelta

from app.models import (
    db, Customer, Engagement, Milestone, OneOnOneAgendaItem, OneOnOneWorkspace,
    Opportunity, Seller, Territory, U2CSnapshot, U2CSnapshotItem,
    U2CSnapshotVersion,
)
from app.services.u2c_snapshot import (
    current_fiscal_quarter, fiscal_quarter_date_range,
    get_attainment, get_workload_prefixes, import_official_snapshot,
)


def make_snapshot(fq_label=None):
    """Build a snapshot from locally synced milestones. Test scaffolding only.

    Production code always sources snapshots from the MSXi pull now, but the
    attainment tests care about the attainment maths rather than where the rows
    came from, so this keeps them focused and independent of the pull.
    """
    fq = fq_label or current_fiscal_quarter()
    milestones = (
        Milestone.query
        .join(Opportunity, Milestone.opportunity_id == Opportunity.id)
        .filter(
            Milestone.customer_commitment == 'Uncommitted',
            Milestone.msx_status.in_(['On Track', 'At Risk', 'Blocked']),
            Opportunity.statecode == 0,
        )
        .all()
    )

    snapshot = U2CSnapshot(
        fiscal_quarter=fq,
        snapshot_date=datetime.now(timezone.utc),
        source=U2CSnapshot.SOURCE_MSXI,
    )
    db.session.add(snapshot)
    db.session.flush()

    total_acr = 0.0
    for ms in milestones:
        acr = ms.monthly_usage or 0.0
        db.session.add(U2CSnapshotItem(
            snapshot_id=snapshot.id,
            milestone_id=ms.id,
            customer_id=ms.customer_id,
            customer_name=ms.customer.name if ms.customer else 'Unknown',
            milestone_title=ms.title or ms.milestone_number or 'Untitled',
            milestone_number=ms.milestone_number,
            workload=ms.workload,
            due_date=ms.due_date,
            monthly_acr=acr,
            opportunity_name=ms.opportunity.name if ms.opportunity else None,
            msx_status=ms.msx_status,
        ))
        total_acr += acr

    snapshot.total_items = len(milestones)
    snapshot.total_monthly_acr = round(total_acr, 2)
    db.session.commit()
    return {'snapshot_id': snapshot.id, 'fiscal_quarter': fq,
            'total_items': len(milestones),
            'total_monthly_acr': round(total_acr, 2)}


@pytest.fixture
def u2c_data(app):
    """Create milestones on open opportunities for U2C snapshot testing."""
    with app.app_context():
        seller = Seller(name='U2C Seller', alias='u2cseller')
        customer = Customer(name='Test Corp', tpid=9999, seller=seller)
        db.session.add_all([seller, customer])
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
            'seller_id': seller.id,
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


class TestSnapshotScaffolding:
    """Sanity checks on the test scaffolding the attainment tests build on."""

    def test_snapshot_captures_uncommitted(self, app, u2c_data):
        """Only uncommitted milestones on open opps are in scope."""
        with app.app_context():
            result = make_snapshot()
            assert result['total_items'] == 3  # ms1, ms2, ms3
            # ms4 (committed) and ms5 (closed opp) excluded
            assert result['total_monthly_acr'] == 10000.0  # 5000+3000+2000

    def test_snapshot_stores_frozen_data(self, app, u2c_data):
        """Snapshot items should store milestone data at snapshot time."""
        with app.app_context():
            make_snapshot()
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
            make_snapshot()
            item = U2CSnapshotItem.query.first()
            assert item.customer_name == 'Test Corp'

    def test_one_snapshot_per_quarter(self, app, u2c_data):
        """The unique constraint is what makes refresh-in-place safe."""
        with app.app_context():
            make_snapshot()
            with pytest.raises(Exception):
                make_snapshot()
            db.session.rollback()


class TestAttainment:
    """Test U2C attainment calculation."""

    def test_attainment_all_uncommitted(self, app, u2c_data):
        """When nothing is committed, attainment should be 0%."""
        with app.app_context():
            make_snapshot()
            snapshot = U2CSnapshot.query.first()
            result = get_attainment(snapshot.id)
            assert result['success'] is True
            assert result['attainment_pct'] == 0.0
            assert result['committed_count'] == 0
            assert result['remaining_count'] == 3

    def test_attainment_after_commit(self, app, u2c_data):
        """Committing a milestone should increase attainment."""
        with app.app_context():
            make_snapshot()
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
            make_snapshot()
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
            make_snapshot()
            snapshot = U2CSnapshot.query.first()

            result = get_attainment(snapshot.id, workload_prefix='Data')
            # Only ms1 and ms2 are Data: workloads
            assert result['total_in_scope'] == 2
            assert result['target_total'] == 8000.0  # 5000+3000

    def test_remaining_sorted_by_acr(self, app, u2c_data):
        """Remaining items should be sorted by monthly ACR descending."""
        with app.app_context():
            make_snapshot()
            snapshot = U2CSnapshot.query.first()
            result = get_attainment(snapshot.id)
            acrs = [i['monthly_acr'] for i in result['remaining_items']]
            assert acrs == sorted(acrs, reverse=True)

    def test_attainment_includes_linked_engagements(self, app, u2c_data):
        """Remaining rows should identify every engagement linked to a milestone."""
        with app.app_context():
            milestone = db.session.get(Milestone, u2c_data['ms1_id'])
            engagement = Engagement(
                customer=milestone.customer,
                title='Fabric adoption',
                status='Active',
            )
            engagement.milestones.append(milestone)
            db.session.add(engagement)
            db.session.commit()
            make_snapshot()

            result = get_attainment(U2CSnapshot.query.first().id)
            fabric = next(
                item for item in result['remaining_items']
                if item['milestone_id'] == milestone.id
            )

            assert fabric['engagements'] == [{
                'id': engagement.id,
                'title': 'Fabric adoption',
                'status': 'Active',
            }]


class TestWorkloadPrefixes:
    """Test workload prefix extraction."""

    def test_get_workload_prefixes(self, app, u2c_data):
        """Should return distinct workload prefixes."""
        with app.app_context():
            make_snapshot()
            snapshot = U2CSnapshot.query.first()
            prefixes = get_workload_prefixes(snapshot.id)
            assert 'Data' in prefixes
            assert 'Infra' in prefixes


class TestReportRoute:
    """Test the U2C report web route."""

    def test_report_page_loads(self, client):
        """Report page should load without errors."""
        response = client.get('/reports/u2c')
        assert response.status_code == 200
        assert b'U2C Attainment' in response.data

    def test_report_shows_the_msxi_version_it_is_displaying(
        self, client, app, monkeypatch,
    ):
        """Users need to see the data date, not just when we last checked."""
        from app.services.u2c_snapshot import refresh_official_snapshot

        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 5000.0}).install(monkeypatch)
        with app.app_context():
            refresh_official_snapshot()

        response = client.get('/reports/u2c')
        assert response.status_code == 200
        assert b'MSXi data as of Sep 15, 2026' in response.data

    def test_report_no_longer_offers_local_snapshots(self, client):
        """Local snapshots and their stale-sync gate are gone."""
        response = client.get('/reports/u2c')
        assert response.status_code == 200
        assert b'Take Local' not in response.data
        assert b'Stale local milestone import' not in response.data

    def test_report_surfaces_automatic_refresh_failure(self, client, app):
        """A failed background pull should not be hidden from the user."""
        import json
        from app.models import SyncStatus

        with app.app_context():
            SyncStatus.mark_started('u2c_import')
            SyncStatus.mark_completed(
                'u2c_import',
                success=False,
                details=json.dumps({'error': 'MSXi schema changed'}),
            )

        response = client.get('/reports/u2c')
        assert response.status_code == 200
        assert b'Automatic MSXi refresh failed.' in response.data
        assert b'MSXi schema changed' in response.data

    def test_report_with_snapshot(self, client, app, u2c_data):
        """Report should show attainment when a snapshot exists."""
        with app.app_context():
            make_snapshot()
        fq = current_fiscal_quarter()
        response = client.get(f'/reports/u2c?fq={fq}')
        assert response.status_code == 200
        assert b'Attainment' in response.data
        assert b'id="u2cFilterBar"' in response.data
        assert b'id="u2cFilterEmpty"' in response.data
        assert response.data.index(b'id="u2cFilterBar"') < response.data.index(
            b'id="remainingCard"')

    def test_report_shows_engagement_and_one_on_one_action(
        self, client, app, u2c_data
    ):
        """Remaining milestones should show engagement context and agenda actions."""
        with app.app_context():
            milestone = db.session.get(Milestone, u2c_data['ms1_id'])
            engagement = Engagement(
                customer=milestone.customer,
                title='Fabric adoption',
                status='Active',
            )
            engagement.milestones.append(milestone)
            db.session.add(engagement)
            db.session.commit()
            make_snapshot()

        fq = current_fiscal_quarter()
        response = client.get(f'/reports/u2c?fq={fq}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'Linked: Fabric adoption' in html
        assert '>Eng</th>' in html
        assert 'No linked engagement' in html
        assert '<i class="bi bi-person-plus"></i>\n                                Add' in html
        assert "Add to U2C Seller's 1:1 notes" in html
        assert (
            f"/api/seller/{u2c_data['seller_id']}/one-on-one/u2c" in html
        )

    def test_report_marks_context_already_on_one_on_one(
        self, client, app, u2c_data
    ):
        """Rows should link to a seller workspace when all targets are active."""
        with app.app_context():
            make_snapshot()
            workspace = OneOnOneWorkspace(
                seller_id=u2c_data['seller_id'],
                person_name='U2C Seller',
                person_type='Seller',
            )
            item = OneOnOneAgendaItem(
                workspace=workspace,
                item_type='milestone',
                milestone_id=u2c_data['ms1_id'],
                title_snapshot='Deploy Fabric',
                customer_snapshot='Test Corp',
            )
            db.session.add_all([workspace, item])
            db.session.commit()

        response = client.get(
            f'/reports/u2c?fq={current_fiscal_quarter()}'
        )

        assert response.status_code == 200
        assert b'On 1:1' in response.data

    def test_create_snapshot_endpoint_is_gone(self, client, app, u2c_data):
        """The local snapshot API was removed along with the feature."""
        response = client.post(
            '/api/reports/u2c/create-snapshot',
            json={},
            content_type='application/json',
        )
        assert response.status_code == 404


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
            make_snapshot()
            from app.services.salesiq_tools import execute_tool
            result = execute_tool('get_u2c_attainment', {})
            assert result['success'] is True
            assert 'attainment_pct' in result


class TestMsxiQuarterLabels:
    """Test translating Sales Buddy FQ labels into MSXi filter labels."""

    def test_translates_label(self):
        from app.services.u2c_pull import msxi_quarter_labels
        assert msxi_quarter_labels('FY27 Q1') == ('FY27', 'FY27-Q1')

    def test_accepts_hyphenated_label(self):
        from app.services.u2c_pull import msxi_quarter_labels
        assert msxi_quarter_labels('fy26-q4') == ('FY26', 'FY26-Q4')

    def test_rejects_garbage(self):
        from app.services.u2c_pull import U2CPullError, msxi_quarter_labels
        with pytest.raises(U2CPullError):
            msxi_quarter_labels('not a quarter')


class TestU2CQueryShape:
    """Test the semantic query sent to Power BI."""

    def test_query_filters_on_requested_territories(self):
        from app.services.u2c_pull import _u2c_query
        query = _u2c_query(['East.SMECC.MAA.0101'], 'FY27', 'FY27-Q1')
        literals = []

        def walk(node):
            if isinstance(node, dict):
                if 'Literal' in node and isinstance(node['Literal'], dict):
                    literals.append(node['Literal'].get('Value'))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(query['Where'])
        assert "'East.SMECC.MAA.0101'" in literals
        assert "'FY27'" in literals
        assert "'FY27-Q1'" in literals
        # The baseline is the uncommitted set at the start of the quarter.
        assert "'Uncommitted'" in literals

    def test_query_selects_expected_columns(self):
        from app.services.u2c_pull import _u2c_query
        query = _u2c_query(['T1'], 'FY27', 'FY27-Q1')
        names = [s['Name'] for s in query['Select']]
        for expected in ('customer_name', 'milestone_name', 'milestone_number',
                         'starting_acr', 'converted_acr', 'starting_due_date',
                         'current_commitment', 'current_status'):
            assert expected in names

    def test_escapes_quotes_in_territory_names(self):
        from app.services.u2c_pull import _lit
        assert _lit("O'Brien") == "'O''Brien'"


class TestDsrDecode:
    """Test the Power BI DSR decoder against the report's response shape."""

    def test_decodes_detail_rows_not_subtotal(self):
        from app.services.u2c_pull import _decode
        data = {
            'descriptor': {'Select': [
                {'Value': 'G0', 'Name': 'customer_name'},
                {'Value': 'M0', 'Name': 'starting_acr'},
            ]},
            'dsr': {'DS': [{
                'PH': [
                    {'DM0': [{'S': [{'N': 'M0', 'T': 3}], 'C': [900]}]},
                    {'DM1': [
                        {'S': [{'N': 'G0', 'T': 1, 'DN': 'D0'}, {'N': 'M0', 'T': 3}],
                         'C': [0, 500]},
                        {'C': [1, 400]},
                    ]},
                ],
                'ValueDicts': {'D0': ['Acme', 'Globex']},
            }]},
        }
        rows = _decode(data)
        assert rows == [
            {'customer_name': 'Acme', 'starting_acr': 500},
            {'customer_name': 'Globex', 'starting_acr': 400},
        ]

    def test_repeats_previous_value_for_reuse_bitmask(self):
        from app.services.u2c_pull import _decode
        data = {
            'descriptor': {'Select': [
                {'Value': 'G0', 'Name': 'customer_name'},
                {'Value': 'M0', 'Name': 'starting_acr'},
            ]},
            'dsr': {'DS': [{
                'PH': [{'DM0': [
                    {'S': [{'N': 'G0', 'T': 1, 'DN': 'D0'}, {'N': 'M0', 'T': 3}],
                     'C': [0, 500]},
                    {'C': [400], 'R': 1},
                ]}],
                'ValueDicts': {'D0': ['Acme']},
            }]},
        }
        rows = _decode(data)
        assert rows[1]['customer_name'] == 'Acme'
        assert rows[1]['starting_acr'] == 400

    def test_empty_payload_returns_no_rows(self):
        from app.services.u2c_pull import _decode
        assert _decode({}) == []


def _msxi_row(**overrides):
    """Build one MSXi U2C row with sensible defaults."""
    q_start, _ = fiscal_quarter_date_range(current_fiscal_quarter())
    row = {
        'customer_name': 'Test Corp',
        'milestone_name': 'Deploy Fabric',
        'milestone_number': 'MS-1',
        'opportunity_number': 'OPP-1',
        'owner_alias': 'someone',
        'starting_commitment': 'Uncommitted',
        'current_commitment': 'Uncommitted',
        'starting_status': 'On Track',
        'current_status': 'On Track',
        'starting_due_date': q_start + timedelta(days=45),
        'current_due_date': q_start + timedelta(days=45),
        'starting_acr': 5000.0,
        'converted_acr': 0.0,
        # MSXi classifies every row itself - this is what the report filters on.
        'workload': 'Data: Analytics - Fabric - New Analytics',
    }
    row.update(overrides)
    return row


@pytest.fixture
def official_rows(app):
    """Patch the MSXi pull so the import runs without network access."""
    rows = [
        _msxi_row(),
        _msxi_row(milestone_name='Migrate SQL', milestone_number='MS-2',
                  starting_acr=3000.0, converted_acr=3000.0,
                  current_commitment='Committed'),
        _msxi_row(customer_name='Unknown Account', milestone_name='Other Team Work',
                  milestone_number='MS-999', opportunity_number='OPP-999',
                  owner_alias='someoneelse', starting_acr=1000.0),
    ]
    import app.services.u2c_pull as pull_module
    original = pull_module.pull_u2c_milestones
    pull_module.pull_u2c_milestones = (
        lambda fq, territories=None, version=None: rows
    )
    yield rows
    pull_module.pull_u2c_milestones = original


class TestImportOfficialSnapshot:
    """Test importing the official MSXi U2C baseline."""

    def test_import_creates_official_snapshot(self, app, u2c_data, official_rows):
        with app.app_context():
            result = import_official_snapshot()
            assert result['success'] is True
            assert result['total_items'] == 3
            assert result['total_monthly_acr'] == 9000.0

            snapshot = U2CSnapshot.query.filter_by(
                fiscal_quarter=current_fiscal_quarter()).first()
            assert snapshot.source == U2CSnapshot.SOURCE_MSXI
            assert snapshot.content_fingerprint

    def test_import_links_matching_local_milestones(self, app, u2c_data, official_rows):
        with app.app_context():
            ms = Milestone.query.filter_by(title='Deploy Fabric').first()
            ms.milestone_number = 'MS-1'
            db.session.commit()
            ms_id = ms.id

            result = import_official_snapshot()
            assert result['matched_locally'] == 1
            assert result['unmatched'] == 2

            item = U2CSnapshotItem.query.filter_by(milestone_number='MS-1').first()
            assert item.milestone_id == ms_id

    def test_msxi_workload_classifies_unmatched_rows(self, app, u2c_data,
                                                     official_rows):
        """Rows with no local milestone must still be filterable by workload."""
        with app.app_context():
            import_official_snapshot()
            orphan = U2CSnapshotItem.query.filter_by(
                milestone_number='MS-999').first()
            assert orphan.milestone_id is None
            assert orphan.workload == 'Data: Analytics - Fabric - New Analytics'

    def test_msxi_workload_wins_over_the_local_value(self, app, u2c_data,
                                                     official_rows):
        """The U2C report is measured on MSXi's classification, not ours."""
        with app.app_context():
            ms = Milestone.query.filter_by(title='Deploy Fabric').first()
            ms.milestone_number = 'MS-1'
            ms.workload = 'Infra: Something Else'
            db.session.commit()

            import_official_snapshot()
            item = U2CSnapshotItem.query.filter_by(milestone_number='MS-1').first()
            assert item.workload == 'Data: Analytics - Fabric - New Analytics'

    def test_local_workload_is_the_fallback(self, app, u2c_data, monkeypatch):
        """If MSXi ever stops classifying a row, fall back to what we know."""
        import app.services.u2c_pull as pull_module

        rows = [_msxi_row(workload=None)]
        monkeypatch.setattr(
            pull_module, 'pull_u2c_milestones',
            lambda fq, territories=None, version=None: rows,
        )
        with app.app_context():
            ms = Milestone.query.filter_by(title='Deploy Fabric').first()
            ms.milestone_number = 'MS-1'
            db.session.commit()
            local_workload = ms.workload

            import_official_snapshot()
            item = U2CSnapshotItem.query.filter_by(milestone_number='MS-1').first()
            assert item.workload == local_workload

    def test_import_falls_back_to_customer_name_match(self, app, u2c_data, official_rows):
        with app.app_context():
            customer_id = Customer.query.filter_by(name='Test Corp').first().id
            import_official_snapshot()
            item = U2CSnapshotItem.query.filter_by(milestone_number='MS-2').first()
            assert item.customer_id == customer_id

            orphan = U2CSnapshotItem.query.filter_by(milestone_number='MS-999').first()
            assert orphan.customer_id is None
            assert orphan.owner_alias == 'someoneelse'

    def test_import_refuses_to_clobber_existing_snapshot(self, app, u2c_data,
                                                         official_rows):
        with app.app_context():
            make_snapshot()
            result = import_official_snapshot()
            assert result['success'] is False
            assert result['needs_replace'] is True
            assert result['existing_source'] == U2CSnapshot.SOURCE_MSXI

    def test_import_replaces_when_asked(self, app, u2c_data, official_rows):
        with app.app_context():
            snapshot_id = make_snapshot()['snapshot_id']
            db.session.add(U2CSnapshotVersion(
                snapshot_id=snapshot_id,
                msxi_version='20260701',
                version_date=date(2026, 7, 1),
                total_items=1,
                total_starting_acr=1000.0,
                total_committed_acr=0.0,
                total_converted_acr=0.0,
            ))
            db.session.commit()

            result = import_official_snapshot(replace=True)

            assert result['success'] is True
            assert result['replaced'] is True
            snapshot = U2CSnapshot.query.filter_by(
                fiscal_quarter=current_fiscal_quarter()).one()
            assert snapshot.id == snapshot_id
            # The replaced snapshot's items must not survive.
            assert U2CSnapshotItem.query.count() == 3
            # Weekly history is permanent, including versions MSXi has expired.
            assert U2CSnapshotVersion.query.filter_by(
                snapshot_id=snapshot_id,
                msxi_version='20260701',
            ).count() == 1

    def test_attainment_uses_msxi_state_for_unmatched_milestones(self, app, u2c_data,
                                                                 official_rows):
        with app.app_context():
            import_official_snapshot()
            snapshot = U2CSnapshot.query.filter_by(
                fiscal_quarter=current_fiscal_quarter()).first()
            attainment = get_attainment(snapshot.id)

            assert attainment['source'] == U2CSnapshot.SOURCE_MSXI
            assert attainment['target_total'] == 9000.0
            # MS-2 is Committed per MSXi even though we have no local milestone.
            assert attainment['committed_count'] == 1
            assert attainment['committed_total'] == 3000.0
            committed = attainment['committed_items'][0]
            assert committed['milestone_number'] == 'MS-2'
            assert committed['status_source'] == 'msxi'

    def test_import_surfaces_pull_errors(self, app, u2c_data, monkeypatch):
        with app.app_context():
            import app.services.u2c_pull as pull_module

            def boom(fq, territories=None, version=None):
                raise pull_module.U2CPullError('No territories configured.')

            monkeypatch.setattr(pull_module, 'pull_u2c_milestones', boom)
            result = import_official_snapshot()
            assert result['success'] is False
            assert 'No territories configured.' in result['error']


class TestImportOfficialRoute:
    """Test the official import API endpoint."""

    def test_import_endpoint_returns_snapshot(self, client, app, u2c_data,
                                              official_rows):
        response = client.post('/api/reports/u2c/import-official', json={})
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['source'] == 'msxi'

    def test_import_endpoint_refreshes_existing_snapshot(
        self, client, app, u2c_data, official_rows,
    ):
        with app.app_context():
            make_snapshot()
        response = client.post('/api/reports/u2c/import-official', json={})
        assert response.status_code == 200
        assert response.get_json()['replaced'] is True

    def test_import_endpoint_records_sync_status(self, client, app, u2c_data,
                                                 official_rows):
        from app.models import SyncStatus

        with app.app_context():
            make_snapshot()
        response = client.post('/api/reports/u2c/import-official', json={})
        assert response.status_code == 200
        with app.app_context():
            status = SyncStatus.get_status('u2c_import')
            assert status['state'] == 'complete'

    def test_import_endpoint_reports_pull_failure(self, client, app, monkeypatch):
        import app.services.u2c_pull as pull_module

        def boom(fq, territories=None, version=None):
            raise pull_module.U2CPullError('gateway down')

        monkeypatch.setattr(pull_module, 'pull_u2c_milestones', boom)
        response = client.post('/api/reports/u2c/import-official', json={})
        assert response.status_code == 502
        assert 'gateway down' in response.get_json()['error']


class TestTerritoryCodes:
    """Test reading the territory scope for the official pull."""

    def test_returns_configured_territories(self, app):
        from app.services.u2c_pull import get_territory_codes
        with app.app_context():
            db.session.add(Territory(name='East.SMECC.MAA.0101'))
            db.session.add(Territory(name='East.SMECC.SOU.0207'))
            db.session.commit()
            assert get_territory_codes() == [
                'East.SMECC.MAA.0101', 'East.SMECC.SOU.0207',
            ]

    def test_pull_requires_territories(self, app):
        from app.services.u2c_pull import U2CPullError, pull_u2c_milestones
        with app.app_context():
            with pytest.raises(U2CPullError, match='No territories'):
                pull_u2c_milestones('FY27 Q1')


class TestScheduledU2CImport:
    """The daily scheduler hook that refreshes the official baseline."""

    def test_due_when_never_run(self, app):
        from app.services.scheduled_sync import _u2c_import_due
        with app.app_context():
            assert _u2c_import_due() is True

    def test_not_due_straight_after_a_success(self, app):
        from app.models import SyncStatus
        from app.services.scheduled_sync import _u2c_import_due
        with app.app_context():
            SyncStatus.mark_started('u2c_import')
            SyncStatus.mark_completed('u2c_import', success=True)
            assert _u2c_import_due() is False

    def test_failure_retries_sooner_than_a_success(self, app):
        """Off-VPN at boot is the common failure, and it self-heals quickly."""
        from app.models import SyncStatus, db as _db
        from app.services.scheduled_sync import _u2c_import_due
        with app.app_context():
            SyncStatus.mark_started('u2c_import')
            SyncStatus.mark_completed('u2c_import', success=False)
            status = SyncStatus.query.filter_by(sync_type='u2c_import').first()
            # Two hours ago: past the retry threshold, well short of the daily one.
            status.completed_at = datetime.now(timezone.utc).replace(
                tzinfo=None) - timedelta(hours=2)
            _db.session.commit()
            assert _u2c_import_due() is True

            status.success = True
            _db.session.commit()
            assert _u2c_import_due() is False

    def test_in_progress_is_not_due(self, app):
        from app.models import SyncStatus
        from app.services.scheduled_sync import _u2c_import_due
        with app.app_context():
            SyncStatus.mark_started('u2c_import')
            SyncStatus.update_heartbeat('u2c_import')
            assert _u2c_import_due() is False

    def test_sync_claim_is_atomic(self, app):
        from app.models import SyncStatus

        with app.app_context():
            assert SyncStatus.try_mark_started('u2c_import') is True
            assert SyncStatus.try_mark_started('u2c_import') is False
            SyncStatus.mark_completed('u2c_import', success=True)
            assert SyncStatus.try_mark_started('u2c_import') is True

    def test_process_lock_rejects_a_concurrent_refresh(self, app):
        from app.services.scheduled_sync import _u2c_process_lock

        with app.app_context():
            with _u2c_process_lock() as first:
                with _u2c_process_lock() as second:
                    assert first is True
                    assert second is False

    def test_long_running_import_renews_its_heartbeat(self, app, monkeypatch):
        import time
        from app.models import SyncStatus
        import app.services.scheduled_sync as scheduled

        heartbeats = []

        def slow_refresh():
            time.sleep(0.04)
            return {
                'success': True,
                'outcome': 'unchanged',
                'fiscal_quarter': current_fiscal_quarter(),
            }

        monkeypatch.setattr(
            scheduled, 'U2C_HEARTBEAT_INTERVAL_SECONDS', 0.01)
        monkeypatch.setattr(
            'app.services.u2c_snapshot.refresh_official_snapshot',
            slow_refresh,
        )
        monkeypatch.setattr(
            SyncStatus,
            'update_heartbeat',
            lambda sync_type: heartbeats.append(sync_type),
        )

        with app.app_context():
            result = scheduled.run_u2c_import(force=True)

        assert result['success'] is True
        assert heartbeats
        assert set(heartbeats) == {'u2c_import'}

    def test_run_records_outcome_in_sync_status(self, app, monkeypatch):
        import json
        from app.models import SyncStatus
        from app.services.scheduled_sync import _run_u2c_import

        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 5000.0}).install(monkeypatch)

        with app.app_context():
            _run_u2c_import()
            status = SyncStatus.get_status('u2c_import')
            assert status['state'] == 'complete'
            assert json.loads(status['details'])['outcome'] == 'imported'

    def test_run_records_failure_when_msxi_is_dark(self, app, monkeypatch):
        import json
        from app.models import SyncStatus
        from app.services.scheduled_sync import _run_u2c_import

        FakeMsxi(None, {}).install(monkeypatch)

        with app.app_context():
            _run_u2c_import()
            status = SyncStatus.get_status('u2c_import')
            assert status['state'] == 'failed'
            assert json.loads(status['details'])['outcome'] == 'broken'

    def test_skips_the_pull_when_not_due(self, app, monkeypatch):
        from app.models import SyncStatus
        from app.services.scheduled_sync import _run_u2c_import

        fq = current_fiscal_quarter()
        fake = FakeMsxi(fq, {'20260915': 5000.0}).install(monkeypatch)

        with app.app_context():
            SyncStatus.mark_started('u2c_import')
            SyncStatus.mark_completed('u2c_import', success=True)
            _run_u2c_import()
            assert fake.calls == []

    def test_daily_u2c_runs_when_milestone_auto_sync_is_disabled(
        self, app, monkeypatch,
    ):
        from app.models import UserPreference
        import app.services.scheduled_sync as scheduled

        calls = []
        monkeypatch.setattr(
            scheduled,
            'run_u2c_import_if_due',
            lambda current_app: calls.append(current_app),
        )
        with app.app_context():
            pref = UserPreference.query.first()
            pref.milestone_auto_sync = False
            db.session.commit()

        scheduled._run_daily_scheduler_cycle(app, None)
        assert calls == [app]


class TestRematchSnapshotItems:
    """Local re-matching between MSXi's weekly publishes."""

    def test_rematch_links_milestones_synced_after_the_import(
        self, app, monkeypatch,
    ):
        from app.services.u2c_snapshot import (
            refresh_official_snapshot, rematch_current_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 0.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            item = U2CSnapshotItem.query.filter_by(
                snapshot_id=snapshot.id, milestone_number='MS-2').first()
            assert item.milestone_id is None

            # The milestone shows up in a later milestone sync.
            customer = Customer(name='Late Corp', tpid=4242)
            db.session.add(customer)
            db.session.flush()
            milestone = Milestone(
                url='https://example.com/ms-late',
                title='Migrate SQL',
                milestone_number='MS-2',
                msx_status='On Track',
                workload='Data: SQL',
                customer_id=customer.id,
            )
            db.session.add(milestone)
            db.session.commit()

            assert rematch_current_snapshot() == 1
            refreshed = db.session.get(U2CSnapshotItem, item.id)
            assert refreshed.milestone_id == milestone.id
            # MSXi already classified this row, so re-matching links the
            # milestone without overwriting the report's own workload.
            assert refreshed.workload == 'Infra: Windows'

    def test_rematch_without_a_snapshot_is_a_no_op(self, app):
        from app.services.u2c_snapshot import rematch_current_snapshot
        with app.app_context():
            assert rematch_current_snapshot() == 0


# =============================================================================
# Automated daily refresh
# =============================================================================

class FakeMsxi:
    """Stand-in for the MSXi pull, keyed by (fiscal quarter, version).

    MSXi only ever serves whichever quarter is current to it, and answers every
    other request with an empty list, so the fake does the same.
    """

    def __init__(self, live_quarter, versions):
        """
        Args:
            live_quarter: The quarter MSXi currently serves, or None for
                "MSXi is returning nothing at all".
            versions: Ordered dict-ish of {version: converted_acr}, newest last.
        """
        self.live_quarter = live_quarter
        self.versions = dict(versions)
        self.calls = []

    def newest(self):
        return list(self.versions)[-1]

    def __call__(self, fq, territories=None, version=None):
        from app.services.u2c_pull import CURRENT_VERSION

        self.calls.append((fq, version))
        if fq != self.live_quarter:
            return []
        key = self.newest() if version in (None, CURRENT_VERSION) else version
        if key not in self.versions:
            return []
        converted = self.versions[key]
        return [
            _msxi_row(converted_acr=converted,
                      workload='Data: Analytics - Fabric - New Analytics',
                      current_commitment='Committed' if converted else 'Uncommitted'),
            _msxi_row(milestone_name='Migrate SQL', milestone_number='MS-2',
                      workload='Infra: Windows', starting_acr=3000.0),
        ]

    def install(self, monkeypatch):
        import app.services.u2c_pull as pull_module
        monkeypatch.setattr(pull_module, 'pull_u2c_milestones', self)
        return self


class TestPreviousFiscalQuarter:
    """Quarter arithmetic used to find the quarter that just ended."""

    def test_wraps_across_fiscal_year(self):
        from app.services.u2c_snapshot import previous_fiscal_quarter
        assert previous_fiscal_quarter('FY27 Q1') == 'FY26 Q4'

    def test_steps_back_within_year(self):
        from app.services.u2c_snapshot import previous_fiscal_quarter
        assert previous_fiscal_quarter('FY27 Q3') == 'FY27 Q2'
        assert previous_fiscal_quarter('FY27 Q4') == 'FY27 Q3'


class TestVersionHelpers:
    """Snapshot-version probing helpers."""

    def test_candidates_are_tuesdays_newest_first(self):
        from app.services.u2c_pull import candidate_versions, version_to_date

        # 2026-09-21 is a Monday; the most recent load day is Tuesday the 15th.
        versions = candidate_versions(weeks=3, ref_date=date(2026, 9, 21))
        assert versions == ['20260915', '20260908', '20260901']
        assert all(version_to_date(v).weekday() == 1 for v in versions)

    def test_candidates_include_today_when_today_is_a_load_day(self):
        from app.services.u2c_pull import candidate_versions

        versions = candidate_versions(weeks=2, ref_date=date(2026, 9, 22))
        assert versions[0] == '20260922'

    def test_version_to_date_rejects_junk(self):
        from app.services.u2c_pull import version_to_date
        assert version_to_date('current') is None
        assert version_to_date('') is None

    def test_fingerprint_ignores_row_order(self):
        from app.services.u2c_pull import fingerprint_rows

        a = _msxi_row(milestone_number='MS-1')
        b = _msxi_row(milestone_number='MS-2')
        assert fingerprint_rows([a, b]) == fingerprint_rows([b, a])

    def test_fingerprint_tracks_conversion_changes(self):
        from app.services.u2c_pull import fingerprint_rows

        before = [_msxi_row(converted_acr=0.0)]
        after = [_msxi_row(converted_acr=5000.0)]
        assert fingerprint_rows(before) != fingerprint_rows(after)

    def test_fingerprint_tracks_workload_changes(self):
        """Workload decides which filtered view a row lands in, so it counts as
        new data even when the money hasn't moved."""
        from app.services.u2c_pull import fingerprint_rows

        before = [_msxi_row(workload='Infra: Windows')]
        after = [_msxi_row(workload='Data: SQL')]
        assert fingerprint_rows(before) != fingerprint_rows(after)

    def test_fingerprint_tracks_newly_projected_fields(self):
        """A field we start storing must invalidate the hash, or existing
        snapshots keep stale values until MSXi happens to publish again."""
        from app.services.u2c_pull import fingerprint_rows

        unclassified = [_msxi_row(workload=None)]
        classified = [_msxi_row(workload='Data: SQL')]
        assert fingerprint_rows(unclassified) != fingerprint_rows(classified)

    @pytest.mark.parametrize(
        ('field', 'value'),
        [
            ('customer_name', 'Renamed Customer'),
            ('milestone_name', 'Renamed Milestone'),
            ('opportunity_number', 'OPP-CHANGED'),
            ('owner_alias', 'newowner'),
            ('starting_due_date', date(2026, 9, 30)),
            ('starting_status', 'At Risk'),
        ],
    )
    def test_fingerprint_tracks_every_persisted_msxi_field(self, field, value):
        from app.services.u2c_pull import fingerprint_rows

        before = [_msxi_row()]
        after = [_msxi_row(**{field: value})]
        assert fingerprint_rows(before) != fingerprint_rows(after)


class TestRefreshOfficialSnapshot:
    """The daily automation entry point."""

    def test_imports_when_msxi_has_the_current_quarter(self, app, monkeypatch):
        from app.services.u2c_snapshot import (
            OUTCOME_IMPORTED, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260908': 0.0, '20260915': 5000.0}).install(monkeypatch)

        with app.app_context():
            result = refresh_official_snapshot()
            assert result['success'] is True
            assert result['outcome'] == OUTCOME_IMPORTED
            snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            assert snapshot.source == U2CSnapshot.SOURCE_MSXI
            assert snapshot.content_fingerprint

    def test_second_run_is_unchanged(self, app, monkeypatch):
        from app.services.u2c_snapshot import (
            OUTCOME_UNCHANGED, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 5000.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            result = refresh_official_snapshot()
            assert result['outcome'] == OUTCOME_UNCHANGED

    def test_falls_back_to_previous_quarter_before_rollover(self, app, monkeypatch):
        """Our calendar has moved on but MSXi still publishes the old quarter."""
        from app.services.u2c_snapshot import (
            OUTCOME_NOT_ROLLED_OVER, previous_fiscal_quarter,
            refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        prev = previous_fiscal_quarter(fq)
        FakeMsxi(prev, {'20260915': 1000.0}).install(monkeypatch)

        with app.app_context():
            result = refresh_official_snapshot()
            assert result['outcome'] == OUTCOME_NOT_ROLLED_OVER
            assert result['awaiting_quarter'] == fq
            assert U2CSnapshot.query.filter_by(fiscal_quarter=prev).first()
            assert U2CSnapshot.query.filter_by(fiscal_quarter=fq).first() is None

    def test_both_quarters_empty_is_broken_not_silence(self, app, monkeypatch):
        """MSXi answers 'schema changed' the same way it answers 'no rows'."""
        from app.services.u2c_snapshot import (
            OUTCOME_BROKEN, refresh_official_snapshot,
        )
        FakeMsxi(None, {'20260915': 0.0}).install(monkeypatch)

        with app.app_context():
            result = refresh_official_snapshot()
            assert result['success'] is False
            assert result['outcome'] == OUTCOME_BROKEN
            assert 'VPN' in result['error'] or 'territories' in result['error']

    def test_empty_pull_never_destroys_an_existing_snapshot(self, app, monkeypatch):
        """The automation depends on an empty pull being non-destructive."""
        from app.services.u2c_snapshot import refresh_official_snapshot

        fq = current_fiscal_quarter()
        fake = FakeMsxi(fq, {'20260915': 5000.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            before = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            assert before.total_items == 2

            fake.live_quarter = None  # MSXi goes dark
            refresh_official_snapshot()

            after = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            assert after is not None
            assert after.total_items == 2


class TestVersionHistory:
    """The permanent weekly attainment trend."""

    def test_import_records_the_version_it_pulled(self, app, monkeypatch):
        from app.models import U2CSnapshotVersion
        from app.services.u2c_snapshot import refresh_official_snapshot

        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 5000.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            entry = U2CSnapshotVersion.query.filter_by(
                msxi_version='20260915').first()
            assert entry is not None
            assert entry.version_date == date(2026, 9, 15)
            assert entry.total_converted_acr == 5000.0

    def test_backfill_recovers_retained_weeks(self, app, monkeypatch):
        from app.models import U2CSnapshotVersion
        from app.services.u2c_snapshot import (
            backfill_version_history, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {
            '20260901': 1000.0, '20260908': 2000.0, '20260915': 5000.0,
        }).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            backfill_version_history(snapshot)

            stored = {
                v.msxi_version: v.total_converted_acr
                for v in U2CSnapshotVersion.query.filter_by(
                    snapshot_id=snapshot.id)
            }
            assert stored == {'20260901': 1000.0, '20260908': 2000.0,
                              '20260915': 5000.0}

    def test_versions_are_write_once(self, app, monkeypatch):
        """Dated MSXi versions are immutable, so re-storing is a no-op."""
        from app.models import U2CSnapshotVersion
        from app.services.u2c_snapshot import (
            record_version_totals, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 5000.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            assert record_version_totals(
                snapshot, '20260915', [_msxi_row()]) is None
            assert U2CSnapshotVersion.query.filter_by(
                snapshot_id=snapshot.id, msxi_version='20260915').count() == 1

    def test_declining_conversions_are_stored_as_is(self, app, monkeypatch):
        """MSXi restates conversions downward - the trend is not monotonic."""
        from app.models import U2CSnapshotVersion
        from app.services.u2c_snapshot import (
            backfill_version_history, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {
            '20260901': 6000.0, '20260908': 4000.0, '20260915': 4600.0,
        }).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            backfill_version_history(snapshot)

            series = [
                v.total_converted_acr for v in
                U2CSnapshotVersion.query
                .filter_by(snapshot_id=snapshot.id)
                .order_by(U2CSnapshotVersion.version_date)
            ]
            assert series == [6000.0, 4000.0, 4600.0]


class TestAttainmentTrend:
    """The chart series and its SalesIQ tool."""

    def _seed(self, app, monkeypatch):
        from app.services.u2c_snapshot import (
            backfill_version_history, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {
            '20260901': 1000.0, '20260908': 800.0, '20260915': 4000.0,
        }).install(monkeypatch)
        refresh_official_snapshot()
        snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
        backfill_version_history(snapshot)
        return snapshot

    def test_trend_is_ordered_oldest_first(self, app, monkeypatch):
        from app.services.u2c_snapshot import get_attainment_trend
        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            trend = get_attainment_trend(snapshot.id)

            assert [p['date'] for p in trend] == [
                '2026-09-01', '2026-09-08', '2026-09-15']
            # The Data row (5000 baseline) commits, the Infra row never does.
            assert [p['committed_acr'] for p in trend] == [5000.0, 5000.0, 5000.0]
            assert [p['msxi_converted_acr'] for p in trend] == [
                1000.0, 800.0, 4000.0]
            assert trend[0]['label'] == 'Sep 01'

    def test_trend_reports_percentage_against_the_frozen_baseline(
        self, app, monkeypatch,
    ):
        from app.services.u2c_snapshot import get_attainment_trend
        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            trend = get_attainment_trend(snapshot.id)
            # Baseline is 5000 + 3000 from the fake rows.
            assert all(p['starting_acr'] == 8000.0 for p in trend)
            assert trend[-1]['u2c_pct'] == 62.5  # 5000 / 8000

    def test_trend_is_empty_without_history(self, app, u2c_data):
        from app.services.u2c_snapshot import get_attainment_trend
        with app.app_context():
            make_snapshot()
            snapshot = U2CSnapshot.query.first()
            assert get_attainment_trend(snapshot.id) == []

    def test_chart_is_hidden_with_a_single_point(self, client, app, monkeypatch):
        """One point isn't a trend - don't render an empty-looking chart."""
        from app.services.u2c_snapshot import refresh_official_snapshot

        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 4000.0}).install(monkeypatch)
        with app.app_context():
            refresh_official_snapshot()

        response = client.get('/reports/u2c')
        assert b'u2cTrendChart' not in response.data

    def test_chart_renders_with_multiple_points(self, client, app, monkeypatch):
        with app.app_context():
            self._seed(app, monkeypatch)

        response = client.get('/reports/u2c')
        assert b'u2cTrendChart' in response.data
        assert b'Attainment Over the Quarter' in response.data
        assert b"type: 'linear'" in response.data
        assert b'Date.parse(p.date)' in response.data
        assert b'"start": "2026-07-01"' in response.data
        assert b'"end": "2026-09-30"' in response.data
        assert b'min: Date.parse(TREND_BOUNDS.start)' in response.data

    def test_salesiq_trend_tool(self, app, monkeypatch):
        from app.services.salesiq_tools import get_u2c_attainment_trend
        with app.app_context():
            self._seed(app, monkeypatch)
            result = get_u2c_attainment_trend()
            assert len(result['points']) == 3
            assert result['latest_u2c_pct'] == 62.5
            assert result['msxi_version'] == '20260915'

    def test_salesiq_trend_tool_without_a_snapshot(self, app):
        from app.services.salesiq_tools import get_u2c_attainment_trend
        with app.app_context():
            result = get_u2c_attainment_trend('FY20 Q1')
            assert 'No U2C snapshot exists' in result['message']


class TestTrendWorkloadFiltering:
    """The trend has to follow the workload dropdown like everything else."""

    def _seed(self, app, monkeypatch):
        from app.services.u2c_snapshot import (
            backfill_version_history, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        # Row 1 is Data (5000 starting), row 2 is Infra (3000 starting, never
        # converts). Only the Data row's conversion moves.
        FakeMsxi(fq, {
            '20260908': 1000.0, '20260915': 4000.0,
        }).install(monkeypatch)
        refresh_official_snapshot()
        snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
        backfill_version_history(snapshot)
        return snapshot

    def test_per_item_history_is_stored(self, app, monkeypatch):
        from app.models import U2CSnapshotVersionItem
        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            rows = (
                U2CSnapshotVersionItem.query
                .join(U2CSnapshotVersionItem.version)
                .filter_by(snapshot_id=snapshot.id)
                .all()
            )
            assert len(rows) == 4  # 2 milestones x 2 versions
            assert {r.workload for r in rows} == {
                'Data: Analytics - Fabric - New Analytics', 'Infra: Windows'}

    def test_filtered_trend_only_counts_that_workload(self, app, monkeypatch):
        from app.services.u2c_snapshot import get_attainment_trend
        with app.app_context():
            snapshot = self._seed(app, monkeypatch)

            data = get_attainment_trend(snapshot.id, 'Data')
            assert [p['starting_acr'] for p in data] == [5000.0, 5000.0]
            assert [p['committed_acr'] for p in data] == [5000.0, 5000.0]
            assert data[-1]['u2c_pct'] == 100.0

            infra = get_attainment_trend(snapshot.id, 'Infra')
            assert [p['starting_acr'] for p in infra] == [3000.0, 3000.0]
            assert [p['committed_acr'] for p in infra] == [0.0, 0.0]

    def test_workload_series_sum_to_the_overall_series(self, app, monkeypatch):
        from app.services.u2c_snapshot import get_attainment_trend
        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            overall = get_attainment_trend(snapshot.id)
            data = get_attainment_trend(snapshot.id, 'Data')
            infra = get_attainment_trend(snapshot.id, 'Infra')

            for i, point in enumerate(overall):
                assert point['starting_acr'] == (
                    data[i]['starting_acr'] + infra[i]['starting_acr'])
                assert point['committed_acr'] == (
                    data[i]['committed_acr'] + infra[i]['committed_acr'])

    def test_series_map_has_one_entry_per_prefix_plus_overall(self, app, monkeypatch):
        from app.services.u2c_snapshot import get_attainment_trend_by_workload
        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            series = get_attainment_trend_by_workload(snapshot.id)
            assert set(series) == {'', 'Data', 'Infra'}

    def test_totals_only_weeks_are_skipped_when_filtering(self, app, monkeypatch):
        """Versions stored before per-item history can't answer a filtered ask."""
        from app.models import U2CSnapshotVersion, U2CSnapshotVersionItem
        from app.services.u2c_snapshot import get_attainment_trend

        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            oldest = (
                U2CSnapshotVersion.query
                .filter_by(snapshot_id=snapshot.id)
                .order_by(U2CSnapshotVersion.version_date)
                .first()
            )
            U2CSnapshotVersionItem.query.filter_by(
                version_id=oldest.id).delete()
            db.session.commit()

            assert len(get_attainment_trend(snapshot.id)) == 2
            assert len(get_attainment_trend(snapshot.id, 'Data')) == 1

    def test_backfill_completes_totals_only_versions(self, app, monkeypatch):
        """A week stored as totals alone gets its detail filled in later."""
        from app.models import U2CSnapshotVersion, U2CSnapshotVersionItem
        from app.services.u2c_snapshot import backfill_version_history

        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            oldest = (
                U2CSnapshotVersion.query
                .filter_by(snapshot_id=snapshot.id)
                .order_by(U2CSnapshotVersion.version_date)
                .first()
            )
            U2CSnapshotVersionItem.query.filter_by(
                version_id=oldest.id).delete()
            db.session.commit()
            assert oldest.items.count() == 0

            assert backfill_version_history(snapshot) == 1
            assert oldest.items.count() == 2

    def test_completing_a_version_does_not_double_its_items(self, app, monkeypatch):
        """Re-pulling a partially stored version must replace, not append."""
        from app.models import U2CSnapshotVersion, U2CSnapshotVersionItem
        from app.services.u2c_snapshot import (
            backfill_version_history, get_attainment_trend,
        )

        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            before = get_attainment_trend(snapshot.id, 'Data')[-1]

            # Simulate a version written before `commitment` existed: the rows
            # are there but incomplete, so the backfill will re-pull them.
            for version in U2CSnapshotVersion.query.filter_by(
                    snapshot_id=snapshot.id):
                U2CSnapshotVersionItem.query.filter_by(
                    version_id=version.id).update({'commitment': None})
            db.session.commit()

            backfill_version_history(snapshot)

            after = get_attainment_trend(snapshot.id, 'Data')[-1]
            assert after['starting_acr'] == before['starting_acr']
            assert after['items'] == before['items']

    def test_chart_series_reaches_the_page(self, client, app, monkeypatch):
        with app.app_context():
            self._seed(app, monkeypatch)

        response = client.get('/reports/u2c')
        assert b'TREND_SERIES' in response.data
        assert b'renderU2cTrend' in response.data

    def test_cards_and_trend_agree_on_the_filtered_target(self, app, monkeypatch):
        """Both views must classify a row the same way, or the page contradicts
        itself: the cards filter U2CSnapshotItem.workload while the chart
        filters the per-version copies."""
        from app.services.u2c_snapshot import get_attainment, get_attainment_trend

        with app.app_context():
            snapshot = self._seed(app, monkeypatch)
            for prefix in ('Data', 'Infra'):
                cards = get_attainment(snapshot.id, prefix)
                trend = get_attainment_trend(snapshot.id, prefix)[-1]
                assert cards['target_total'] == trend['starting_acr'], prefix

    def test_a_milestone_converting_above_its_baseline_does_not_inflate_the_chart(
        self, app, monkeypatch,
    ):
        """A $2,000 baseline can convert at $5,600 in MSXi's own measure.

        The chart must report how much of the *baseline* committed, matching the
        "Committed ACR" card, rather than MSXi's converted-pipeline figure -
        otherwise the line can exceed its own target and disagree with every
        other number on the page.
        """
        import app.services.u2c_pull as pull_module
        from app.services.u2c_snapshot import (
            get_attainment, get_attainment_trend, refresh_official_snapshot,
        )

        rows = [
            _msxi_row(milestone_number='MS-GROWS', workload='Data: SQL MI',
                      starting_acr=2000.0, converted_acr=5600.0,
                      current_commitment='Committed'),
            _msxi_row(milestone_number='MS-FLAT', workload='Data: Fabric',
                      starting_acr=3000.0, converted_acr=0.0),
        ]
        monkeypatch.setattr(
            pull_module, 'pull_u2c_milestones',
            lambda fq, territories=None, version=None: rows,
        )
        with app.app_context():
            refresh_official_snapshot()
            snapshot = U2CSnapshot.query.first()

            point = get_attainment_trend(snapshot.id, 'Data')[-1]
            cards = get_attainment(snapshot.id, 'Data')

            assert point['committed_acr'] == 2000.0 == cards['committed_total']
            assert point['starting_acr'] == 5000.0 == cards['target_total']
            assert point['u2c_pct'] == cards['u2c_pct'] == 40.0
            # MSXi's raw measure is still available, just not the headline.
            assert point['msxi_converted_acr'] == 5600.0
            assert point['committed_acr'] <= point['starting_acr']

    def test_a_new_stored_field_forces_a_reimport(self, app, monkeypatch):
        """Adding a projected column must not leave stored items stale."""
        import app.services.u2c_pull as pull_module
        from app.services.u2c_snapshot import (
            OUTCOME_IMPORTED, refresh_official_snapshot,
        )

        fq = current_fiscal_quarter()
        unclassified = [_msxi_row(workload=None)]
        monkeypatch.setattr(
            pull_module, 'pull_u2c_milestones',
            lambda f, territories=None, version=None: unclassified,
        )
        with app.app_context():
            refresh_official_snapshot()
            item = U2CSnapshotItem.query.first()
            assert item.workload is None

            # MSXi starts classifying the same row - same money, new field.
            classified = [_msxi_row(workload='Data: SQL')]
            monkeypatch.setattr(
                pull_module, 'pull_u2c_milestones',
                lambda f, territories=None, version=None: classified,
            )
            result = refresh_official_snapshot()
            assert result['outcome'] == OUTCOME_IMPORTED
            assert U2CSnapshotItem.query.first().workload == 'Data: SQL'


class TestCloseOutQuarter:
    """Finalising a quarter that has rolled over in MSXi."""

    def test_close_out_uses_a_retained_pre_rollover_version(self, app, monkeypatch):
        from app.services.u2c_snapshot import (
            close_out_quarter, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        fake = FakeMsxi(fq, {'20260915': 9000.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            result = close_out_quarter(fq)

            assert result['success'] is True
            assert result['msxi_version'] == '20260915'
            snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            assert snapshot.is_final is True
        assert fake.calls

    def test_close_out_freezes_what_we_have_when_nothing_is_retained(
        self, app, monkeypatch,
    ):
        """Best-effort: an expired quarter still gets marked final."""
        from app.services.u2c_snapshot import (
            close_out_quarter, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        fake = FakeMsxi(fq, {'20260915': 9000.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            fake.live_quarter = None  # quarter has aged out of MSXi entirely
            result = close_out_quarter(fq)

            assert result['success'] is True
            assert result['reason'] == 'no_retained_version'
            snapshot = U2CSnapshot.query.filter_by(fiscal_quarter=fq).first()
            assert snapshot.is_final is True
            assert snapshot.total_items == 2  # untouched

    def test_close_out_is_idempotent(self, app, monkeypatch):
        from app.services.u2c_snapshot import (
            close_out_quarter, refresh_official_snapshot,
        )
        fq = current_fiscal_quarter()
        FakeMsxi(fq, {'20260915': 9000.0}).install(monkeypatch)

        with app.app_context():
            refresh_official_snapshot()
            close_out_quarter(fq)
            again = close_out_quarter(fq)
            assert again['reason'] == 'already_final'

    def test_close_out_without_a_snapshot_is_a_no_op(self, app, monkeypatch):
        from app.services.u2c_snapshot import close_out_quarter
        FakeMsxi(None, {}).install(monkeypatch)
        with app.app_context():
            result = close_out_quarter('FY20 Q1')
            assert result['success'] is False
            assert result['reason'] == 'no_snapshot'
