"""Customer dossier provider, value and shared-consumer parity in an isolated database."""
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from flask import render_template_string
from sqlalchemy.exc import SQLAlchemyError

from app.models import (
    Customer, CustomerContact, CustomerCSAM, Engagement, Favorite, Milestone, Note,
    Opportunity, RevenueAnalysis, UserPreference, db,
)


def test_contact_review_scrolls_without_showing_closed_dialog():
    """The open review constrains its scrolling body between fixed controls."""
    project = Path(__file__).resolve().parents[1]
    css = (project / 'static' / 'css' / 'ui-customer-detail.css').read_text(encoding='utf-8')
    assert '.cd-review[open] { display: flex; flex-direction: column; }' in css
    template = (
        project / 'templates' / 'partials' / 'customer_view_content.html'
    ).read_text(encoding='utf-8')
    dialog = BeautifulSoup(template, 'html.parser').select_one('#custScrapeReviewModal')
    assert 'cd-review' in dialog['class']
    assert dialog.select_one(':scope > .sb-dialog-header')
    assert dialog.select_one(':scope > .sb-dialog-body#custScrapeReviewBody')
    assert dialog.select_one(':scope > .sb-dialog-footer')


def test_customer_tab_panels_do_not_draw_an_extra_outer_bottom_divider():
    """Tab panels omit their enclosing divider without removing separators between records."""
    project = Path(__file__).resolve().parents[1]
    css = (project / 'static' / 'css' / 'ui-customer-detail.css').read_text(encoding='utf-8')
    assert '.cd-section.cd-panel { border-bottom: 0; }' in css
    assert '.cd-panel :is(.cd-engagement, .cd-note, .cd-opportunity):last-child,' in css
    assert '.cd-panel .cd-table tbody tr:last-child :is(th, td) { border-bottom: 0; }' in css
    for selector in ('.cd-section', '.cd-engagement', '.cd-note', '.cd-opportunity'):
        assert f'{selector} {{' in css
    assert '.cd-section { padding: 18px 0; border-bottom: 1px' in css


@pytest.mark.parametrize('partial,bounded', [
    ('customer_view_content.html', True),
    ('territory_view_content.html', True),
    ('pod_view_content.html', True),
    ('solution_engineer_view_content.html', True),
    ('seller_view_content.html', False),
])
def test_readable_width_only_matches_standalone_partial_roots(partial, bounded):
    """Shared domain fragments never inherit full-page caps inside a dialog."""
    project = Path(__file__).resolve().parents[1]
    template = (project / 'templates' / 'partials' / partial).read_text(encoding='utf-8')
    root = BeautifulSoup(template, 'html.parser').select_one('[data-sb-page-surface]')
    assert ('sb-directory-frame' in root['class']) is bounded
    assert ('sb-directory-frame-wide' in root['class']) is (
        partial != 'seller_view_content.html'
    )
    selector = '.sb-page-frame #main-content > [data-sb-page-surface].sb-directory-frame'
    css = (project / 'static' / 'css' / 'ui-page-frame.css').read_text(encoding='utf-8')
    assert selector in css
    standalone = BeautifulSoup(
        f'<body class="sb-page-frame"><main id="main-content">{root}</main></body>',
        'html.parser',
    )
    assert bool(standalone.select(selector)) is bounded
    embedded = BeautifulSoup(
        f'<body class="sb-page-frame"><main id="main-content">'
        f'<dialog><div>{root}</div></dialog></main></body>',
        'html.parser',
    )
    assert not embedded.select(selector)


