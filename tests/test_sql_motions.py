"""SQL report eligibility, exact widget parity, scope and combined-source readiness."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
import pytest

from app.models import (
    Customer, Job, Seller, SqlArcVersion, SqlMotionImport, SqlRenewal, UserPreference, db,
)
from app.services import sql_motions
from app.services.dashboard_filters import scope_report_destination
from app.services.dashboard_sync import WIDGET_SOURCES, get_source_sync
from app.services.dashboards import get_widget_catalog, get_widget_preview
from app.services.salesiq_tools import execute_tool
from tests.test_fabric_renewal_widgets import render_widget


@pytest.fixture
def sql_rows(app):
    """Create source evidence only in the isolated test database."""
    with app.app_context():
        snapshot = SqlMotionImport(
            source_metadata=json.dumps({
                'sources': {
                    'azure_migrations': {'refresh_rows': [
                        {'data': 'SQL license snapshot', 'refresh_date': None,
                         'frequency': 'Monthly'},
                    ]},
                    'ads_accelerate': {'quality_latest': [
                        {'table_name': 'Arc versions', 'result': 'Pass',
                         'last_refresh': '2026-10-21T11:51:12+00:00'},
                    ]},
                },
                'notices': ['Source caveat'],
            }),
        )
        seller = Seller(name='SQL owner')
        other = Seller(name='Other SQL owner')
        db.session.add_all([snapshot, seller, other])
        db.session.flush()

        def customer(name: str, owner: Seller | None = seller) -> Customer:
            """Use current account ownership for both queues."""
            saved = Customer(name=name, tpid=760000 + Customer.query.count(), seller=owner)
            db.session.add(saved)
            db.session.flush()
            return saved

        def renewal(saved: Customer, days: int = 10, amount: float | None = 100) -> SqlRenewal:
            """Keep source dates and unknown amounts separate from numeric zero."""
            row = SqlRenewal(
                import_id=snapshot.id, customer_id=saved.id,
                expiration_date=date.today() + timedelta(days=days),
                expiration_amount=amount, install_base_cores=64,
                sql_iaas_cores=8, monthly_opportunity=200,
            )
            db.session.add(row)
            db.session.flush()
            return row

        def arc(saved: Customer, count: float | None = 3, *, version: str = 'SQL SERVER 2016',
                category: str = 'All Discovered', subscription: str = 'sub-1') -> SqlArcVersion:
            """Store subscription/version/category grain without mixing category totals."""
            row = SqlArcVersion(
                import_id=snapshot.id, customer_id=saved.id,
                subscription_id=subscription, subscription_name=f'Subscription {subscription}',
                sql_version=version, discovery_type=category, discovered_count=count,
                processed_at=datetime.now(timezone.utc),
            )
            db.session.add(row)
            db.session.flush()
            return row

        yield SimpleNamespace(
            snapshot=snapshot, seller=seller, other=other, customer=customer,
            renewal=renewal, arc=arc,
        )


def test_renewals_keep_all_future_dates_and_unknown_amounts(sql_rows):
    """The 90-day summary is inclusive, not an arbitrary cutoff for the queue."""
    source = sql_rows
    later = source.customer('Later')
    source.renewal(later, 500, None)
    today = source.customer('Today')
    source.renewal(today, 0, 0)
    boundary = source.customer('Ninety')
    source.renewal(boundary, 90, 200)
    past = source.customer('Expired')
    source.renewal(past, -1, 90000)
    source.renewal(source.customer('Day 91'), 91, 100)
    stale = source.customer('Stale')
    source.renewal(stale, 1, 999999)
    stale.stale_since = datetime.now(timezone.utc)
    db.session.commit()
    report = sql_motions.get_sql_motion_report('renewals')
    assert [row['name'] for row in report['rows']] == ['Today', 'Ninety', 'Day 91', 'Later']
    assert report['count'] == 4
    assert report['within_90_count'] == 2
    assert report['expiration_amount'] == 300
    assert report['missing_amount_count'] == 1
    assert report['rows'][0]['expiration_amount'] == 0
    assert report['rows'][-1]['expiration_amount'] is None
    assert report['rows'][0]['install_base_cores'] == 64
    assert report['rows'][0]['monthly_opportunity'] == 200
    assert report['notices'] == ['Source caveat']
    assert report['data_warning'] is None
    assert sql_motions.get_sql_renewals_widget() == report
    assert sql_motions.get_sql_renewals_widget(customer_ids=())['count'] == 0
    assert sql_motions.get_sql_renewals_widget(customer_ids=(stale.id,))['count'] == 0
    assert sql_motions.get_sql_renewals_widget(customer_ids=(later.id,))['count'] == 1


def test_eos_uses_exact_version_all_discovered_and_subscription_grain(sql_rows):
    """License categories, other SQL versions and absent observations never prove exposure."""
    source = sql_rows
    matched = source.customer('SQL 2016')
    source.arc(matched, 4)
    source.arc(matched, 6, subscription='sub-2')
    for category in ('Enterprise', 'Standard', 'Non Production'):
        source.arc(matched, 999, category=category)
    source.arc(matched, 9000, version='SQL SERVER 2014')
    source.renewal(matched, 10, 500)
    for version in ('SQL SERVER 2012', 'SQL SERVER 2014', 'SQL SERVER 2019',
                    'SQL SERVER 20160'):
        source.arc(source.customer(version), 20, version=version)
    source.arc(source.customer('Zero'), 0)
    source.arc(source.customer('Unknown'), None)
    source.arc(source.customer('Only paid licenses'), 100, category='Enterprise')
    source.customer('Unobserved')
    smaller = source.customer('Smaller', None)
    source.arc(smaller, 2)
    stale = source.customer('Stale')
    source.arc(stale, 9999)
    stale.stale_since = datetime.now(timezone.utc)
    db.session.commit()
    report = sql_motions.get_sql_motion_report('eos')
    assert [row['id'] for row in report['rows']] == [matched.id, smaller.id]
    assert report['discovered_count'] == 12
    assert report['data_warning'] == sql_motions.SQL_2016_DATA_WARNING
    assert report['rows'][0]['subscription_count'] == 2
    assert len(report['rows'][0]['subscriptions']) == 2
    assert all('expiration_date' not in row and 'expiration_amount' not in row
               for row in report['rows'])
    assert not {'within_90_count', 'expiration_amount', 'missing_amount_count'} & report.keys()
    assert 'azure_migrations' not in report['snapshot']['sources']
    assert report['rows'][1]['esu_coverage'] == 'Unknown'
    assert sql_motions.get_sql_eos_widget() == report
    assert sql_motions.get_sql_eos_widget(customer_ids=(smaller.id,))['discovered_count'] == 2
    assert sql_motions.get_sql_eos_widget(customer_ids=())['count'] == 0
    assert len(report['seller_groups']) == 2


def test_renewal_queue_is_not_capped_at_fifty(sql_rows):
    """Lower-ranked renewals remain in the shared full-cohort totals and Details."""
    for index in range(61):
        sql_rows.renewal(sql_rows.customer(f'Account {index}'), index, 1)
    db.session.commit()
    data = sql_motions.get_sql_renewals_widget()
    assert data['count'] == data['within_90_count'] == 61
    assert data['expiration_amount'] == 61
    soup = BeautifulSoup(render_widget('sql_motion', data), 'html.parser')
    assert len(soup.select('tr[data-customer-id]')) == 61


def test_eos_does_not_read_renewal_records(sql_rows, monkeypatch):
    """EOS depends only on version discovery, even when an account also has a renewal."""
    customer = sql_rows.customer('Independent SQL 2016 evidence')
    sql_rows.arc(customer)
    sql_rows.renewal(customer)
    db.session.commit()

    def unexpected_renewal_read(*args):
        """Reject a renewal join in the version-specific report."""
        pytest.fail('EOS must not read unrelated renewal records.')

    monkeypatch.setattr(sql_motions, '_renewals', unexpected_renewal_read)
    assert sql_motions.get_sql_eos_widget()['count'] == 1


def test_eos_without_snapshot_has_no_renewal_metrics(app):
    """Missing-data states must not expose unrelated zero or unknown renewal metrics."""
    with app.app_context():
        report = sql_motions.get_sql_eos_widget()
        assert report['snapshot'] is None
        assert not {'within_90_count', 'expiration_amount', 'missing_amount_count'} & report.keys()


def test_current_ownership_and_missing_subscription_identity(sql_rows):
    """Unknown subscription IDs are not converted into a measured subscription count."""
    customer = sql_rows.customer('Reassigned')
    record = sql_rows.arc(customer)
    record.subscription_id = ''
    record.subscription_name = ''
    customer.seller = sql_rows.other
    db.session.commit()
    data = sql_motions.get_sql_eos_widget()
    assert data['rows'][0]['seller']['name'] == 'Other SQL owner'
    assert data['rows'][0]['subscription_count'] is None
    soup = BeautifulSoup(render_widget('sql_motion', data), 'html.parser')
    assert soup.select_one('tr[data-customer-id] td:last-child').text == 'Not supplied'


def test_subscription_evidence_only_removes_the_two_bottom_lines(client, sql_rows):
    """Preserve the original subscription text without the separate ID and processed lines."""
    customer = sql_rows.customer('Readable subscriptions')
    record = sql_rows.arc(customer, 17)
    record.subscription_name = 'Production <main> (sub-1)'
    db.session.commit()
    report = sql_motions.get_sql_eos_widget()
    assert report['rows'][0]['subscriptions'][0]['name'] == 'Production <main> (sub-1)'
    assert report['rows'][0]['subscriptions'][0]['processed_at'] is not None
    response = client.get('/reports/sql-eos')
    soup = BeautifulSoup(response.data, 'html.parser')
    evidence = soup.select_one('.sb-sql-subscriptions')
    row = evidence.select_one('li')
    assert ' '.join(row.text.split()) == 'Production <main> (sub-1): 17'
    assert not row.select('span')
    assert row.text.count('sub-1') == 1
    assert not evidence.select('small, .local-datetime')
    assert 'Processed' not in evidence.text
    assert not evidence.select('main')


@pytest.mark.parametrize('motion', ['renewals', 'eos'])
def test_latest_complete_import_including_successful_empty(sql_rows, motion):
    """New empty snapshots replace old candidates and are ready without another sync."""
    saved = sql_rows.customer('Previous import')
    sql_rows.renewal(saved)
    sql_rows.arc(saved)
    db.session.commit()
    assert sql_motions.get_sql_motion_report(motion)['count'] == 1
    newer = SqlMotionImport(
        imported_at=datetime(2000, 1, 1, tzinfo=timezone.utc), source_metadata='{}',
    )
    db.session.add(newer)
    db.session.commit()
    report = sql_motions.get_sql_motion_report(motion)
    assert report['count'] == 0
    assert report['snapshot']['id'] == newer.id
    assert get_source_sync(WIDGET_SOURCES[f'sql.{motion}']) is None


@pytest.mark.parametrize('motion', ['renewals', 'eos'])
def test_report_api_widget_tool_and_scope_parity(client, sql_rows, motion):
    """Every read uses the same cohort; explicit empty scopes do not become portfolio-wide."""
    source = sql_rows
    for owner in (source.seller, source.other):
        customer = source.customer(f'Account {owner.id} <safe>', owner)
        source.renewal(customer)
        source.arc(customer)
    db.session.commit()
    report = sql_motions.get_sql_motion_report(motion)
    path = f'/reports/sql-{motion}'
    assert client.get(f'/api{path}').json == report
    assert execute_tool(f'report_sql_{motion}', {}) == report
    assert execute_tool('report_dashboard_signals', {'widget_key': f'sql.{motion}'}) == report
    page = BeautifulSoup(client.get(path).data, 'html.parser')
    assert len(page.select('tr[data-customer-id]')) == 2
    assert page.select_one('#reportExportBtnGroup')
    assert page.select_one('[data-sb-sync-progress="sql-motions"]')
    assert page.select_one('h1').text == report['title']
    warning = page.select_one('[data-sb-data-quality="incorrect"]')
    watermark = page.select_one('.sb-sql-data-watermark')
    assert bool(watermark) == (motion == 'eos')
    if watermark:
        assert watermark['aria-hidden'] == 'true'
        assert watermark.text.strip() == 'DATA INCORRECT'
        assert 'sb-data-watermark' in watermark['class']
        assert 'sb-data-watermarked' in watermark.parent['class']
    if motion == 'eos':
        assert warning['role'] == 'alert'
        assert sql_motions.SQL_2016_DATA_WARNING in warning.get_text(' ', strip=True)
        assert sql_motions.SQL_2016_DATA_WARNING in page.text
    else:
        assert warning is None
    export_script = next(script.text for script in page.select('script')
                         if 'const EXPORT_WARNING' in script.text)
    assert (
        'const EXPORT_WARNING = "SQL Server 2016 data is known to be incorrect.'
        in export_script
    ) == (motion == 'eos')
    context_text = page.select_one('.sb-sql-source-context').text
    if motion == 'renewals':
        assert 'SQL license snapshot' in context_text
        assert 'Refresh date not supplied' in context_text
    else:
        assert 'SQL license snapshot' not in context_text
        assert 'Azure Migrations' not in context_text
        assert [th['data-export-label'] for th in page.select('.sb-sql-table thead th')] == [
            'Customer', 'Discovered', 'Subscriptions',
        ]
        assert page.select_one('.sb-sql-table')['data-sb-sort-key'] == 'sql-eos-v2-flat'
        assert all(len(row.select('td')) == 3 for row in page.select('tr[data-customer-id]'))
    assert 'Pass quality check' in page.select_one('.sb-sql-source-context').text
    assert 'Source caveat' in page.select_one('.sb-sql-source-context').text
    assert not page.select_one('.sb-sql-source-context').has_attr('open')
    assert not page.select('safe')
    assert 'SQL owner' not in page.select_one('.sb-sql-table').text
    first = report['rows'][0]['id']
    scoped = scope_report_destination(report, f'sql.{motion}', (first,))
    parsed = parse_qs(urlparse(scoped['destination']).query)
    assert parsed == {'customer_scope': ['1'], 'customer_ids': [str(first)]}
    response = client.get(scoped['destination'])
    assert response.status_code == 200
    scoped_page = BeautifulSoup(response.data, 'html.parser')
    scoped_rows = scoped_page.select('tr[data-customer-id]')
    assert [int(row['data-customer-id']) for row in scoped_rows] == [
        first,
    ]
    assert client.get(f'/api{path}?customer_scope=1').json['count'] == 0
    assert client.get(f'/api{path}?customer_scope=1').json['data_warning'] == (
        sql_motions.SQL_2016_DATA_WARNING if motion == 'eos' else None
    )
    grouped = BeautifulSoup(client.get(
        scoped['destination'] + '&group_by_seller=true',
    ).data, 'html.parser')
    assert len(grouped.select('.db-report-seller-heading')) == 1
    assert bool(grouped.select_one('[data-sb-data-quality="incorrect"]')) == (motion == 'eos')
    assert bool(grouped.select_one('.sb-sql-data-watermark')) == (motion == 'eos')
    assert grouped.select_one('.db-report-seller-heading th')['colspan'] == (
        '6' if motion == 'renewals' else '3'
    )
    assert grouped.select_one('.db-report-seller-heading')['data-export-group-label'].startswith(
        'Seller: ',
    )
    assert all(row.select_one('td')['data-export-label'] == report['rows'][0]['name']
               for row in grouped.select('tr[data-customer-id]'))
    assert grouped.select_one('input[name=customer_scope]')['value'] == '1'
    assert grouped.select_one('input[name=customer_ids]')['value'] == str(first)
    assert client.get(f'/api{path}?seller_ids={source.other.id}').json['count'] == 1


@pytest.mark.parametrize('motion', ['renewals', 'eos'])
@pytest.mark.parametrize('query', [
    'customer_scope=bad', 'customer_ids=0', 'customer_ids=abc', 'seller_ids=-1',
])
def test_malformed_scope_is_rejected(client, motion, query):
    """Invalid IDs must fail instead of silently broadening report or API results."""
    assert client.get(f'/reports/sql-{motion}?{query}').status_code == 400
    assert client.get(f'/api/reports/sql-{motion}?{query}').status_code == 400


@pytest.mark.parametrize('motion', ['renewals', 'eos'])
def test_grouping_validation_and_no_remote_get(client, monkeypatch, motion):
    """Read-only routes leave source loading to the shared explicit sync operation."""
    from app.services import sql_motions_sync

    monkeypatch.setattr(sql_motions_sync, 'sync_sql_motions', lambda *a, **kw: pytest.fail(
        'A read started a sync',
    ))
    assert client.get(f'/reports/sql-{motion}?group_by_seller=invalid').status_code == 400
    response = client.get(f'/reports/sql-{motion}')
    assert response.status_code == 200
    assert 'No SQL snapshot yet' in response.text
    page = BeautifulSoup(response.data, 'html.parser')
    assert bool(page.select_one('[data-sb-data-quality="incorrect"]')) == (motion == 'eos')
    assert bool(page.select_one('.sb-sql-data-watermark')) == (motion == 'eos')
    data = client.get(f'/api/reports/sql-{motion}').json
    for variant in ('metric', 'detailed'):
        widget = BeautifulSoup(render_widget('sql_motion', data, variant), 'html.parser')
        assert bool(widget.select_one('[data-sb-data-quality="incorrect"]')) == (motion == 'eos')
        assert bool(widget.select_one('.db-sql-data-watermark')) == (motion == 'eos')


@pytest.mark.parametrize('motion', ['renewals', 'eos'])
@pytest.mark.parametrize('variant', ['metric', 'detailed'])
def test_widget_seller_visibility_only_when_grouped(sql_rows, motion, variant):
    """Seller mode and customer filtering do not authorize displaying seller names."""
    saved = sql_rows.customer('SQL customer')
    sql_rows.renewal(saved)
    sql_rows.arc(saved)
    db.session.commit()
    data = sql_motions.get_sql_motion_report(motion, customer_ids=(saved.id,))
    flat = BeautifulSoup(render_widget('sql_motion', data, variant), 'html.parser')
    assert 'SQL owner' not in flat.text
    assert 'Source notes in full report.' not in flat.text
    assert 'Source caveat' not in flat.text
    assert not flat.select('.db-report-seller, .db-report-seller-heading')
    warning = flat.select_one('[data-sb-data-quality="incorrect"]')
    watermark = flat.select_one('.db-sql-data-watermark')
    assert bool(watermark) == (motion == 'eos')
    if watermark:
        assert watermark['aria-hidden'] == 'true'
        assert watermark.text == 'DATA INCORRECT'
        assert 'sb-data-watermark' in watermark['class']
        assert 'sb-data-watermarked' in watermark.parent['class']
        assert watermark.parent.has_attr('class')
        assert 'db-sql-data-incorrect' in watermark.parent['class']
    assert bool(warning) == (motion == 'eos')
    if warning:
        assert warning['role'] == 'alert'
        assert sql_motions.SQL_2016_DATA_WARNING in warning.get_text(' ', strip=True)
    grouped = BeautifulSoup(
        render_widget('sql_motion', data, variant, group_by_seller=True), 'html.parser',
    )
    assert 'Source notes in full report.' not in grouped.text
    assert bool(grouped.select_one('[data-sb-data-quality="incorrect"]')) == (motion == 'eos')
    assert bool(grouped.select_one('.db-sql-data-watermark')) == (motion == 'eos')
    if variant == 'detailed':
        assert len(grouped.select('.db-report-seller')) == 1
        assert grouped.select_one('.db-report-seller-heading th')['colspan'] == '3'
        assert not grouped.select('tr[data-customer-id] .db-report-seller')
        assert grouped.select_one('table')['data-sb-sort-default-column'] == '1'
        assert len(grouped.select('thead th')) == 3
        assert grouped.select_one('.db-sql-table-scroll[tabindex="0"]')
    else:
        assert not grouped.select('.db-report-seller')
    assert len(flat.select('tr[data-customer-id]')) == (1 if variant == 'detailed' else 0)


@pytest.mark.parametrize('motion', ['renewals', 'eos'])
def test_seller_mode_intersects_explicit_scope(client, sql_rows, motion):
    """An explicit selection cannot escape active seller mode."""
    owned = sql_rows.customer('Owned')
    other = sql_rows.customer('Other', sql_rows.other)
    for customer in (owned, other):
        sql_rows.renewal(customer)
        sql_rows.arc(customer)
    db.session.commit()
    with client.session_transaction() as session:
        session['seller_mode_seller_id'] = sql_rows.seller.id
    result = client.get(f'/api/reports/sql-{motion}').json
    assert [row['id'] for row in result['rows']] == [owned.id]
    assert client.get(f'/api/reports/sql-{motion}?customer_ids={other.id}').json['count'] == 0
    response = client.get(f'/reports/sql-{motion}')
    table = BeautifulSoup(response.data, 'html.parser').select_one('.sb-sql-table')
    assert 'SQL owner' not in table.text


def test_catalog_shared_readiness_preview_and_navigation(client, sql_rows):
    """The two widgets have one shared source, supported footprints and reachable reports."""
    saved = sql_rows.customer('Catalog customer')
    sql_rows.renewal(saved)
    sql_rows.arc(saved)
    db.session.commit()
    catalog = {item['key']: item for item in get_widget_catalog()}
    assert catalog['sql.eos']['title'].endswith('(data incorrect)')
    assert 'Known incorrect data' in catalog['sql.eos']['description']
    assert '(data incorrect)' not in catalog['sql.renewals']['title']
    hub = BeautifulSoup(client.get('/reports').data, 'html.parser')
    assert '(data incorrect)' in hub.select_one('a[href="/reports/sql-eos"]').text
    for motion in ('renewals', 'eos'):
        key = f'sql.{motion}'
        assert WIDGET_SOURCES[key] == ('sql-motions',)
        assert get_source_sync(WIDGET_SOURCES[key]) is None
        variants = catalog[key]['variants']
        assert tuple(variants[0]['widths']) == (3, 4, 6, 12)
        assert tuple(variants[1]['widths']) == (6, 12)
        preview = get_widget_preview(key, 'detailed', 6)
        assert preview['data']['count'] == 1
        assert f'/reports/sql-{motion}' in client.get('/reports').text
        assert f'/reports/sql-{motion}' in client.get('/').text
    console = client.get('/admin/sync-console')
    assert console.status_code == 200
    assert console.text.count('data-sync-operation="sql-motions"') == 1


def test_both_missing_widgets_join_one_combined_sync(client, app):
    """Neither widget creates an independent one-source import or duplicate job."""
    with app.app_context():
        db.session.add(Customer(name='Current SQL scope', tpid=123456))
        db.session.commit()
    first = client.post('/api/reports/dashboards/widgets/sql.renewals/sync', json={'retry': False})
    second = client.post('/api/reports/dashboards/widgets/sql.eos/sync', json={'retry': False})
    assert first.status_code == second.status_code == 200
    assert first.json['sync']['operation'] == second.json['sync']['operation'] == 'sql-motions'
    with app.app_context():
        assert Job.query.filter_by(job_type='sync:sql-motions').count() == 1


@pytest.mark.parametrize('role,specialty,allowed', [
    ('se', 'data', True), ('se', 'infra', False), ('dss', 'infra', True),
])
def test_specialty_gate_applies_to_reports_tools_and_widgets(client, sql_rows, role, specialty,
                                                           allowed):
    """SQL source evidence stays restricted to Data SEs and all sellers."""
    pref = UserPreference.query.first()
    pref.user_role = role
    pref.se_specialty = specialty
    db.session.commit()
    catalog = {item['key'] for item in get_widget_catalog()}
    for motion in ('renewals', 'eos'):
        assert client.get(f'/reports/sql-{motion}').status_code == (200 if allowed else 403)
        assert client.get(f'/api/reports/sql-{motion}').status_code == (200 if allowed else 403)
        assert (f'sql.{motion}' in catalog) == allowed
        assert ('error' not in execute_tool(f'report_sql_{motion}', {})) == allowed


def test_unknown_motion_is_an_explicit_error(app):
    """A mistyped queue identity cannot return a success-shaped empty report."""
    with app.app_context(), pytest.raises(ValueError, match='supported SQL motion'):
        sql_motions.get_sql_motion_report('invalid')