@pytest.fixture
def dossier(app, sample_data):
    """Seed differing orders, conditional fields and real source relationships."""
    customer = db.session.get(Customer, sample_data['customer1_id'])
    customer.nickname = 'Short <name>'
    customer.account_context = 'Original\naccount context'
    customer.tpid_url = 'https://example.com/account?a=1&b=2'
    customer.website = 'example.com'
    customer.dae_name, customer.dae_alias = 'Account owner', 'owner'
    csam = CustomerCSAM(name='Available CSAM', alias='csam')
    customer.available_csams.append(csam)
    contacts = [CustomerContact(customer_id=customer.id, name='Z Contact', title='CTO',
                                email='z@example.com'),
                CustomerContact(customer_id=customer.id, name='A Contact')]
    opps = [Opportunity(customer_id=customer.id, name=name, msx_opportunity_id=name)
            for name in ['Alpha', 'Zulu', 'Middle']]
    db.session.add_all(contacts + opps)
    db.session.flush()
    milestones = [
        Milestone(customer_id=customer.id, opportunity_id=opps[1].id,
                  title='Source milestone', url='https://example.com/milestone',
                  due_date=datetime(2027, 1, 2), monthly_usage=1234.56,
                  msx_status='At Risk', workload='Data: Fabric'),
        Milestone(customer_id=customer.id, opportunity_id=opps[0].id,
                  title='Completed milestone', url='https://example.com/completed',
                  msx_status='Completed'),
    ]
    notes = [Note(customer_id=customer.id, content='<h2>Full note</h2><p>Original</p>',
                  call_date=datetime(2026, 10, 1, 13, 30)),
             Note(customer_id=customer.id, content='<p>Old note</p>',
                  call_date=datetime(2026, 1, 1))]
    engagements = [
        Engagement(customer_id=customer.id, title='Newest work', status='Lost',
                   created_at=datetime(2026, 10, 2, tzinfo=timezone.utc)),
        Engagement(customer_id=customer.id, title='Older work', status='Active',
                   created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                   notes=notes, opportunities=[opps[1]], milestones=milestones,
                   target_date=date(2027, 2, 3)),
    ]
    analysis = RevenueAnalysis(
        customer_name=customer.name, bucket='Raw bucket', category='DECLINING',
        months_analyzed=6, avg_revenue=1234.56, latest_revenue=1200,
        trend_slope=-0.125, dollars_at_risk=987.65, priority_score=85,
        recommended_action='CHECK-IN', confidence='HIGH')
    db.session.add_all(milestones + engagements + [analysis,
        Favorite(object_type='opportunity', object_id=opps[2].id)])
    db.session.commit()
    return {'customer': customer.id, 'notes': [notes[0].id, notes[1].id],
            'engagements': [engagements[0].id, engagements[1].id],
            'opportunities': [opps[2].id, opps[0].id, opps[1].id],
            'milestones': [ms.id for ms in milestones], 'analysis': analysis.id}


def parse(response):
    """Parse a successful HTML response."""
    assert response.status_code == 200
    return BeautifulSoup(response.data, 'html.parser')


@pytest.mark.parametrize('role', ['se', 'dss', 'seller-session'])
def test_exact_provider_populations_and_original_links(client, app, dossier, role):
    """Presentation keeps the original provider, ordering, values and role scope."""
    preference = UserPreference.query.first()
    preference.user_role = 'dss' if role == 'dss' else 'se'
    preference.my_seller_id = db.session.get(Customer, dossier['customer']).seller_id
    db.session.commit()
    if role == 'seller-session':
        with client.session_transaction() as session:
            session['seller_mode_seller_id'] = preference.my_seller_id
    original = subprocess.run(
        ['git', 'show', '4e57b1a6:templates/customer_view.html'], capture_output=True,
        text=True, check=True, encoding='utf-8').stdout
    original_opp = subprocess.run(
        ['git', 'show', '4e57b1a6:templates/partials/_customer_opportunities.html'],
        capture_output=True, text=True, check=True, encoding='utf-8').stdout
    original = original.replace("{% include 'partials/_customer_opportunities.html' %}",
                                original_opp)
    captured = {}
    baseline = []
    commits = []
    real_commit = db.session.commit

    def audit_commit():
        """Allow normal read telemetry while rejecting business mutations."""
        commits.append({
            'new': [type(record).__name__ for record in db.session.new],
            'dirty': [type(record).__name__ for record in db.session.dirty],
            'deleted': [type(record).__name__ for record in db.session.deleted],
        })
        real_commit()

    def capture(template, **context):
        """Capture the unmodified detail provider and render the actual template."""
        from flask import render_template
        captured.update(context)
        baseline.append(render_template_string(original, **context))
        return render_template(template, **context)

    with patch('app.routes.customers.render_template', side_effect=capture), \
            patch.object(db.session, 'commit', side_effect=audit_commit):
        response = client.get(f"/customer/{dossier['customer']}")
    assert commits == [{'new': ['UsageEvent'], 'dirty': [], 'deleted': []}]
    soup = parse(response)
    old = BeautifulSoup(baseline[0], 'html.parser')
    root = soup.select_one('[data-sb-customer-detail]')
    assert 'sb-directory-frame' in root['class']
    expected_links = [a['href'] for a in old.select('main a[href]')]
    assert sorted(a['href'] for a in root.select('a[href]')) == sorted(expected_links)
    assert [int(row['data-note-id']) for row in root.select('.cd-note')] == [
        note.id for note in captured['notes']]
    assert [row['data-created'] for row in root.select('.eng-card-col')] == [
        eng.created_at.isoformat() for eng in captured['engagements']]
    assert [int(row['data-opportunity-id']) for row in root.select('.cd-opportunity')] == [
        opp.id for opp in captured['sorted_opps']]
    assert [str(content) for content in root.select('.call-content')] == [
        str(content).replace('class="mb-2 call-content"', 'class="call-content"')
        for content in old.select('main .call-content')]
    for value in ['Short <name>', 'Original\naccount context', 'Account owner',
                  'A Contact', 'Z Contact', 'CTO', 'z@example.com', 'Available CSAM',
                  '$1,235/mo', '-12.5%', '$988 at risk', 'CHECK-IN',
                  'Source milestone', 'Data: Fabric', 'Jan 02, 2027']:
        assert value in root.get_text()
    assert root.select_one('#contextForm')['method'] == 'post'
    assert root.select_one('#contextForm')['action'].endswith('/overview')
    assert [field['name'] for field in root.select('#contextForm [name]')] == ['account_context']
    assert root.select_one('#contextTextarea').get_text() == 'Original\naccount context'
    assert not root.select('.card, .btn, .form-control, .form-select, [data-bs-dismiss]')
    assert sum(urlsplit(script['src']).path.endswith('/ui-customer-detail.js')
               for script in root.select('script[src]')) == 1
    assert sum(urlsplit(script['src']).path.endswith('/ui-table-sort.js')
               for script in soup.select('script[src]')) == 1
    assert len(root.select('dialog.sb-dialog')) == 3
    assert len(root.select('.cd-table thead th[data-sb-sort-type][data-export-label]')) == 6
    assert [td.get('data-sb-sort-value') for td in root.select('.cd-table tbody td')[1:4]] == [
        '1234.56', '-0.125', '987.65']
    assert root.select_one('a[href$="/revenue"]')
    assert root.select_one('a[href="https://example.com/account?a=1&b=2"]')
    assert 'active milestones' in soup.select_one('.sb-page-status').get_text()


def test_shared_info_and_pipeline_consumer_shapes(client, dossier):
    """The note-editor API keeps its exact JSON and alphabetical HTML scope."""
    cid = dossier['customer']
    data = client.get(f'/api/customer/{cid}/info').get_json()
    assert set(data) == {'id', 'name', 'nickname', 'tpid', 'tpid_url', 'territory',
                         'territory_id', 'seller_name', 'dae_name', 'dae_alias',
                         'verticals', 'email_domains', 'opportunities'}
    assert set(data['opportunities'][0]) == {'id', 'name', 'milestones'}
    milestones = [ms for opp in data['opportunities'] for ms in opp['milestones']]
    assert set(milestones[0]) == {
        'id', 'display_text', 'msx_status', 'workload', 'monthly_usage', 'due_date'}
    assert {ms['id'] for ms in milestones} == set(dossier['milestones'])
    fragment = parse(client.get(f'/api/customer/{cid}/opportunities-html'))
    assert [row.select_one('.cd-opportunity-title').get_text(' ', strip=True)
            for row in fragment.select('.cd-opportunity')] == ['Alpha', 'Middle', 'Zulu']
    assert fragment.select_one('.sb-customer-pipeline')
    assert not fragment.select('.card, .btn, [data-bs-toggle]')
    contacts = client.get(f'/api/customer/{cid}/contacts').get_json()
    assert [contact['name'] for contact in contacts] == ['A Contact', 'Z Contact']
    assert set(contacts[0]) == {'id', 'name', 'email', 'title', 'photo_b64', 'photo_full_b64'}


def test_context_manual_payload_success_and_failure(client, dossier):
    """AJAX manual context saving preserves its output and never reports failed writes as success."""
    path = f"/customer/{dossier['customer']}/overview"
    headers = {'X-Requested-With': 'XMLHttpRequest'}
    with patch('app.routes.customers._schedule_customer_backup') as backup:
        response = client.post(path, data={'account_context': '  Saved context  '},
                               headers=headers)
        assert response.get_json() == {'success': True, 'account_context': 'Saved context'}
        backup.assert_called_once_with(dossier['customer'])
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('isolated')), \
            patch('app.routes.customers._schedule_customer_backup') as backup:
        response = client.post(path, data={'account_context': 'Unsaved draft'}, headers=headers)
    assert response.status_code == 500
    assert response.get_json()['success'] is False
    backup.assert_not_called()
    assert db.session.get(Customer, dossier['customer']).account_context == 'Saved context'


def test_empty_and_missing_detail(client, sample_customer):
    """Empty accounts retain explicit destinations without invented revenue or pipeline."""
    soup = parse(client.get(f'/customer/{sample_customer.id}'))
    assert soup.select_one('#cdWorkstreams')
    assert soup.select_one('#cdNotes')
    assert not soup.select('#cdPipelineTab, #cdRevenueTab, #csamSection')
    assert 'No engagements yet' in soup.get_text()
    assert 'No notes for this customer yet' in soup.get_text()
    assert client.get('/customer/99999999').status_code == 404


def test_extracted_inline_script_syntax(client, dossier):
    """The complete moved inline controllers remain executable, not simplified copies."""
    soup = parse(client.get(f"/customer/{dossier['customer']}"))
    scratch = Path(__file__).resolve().parents[1] / '.checkpoint62-inline-syntax.js'
    try:
        for script in soup.select('[data-sb-customer-detail] script:not([src])'):
            scratch.write_text(script.get_text(), encoding='utf-8')
            result = subprocess.run(
                ['node', '--check', str(scratch)], text=True, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stderr
    finally:
        scratch.unlink(missing_ok=True)
