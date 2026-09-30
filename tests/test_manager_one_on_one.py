"""Initiative Tracker persistence, validation, navigation, and live data tests."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from sqlalchemy.exc import SQLAlchemyError

from app.models import (
    Customer, Engagement, ManagerDiscussedPoint, ManagerInitiativeItem,
    ManagerInitiativeSection, Milestone, Seller, U2CSnapshot, U2CSnapshotItem, db,
)
from app.services.salesiq_tools import execute_tool

API = '/api/reports/manager-one-on-one'


@pytest.fixture
def initiative_data(app) -> dict:
    """Create active work across two customers for report selection tests."""
    with app.app_context():
        customer = Customer(name='SQL Customer', nickname='SQL Alias', tpid=987001)
        other = Customer(name='Other Customer', tpid=987002)
        engagement = Engagement(customer=customer, title='SQL Renewal', status='Active')
        milestone = Milestone(
            customer=other, title='Commit this quarter', msx_milestone_id='INIT-MS-1',
            url='https://example.com/milestone', msx_status='On Track',
            customer_commitment='Uncommitted', monthly_usage=15000, on_my_team=True,
            due_date=datetime(2026, 12, 15, tzinfo=timezone.utc),
        )
        db.session.add_all([customer, other, engagement, milestone])
        db.session.commit()
        return {'engagement': engagement.id, 'milestone': milestone.id}


def create_section(client, name: str = 'SQL Renewals') -> dict:
    """Create a section through the API and read its persistent identifier."""
    response = client.post(f'{API}/sections', json={'name': name, 'description': 'Renewal focus'})
    assert response.status_code == 200
    return client.get(API).json['sections'][-1]


def add_work(client, section_id: int, item_type: str, entity_ids: list[int]):
    """Add a selection through the public report endpoint."""
    return client.post(f'{API}/sections/{section_id}/items', json={
        'item_type': item_type, 'entity_ids': entity_ids,
    })


@pytest.mark.parametrize('page_url', [
    '/reports/initiative-tracker', '/reports/manager-one-on-one',
])
def test_empty_report_and_navigation(client, page_url):
    """Use the new report name and URL while retaining saved links and the 1:1 report."""
    page = client.get(page_url)
    assert page.status_code == 200
    soup = BeautifulSoup(page.data, 'html.parser')
    assert soup.title.get_text(strip=True) == 'Initiative Tracker - Sales Buddy'
    assert soup.select_one('h2').get_text(strip=True) == 'Initiative Tracker'
    assert b'Create your first initiative' in page.data
    assert b'Track priorities, related work, and discussion points.' in page.data
    assert client.get(API).json['sections'] == []
    hub = client.get('/reports')
    assert b'/reports/initiative-tracker' in hub.data
    assert b'/reports/one-on-one' in hub.data
    tracker_nav = soup.select_one('#navReports a[href="/reports/initiative-tracker"]')
    assert tracker_nav.get_text(strip=True) == 'Initiative Tracker'
    meeting = BeautifulSoup(client.get('/reports/one-on-one').data, 'html.parser')
    assert meeting.title.get_text(strip=True) == '1:1 Report - Sales Buddy'
    assert meeting.select_one('h2').get_text(strip=True) == '1:1 Report'
    link = meeting.select_one('main a.btn[href="/reports/initiative-tracker"]')
    assert link is not None
    assert link.get_text(strip=True) == 'Initiative Tracker'


def test_initiative_labels_are_consistent(client):
    """Use initiative wording in creation, editing, navigation, and API errors."""
    create_section(client)
    soup = BeautifulSoup(client.get('/reports/initiative-tracker').data, 'html.parser')
    for panel in soup.select('.section-form'):
        assert panel.select_one('label').get_text(strip=True) == 'Initiative name'
    assert soup.select_one('#newSection [type="submit"]').get_text(strip=True) == (
        'Create initiative'
    )
    assert soup.select_one('[data-section-id] [type="submit"]').get_text(strip=True) == (
        'Save initiative'
    )
    assert soup.select_one('[data-action="delete-section"]').get_text(strip=True) == (
        'Delete initiative'
    )
    assert soup.select_one('[aria-label="Close initiative editor"]') is not None
    assert client.post(f'{API}/sections', json={}).json['error'] == (
        'Enter an initiative name of 1 to 200 characters.'
    )
    assert client.get(f'{API}/sections/999999/candidates').json['error'] == (
        'Initiative not found.'
    )


def test_section_create_edit_and_persist(client, app):
    """Names and context survive reloads and are escaped rather than executed."""
    section = create_section(client)
    response = client.patch(f"{API}/sections/{section['id']}", json={
        'name': '  Commit this quarter  ', 'description': '<script>alert(1)</script>',
    })
    assert response.status_code == 200
    with app.app_context():
        saved = db.session.get(ManagerInitiativeSection, section['id'])
        assert saved.name == 'Commit this quarter'
    html = client.get('/reports/manager-one-on-one').data.decode()
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '<script>alert(1)</script>' not in html


@pytest.mark.parametrize('data', [
    {}, [], {'name': ''}, {'name': '  '}, {'name': 'x' * 201},
    {'name': 4}, {'name': 'Valid', 'description': []},
    {'name': 'Valid', 'description': 'x' * 10001},
])
def test_invalid_sections_are_rejected(client, data):
    """Malformed section input cannot write partial or invalid report state."""
    assert client.post(f'{API}/sections', json=data).status_code == 400
    assert client.get(API).json['sections'] == []


def test_mixed_items_live_data_talking_points_and_tool(client, app, initiative_data):
    """Both item types show current data, saved notes, and navigable entity links."""
    section = create_section(client)
    for item_type, entity_id in initiative_data.items():
        assert add_work(client, section['id'], item_type, [entity_id]).status_code == 200
    with app.app_context():
        engagement = db.session.get(Engagement, initiative_data['engagement'])
        engagement.title = 'SQL Renewal updated'
        engagement.status = 'Completed'
        milestone = db.session.get(Milestone, initiative_data['milestone'])
        milestone.customer_commitment = 'Committed'
        db.session.commit()
    report = client.get(API).json
    items = report['sections'][0]['items']
    assert items[0]['title'] == 'SQL Renewal updated'
    assert items[0]['status'] == 'Completed'
    assert items[1]['commitment'] == 'Committed'
    assert items[1]['due_date'] == '2026-12-15'
    assert items[1]['acr'] == 15000
    assert client.patch(f"{API}/items/{items[0]['id']}", json={
        'talking_points': 'Ask for help with renewal',
    }).status_code == 200
    with app.test_request_context():
        tool_data = execute_tool('report_manager_one_on_one', {})
    assert tool_data == {'sections': client.get(API).json['sections']}
    assert tool_data['sections'][0]['items'][0]['talking_points'] == 'Ask for help with renewal'
    page = client.get('/reports/manager-one-on-one')
    assert page.status_code == 200
    assert b'SQL Renewal updated' in page.data
    assert client.get(f"{API}/items/{items[0]['id']}").json['item']['talking_points'] == (
        'Ask for help with renewal'
    )
    assert f"/engagement/{initiative_data['engagement']}".encode() in page.data


def test_compact_report_columns_and_autosave_fields(client, initiative_data):
    """Keep each row compact with separate customers and no per-row save chrome."""
    section = create_section(client)
    for item_type, entity_id in initiative_data.items():
        assert add_work(client, section['id'], item_type, [entity_id]).status_code == 200
    soup = BeautifulSoup(client.get('/reports/manager-one-on-one').data, 'html.parser')
    table = soup.select_one('.manager-work-table')
    assert [header.get_text(strip=True) for header in table.select('thead th')] == [
        'Work', 'Customer', 'Status', 'Due', 'ACR', 'Points', 'Actions',
    ]
    assert soup.select_one('aside') is None
    assert soup.select_one('nav[aria-label="Initiatives"]') is not None
    for row, customer in zip(table.select('tr.manager-work-row'), ['SQL Alias', 'Other Customer']):
        cells = row.select('td')
        assert len(cells) == 7
        assert cells[1].get_text(strip=True) == customer
        assert customer not in cells[0].get_text()
        assert cells[0].get_text(strip=True) not in {'Engagement', 'Milestone'}
        assert row['data-entity-id']
        assert row['data-item-type'] in {'engagement', 'milestone'}
        assert cells[0].select_one('.work-detail-link') is not None
        assert cells[5].select_one('[data-action="open-points"]') is not None
        assert cells[5].select('textarea, .talking-points-field') == []
    assert 'Points save automatically' in soup.get_text()
    assert soup.select_one('#managerPointsModal textarea')['rows'] == '5'
    assert soup.select_one('#managerWorkModal #managerWorkBody') is not None
    assert soup.select_one('iframe') is None
    assert soup.select_one('#retryTalkingPoints') is not None


def test_each_section_groups_mixed_work_by_current_seller(client, app, initiative_data):
    """Sort seller groups by name, keep mixed work together, and put unassigned work last."""
    section = create_section(client)
    with app.app_context():
        alpha = Seller(name='alpha Seller')
        zulu = Seller(name='Zulu Seller')
        engagement = db.session.get(Engagement, initiative_data['engagement'])
        milestone = db.session.get(Milestone, initiative_data['milestone'])
        engagement.customer.seller = zulu
        milestone.customer.seller = alpha
        second_engagement = Engagement(
            customer=milestone.customer, title='Second initiative engagement', status='Active',
        )
        unassigned = Engagement(
            customer=Customer(name='Unassigned Customer', tpid=987003),
            title='Unassigned initiative engagement', status='Active',
        )
        db.session.add_all([alpha, zulu, second_engagement, unassigned])
        db.session.commit()
        alpha_id, zulu_id = alpha.id, zulu.id
        extra_ids = [second_engagement.id, unassigned.id]
    assert add_work(client, section['id'], 'engagement', [
        initiative_data['engagement'], *extra_ids,
    ]).status_code == 200
    assert add_work(
        client, section['id'], 'milestone', [initiative_data['milestone']],
    ).status_code == 200
    report_section = client.get(API).json['sections'][0]
    groups = report_section['seller_groups']
    assert [group['seller_id'] for group in groups] == [alpha_id, zulu_id, None]
    assert [group['seller_name'] for group in groups] == ['alpha Seller', 'Zulu Seller', 'No seller']
    assert [item['item_type'] for item in groups[0]['items']] == ['engagement', 'milestone']
    assert len(report_section['items']) == sum(len(group['items']) for group in groups) == 4
    soup = BeautifulSoup(client.get('/reports/manager-one-on-one').data, 'html.parser')
    bodies = soup.select('.manager-work-table tbody')
    for body in bodies:
        link = body.select_one('.seller-notes-link')
        if body['data-seller-id'] == 'none':
            assert link is None
        else:
            assert link.get_text(strip=True) == '(1:1 notes)'
            assert link['data-seller-id'] == body['data-seller-id']
            assert link['href'] == f"/seller/{body['data-seller-id']}/one-on-one"
    assert [body['data-seller-id'] for body in bodies] == [str(alpha_id), str(zulu_id), 'none']
    for body, seller_id in zip(bodies[:2], [alpha_id, zulu_id]):
        heading_link = body.select_one('th[scope="rowgroup"] a.seller-group-link')
        assert heading_link['href'] == f'/seller/{seller_id}'
        assert heading_link.select_one('i.bi-person') is not None
        assert 'fw-semibold' in heading_link['class']
        assert 'badge' not in heading_link['class']
        assert body.select_one('th[scope="rowgroup"]').get_text(strip=True) == (
            heading_link.get_text(strip=True) + '(1:1 notes)'
        )
    assert bodies[-1].select_one('th[scope="rowgroup"]').get_text(strip=True) == 'No seller'
    assert [len(body.select('tr.manager-work-row')) for body in bodies] == [2, 1, 1]


def test_seller_grouping_tracks_reassignment_and_rename(client, app, initiative_data):
    """A seller change on the source customer updates the report without replacing its links."""
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    original_item = client.get(API).json['sections'][0]['items'][0]
    with app.app_context():
        seller = Seller(name='Original Seller')
        db.session.get(Engagement, initiative_data['engagement']).customer.seller = seller
        db.session.add(seller)
        db.session.commit()
        seller_id = seller.id
    groups = client.get(API).json['sections'][0]['seller_groups']
    assert groups[0]['seller_id'] == seller_id
    assert groups[0]['seller_name'] == 'Original Seller'
    assert groups[0]['items'][0]['id'] == original_item['id']
    with app.app_context():
        db.session.get(Seller, seller_id).name = 'Renamed Seller'
        db.session.commit()
    assert client.get(API).json['sections'][0]['seller_groups'][0]['seller_name'] == 'Renamed Seller'
    with app.app_context():
        db.session.get(Engagement, initiative_data['engagement']).customer.seller = None
        db.session.commit()
    groups = client.get(API).json['sections'][0]['seller_groups']
    assert groups[0]['seller_id'] is None
    assert groups[0]['seller_name'] == 'No seller'


def test_same_named_sellers_remain_separate_groups(client, app, initiative_data):
    """Grouping uses seller identity rather than merging two people with the same name."""
    section = create_section(client)
    with app.app_context():
        first = Seller(name='Same Name')
        second = Seller(name='Same Name')
        db.session.get(Engagement, initiative_data['engagement']).customer.seller = first
        db.session.get(Milestone, initiative_data['milestone']).customer.seller = second
        db.session.add_all([first, second])
        db.session.commit()
        seller_ids = [first.id, second.id]
    for item_type, entity_id in initiative_data.items():
        add_work(client, section['id'], item_type, [entity_id])
    groups = client.get(API).json['sections'][0]['seller_groups']
    assert [group['seller_id'] for group in groups] == seller_ids
    assert [len(group['items']) for group in groups] == [1, 1]


def test_same_work_can_be_in_multiple_sections_not_twice_in_one(client, initiative_data):
    """Initiatives overlap without duplicating an entity inside the same section."""
    first = create_section(client)
    second = create_section(client, 'Commit this quarter')
    for section in [first, second]:
        assert add_work(
            client, section['id'], 'engagement', [initiative_data['engagement']],
        ).status_code == 200
    assert add_work(
        client, first['id'], 'engagement', [initiative_data['engagement']],
    ).status_code == 409
    assert len(client.get(API).json['sections'][0]['items']) == 1


def test_removing_links_and_sections_preserves_source_work(client, app, initiative_data):
    """Deleting report items and entire sections never deletes engagements or milestones."""
    section = create_section(client)
    for item_type, entity_id in initiative_data.items():
        assert add_work(client, section['id'], item_type, [entity_id]).status_code == 200
    item = client.get(API).json['sections'][0]['items'][0]
    assert client.delete(f"{API}/items/{item['id']}").status_code == 200
    assert client.delete(f"{API}/sections/{section['id']}").status_code == 200
    with app.app_context():
        assert ManagerInitiativeItem.query.count() == 0
        assert db.session.get(Engagement, initiative_data['engagement']) is not None
        assert db.session.get(Milestone, initiative_data['milestone']) is not None


@pytest.mark.parametrize('payload', [
    {}, [], {'item_type': [], 'entity_ids': [1]},
    {'item_type': 'unknown', 'entity_ids': [1]},
    {'item_type': 'milestone', 'entity_ids': []},
    {'item_type': 'milestone', 'entity_ids': [True]},
    {'item_type': 'milestone', 'entity_ids': ['1']},
    {'item_type': 'milestone', 'entity_ids': [1, 1]},
    {'item_type': 'milestone', 'entity_ids': list(range(1, 77))},
])
def test_invalid_item_selection(client, payload):
    """Invalid bulk input produces useful errors and never partial writes."""
    section = create_section(client)
    assert client.post(f"{API}/sections/{section['id']}/items", json=payload).status_code == 400
    assert client.get(API).json['sections'][0]['items'] == []


def test_missing_entity_rejects_entire_batch(client, initiative_data):
    """A stale selection must not save any of the remaining records."""
    section = create_section(client)
    assert add_work(client, section['id'], 'engagement', [
        initiative_data['engagement'], 999999,
    ]).status_code == 404
    assert client.get(API).json['sections'][0]['items'] == []


def test_candidates_search_customer_and_exclude_per_section(client, initiative_data):
    """The picker searches both books and excludes only the chosen section's links."""
    first = create_section(client)
    second = create_section(client, 'Second initiative')
    url = f"{API}/sections/{first['id']}/candidates"
    engagement = client.get(f'{url}?type=engagement&q=SQL+Alias').json['results']
    assert [item['id'] for item in engagement] == [initiative_data['engagement']]
    milestones = client.get(f'{url}?type=milestone').json['results']
    assert [item['id'] for item in milestones] == [initiative_data['milestone']]
    assert milestones[0]['commitment'] == 'Uncommitted'
    assert client.get(f'{url}?type=unknown').status_code == 400
    add_work(client, first['id'], 'engagement', [initiative_data['engagement']])
    assert client.get(f'{url}?type=engagement').json['results'] == []
    other = client.get(f"{API}/sections/{second['id']}/candidates?type=engagement")
    assert len(other.json['results']) == 1


def test_unavailable_link_does_not_hide_available_candidates(client, app, initiative_data):
    """A NULL source ID must not turn the exclusion query into NOT IN (NULL)."""
    section = create_section(client)
    with app.app_context():
        db.session.add(ManagerInitiativeItem(
            section_id=section['id'], item_type='engagement',
            title_snapshot='Deleted work', customer_snapshot='Former customer',
        ))
        db.session.commit()
    results = client.get(
        f"{API}/sections/{section['id']}/candidates?type=engagement"
    ).json['results']
    assert [item['id'] for item in results] == [initiative_data['engagement']]


@pytest.mark.parametrize('item_type', ['engagement', 'milestone'])
def test_unavailable_entity_uses_snapshot(client, app, initiative_data, item_type):
    """A deleted source retains enough report context to remove the stale link."""
    section = create_section(client)
    add_work(client, section['id'], item_type, [initiative_data[item_type]])
    with app.app_context():
        model = Engagement if item_type == 'engagement' else Milestone
        db.session.delete(db.session.get(model, initiative_data[item_type]))
        db.session.commit()
    item = client.get(API).json['sections'][0]['items'][0]
    assert item['available'] is False
    assert item['title'] == ('SQL Renewal' if item_type == 'engagement' else 'Commit this quarter')
    assert item['customer_name'] == ('SQL Alias' if item_type == 'engagement' else 'Other Customer')
    assert item['status'] == 'Record unavailable'
    group = client.get(API).json['sections'][0]['seller_groups'][0]
    assert group['seller_id'] is None
    assert group['seller_name'] == 'No seller'
    assert client.get('/reports/manager-one-on-one').status_code == 200


def test_candidate_limit_is_applied_after_excluding_selected_work(client, app, initiative_data):
    """Already-selected work cannot consume the 75-result search window."""
    section = create_section(client)
    with app.app_context():
        customer = Engagement.query.first().customer
        for number in range(80):
            db.session.add(Engagement(
                customer=customer, title=f'Candidate {number:02d}', status='Active',
                estimated_acr=number,
            ))
        db.session.commit()
    url = f"{API}/sections/{section['id']}/candidates?type=engagement"
    initial = client.get(url).json['results']
    assert len(initial) == 75
    assert add_work(client, section['id'], 'engagement', [
        item['id'] for item in initial
    ]).status_code == 200
    remaining = client.get(url).json['results']
    assert len(remaining) == 6
    assert not {item['id'] for item in initial}.intersection(item['id'] for item in remaining)


def test_database_error_rolls_back_and_reports_failure(client, app):
    """A failed save is logged, returns failure, and leaves no apparent saved section."""
    with app.app_context(), patch.object(
        db.session, 'commit', side_effect=SQLAlchemyError('test database failure'),
    ):
        response = client.post(f'{API}/sections', json={'name': 'Failing section'})
    assert response.status_code == 500
    assert response.json['success'] is False
    assert client.get(API).json['sections'] == []


def test_missing_routes_and_invalid_talking_points(client, initiative_data):
    """Unknown links and invalid notes have explicit not-found or validation errors."""
    assert client.patch(f'{API}/sections/999999', json={'name': 'Missing'}).status_code == 404
    assert client.patch(f'{API}/sections/0', json={'name': 'Missing'}).status_code == 404
    assert client.delete(f'{API}/sections/999999').status_code == 404
    assert client.get(f'{API}/sections/999999/candidates').status_code == 404
    assert add_work(client, 999999, 'engagement', [1]).status_code == 404
    assert client.delete(f'{API}/items/999999').status_code == 404
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    item = client.get(API).json['sections'][0]['items'][0]
    for points in [None, [], 'x' * 10001]:
        assert client.patch(f"{API}/items/{item['id']}", json={
            'talking_points': points,
        }).status_code == 400


def test_points_archive_separate_blocks_with_utc_timestamps(client, app, initiative_data):
    """Each discussed block is immutable, dated, ordered, and available to the read tool."""
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    item = client.get(API).json['sections'][0]['items'][0]
    url = f"{API}/items/{item['id']}"
    before = datetime.now(timezone.utc)
    first = client.patch(url, json={'talking_points': 'First draft'}).json['item']
    created = first['points_created_at']
    assert before <= datetime.fromisoformat(created) <= datetime.now(timezone.utc)
    edited = client.patch(url, json={'talking_points': 'First meeting\nAsk for support'}).json['item']
    assert edited['points_created_at'] == created
    first_archive = client.post(f'{url}/discuss', json={
        'talking_points': edited['talking_points'],
    })
    assert first_archive.status_code == 200
    archived = first_archive.json['item']
    assert archived['talking_points'] == ''
    assert archived['points_created_at'] is None
    note = archived['discussed_points'][0]
    assert note['text'] == 'First meeting\nAsk for support'
    assert note['created_at'] == created
    assert datetime.fromisoformat(note['discussed_at']) >= (
        datetime.fromisoformat(created)
    )
    next_block = client.patch(url, json={'talking_points': 'Next meeting'}).json['item']
    assert next_block['points_created_at'] != created
    second = client.post(f'{url}/discuss', json={'talking_points': 'Next meeting'}).json['item']
    assert [point['text'] for point in second['discussed_points']] == [
        'Next meeting', 'First meeting\nAsk for support',
    ]
    assert second['discussed_points'][1] == note
    assert client.get(url).json['item'] == second
    with app.test_request_context():
        tool = execute_tool('report_manager_one_on_one', {})
    assert tool['sections'][0]['items'][0]['discussed_points'] == second['discussed_points']
    with app.app_context():
        assert ManagerDiscussedPoint.query.count() == 2


def test_discuss_rejects_stale_and_duplicate_requests(client, initiative_data):
    """Do not archive somebody else's newer edit or record the same meeting twice."""
    section = create_section(client)
    add_work(client, section['id'], 'milestone', [initiative_data['milestone']])
    item = client.get(API).json['sections'][0]['items'][0]
    url = f"{API}/items/{item['id']}"
    client.patch(url, json={'talking_points': 'Newer edit'})
    assert client.post(f'{url}/discuss', json={'talking_points': 'Stale edit'}).status_code == 409
    assert client.get(url).json['item']['talking_points'] == 'Newer edit'
    assert client.get(url).json['item']['discussed_points'] == []
    assert client.post(f'{url}/discuss', json={'talking_points': 'Newer edit'}).status_code == 200
    assert client.post(f'{url}/discuss', json={'talking_points': 'Newer edit'}).status_code == 409
    assert len(client.get(url).json['item']['discussed_points']) == 1


@pytest.mark.parametrize('points', [None, [], '', '   ', 'x' * 10001])
def test_discuss_requires_nonempty_valid_points(client, initiative_data, points):
    """Invalid completion requests cannot create history or clear the current block."""
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    item = client.get(API).json['sections'][0]['items'][0]
    url = f"{API}/items/{item['id']}"
    client.patch(url, json={'talking_points': 'Keep me'})
    assert client.post(f'{url}/discuss', json={'talking_points': points}).status_code == 400
    assert client.get(url).json['item']['talking_points'] == 'Keep me'
    assert client.get(url).json['item']['discussed_points'] == []


def test_archiving_failure_preserves_the_current_block(client, app, initiative_data):
    """Failed commits roll back both the cleared editor and the new history record."""
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    item = client.get(API).json['sections'][0]['items'][0]
    url = f"{API}/items/{item['id']}"
    saved = client.patch(url, json={'talking_points': 'Keep me'}).json['item']
    with app.app_context(), patch.object(
        db.session, 'commit', side_effect=SQLAlchemyError('archive failure'),
    ):
        response = client.post(f'{url}/discuss', json={'talking_points': 'Keep me'})
    assert response.status_code == 500
    assert client.get(url).json['item'] == saved


def test_history_is_scoped_to_initiative_link_and_survives_source_deletion(
    client, app, initiative_data,
):
    """Shared work has independent agendas and stale source links retain their discussion."""
    first = create_section(client)
    second = create_section(client, 'Second focus')
    for section in [first, second]:
        add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    items = [section['items'][0] for section in client.get(API).json['sections']]
    url = f"{API}/items/{items[0]['id']}"
    client.patch(url, json={'talking_points': 'Discussed here only'})
    client.post(f'{url}/discuss', json={'talking_points': 'Discussed here only'})
    with app.app_context():
        db.session.delete(db.session.get(Engagement, initiative_data['engagement']))
        db.session.commit()
    assert len(client.get(url).json['item']['discussed_points']) == 1
    other = client.get(f"{API}/items/{items[1]['id']}").json['item']
    assert other['discussed_points'] == []
    assert not other['available']
    assert client.delete(url).status_code == 200
    with app.app_context():
        assert ManagerDiscussedPoint.query.count() == 0


def test_legacy_points_do_not_invent_a_creation_timestamp(client, app, initiative_data):
    """Existing blocks keep their text without claiming a historical creation time."""
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    item = client.get(API).json['sections'][0]['items'][0]
    with app.app_context():
        saved = db.session.get(ManagerInitiativeItem, item['id'])
        saved.talking_points = 'Existing points'
        db.session.commit()
    response = client.post(f"{API}/items/{item['id']}/discuss", json={
        'talking_points': 'Existing points',
    })
    assert response.status_code == 200
    assert response.json['item']['discussed_points'][0]['created_at'] is None
    assert client.get(f'{API}/items/999999').status_code == 404
    assert client.post(f'{API}/items/999999/discuss', json={'talking_points': 'X'}).status_code == 404


def test_points_timestamp_migration_is_additive_and_idempotent():
    """An older item table retains its points and gains the nullable timestamp once."""
    from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, inspect, select
    from sqlalchemy.orm import Session
    from app.migrations import _add_column_if_not_exists

    engine = create_engine('sqlite://')
    metadata = MetaData()
    legacy = Table(
        'manager_initiative_items', metadata,
        Column('id', Integer, primary_key=True), Column('talking_points', Text),
    )
    metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(legacy.insert().values(id=1, talking_points='Existing agenda'))
        session.commit()
        database = SimpleNamespace(session=session)
        for _ in range(2):
            _add_column_if_not_exists(
                database, inspect(engine), 'manager_initiative_items',
                'points_created_at', 'DATETIME',
            )
        columns = inspect(engine).get_columns('manager_initiative_items')
        assert [column['name'] for column in columns].count('points_created_at') == 1
        assert session.execute(select(legacy.c.talking_points)).scalar_one() == 'Existing agenda'
    engine.dispose()


def test_clearing_current_points_resets_creation_time(client, initiative_data):
    """Blank drafts do not carry an earlier conversation's creation time into a new block."""
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    item = client.get(API).json['sections'][0]['items'][0]
    url = f"{API}/items/{item['id']}"
    first = client.patch(url, json={'talking_points': 'Discard this'}).json['item']
    blank = client.patch(url, json={'talking_points': '  '}).json['item']
    assert blank['points_created_at'] is None
    fresh = client.patch(url, json={'talking_points': 'New block'}).json['item']
    assert fresh['points_created_at'] != first['points_created_at']
    assert fresh['discussed_points'] == []


@pytest.fixture
def manager_u2c_data(app, monkeypatch) -> dict:
    """Build a current baseline with live, converted, missing, and out-of-quarter records."""
    monkeypatch.setattr(
        'app.services.u2c_snapshot.current_fiscal_quarter', lambda: 'FY27 Q1',
    )
    with app.app_context():
        customer = Customer(name='Snapshot Customer', nickname='Snapshot Alias', tpid=987700)
        snapshot = U2CSnapshot(
            fiscal_quarter='FY27 Q1', snapshot_date=datetime(2026, 9, 29, tzinfo=timezone.utc),
            msxi_version='20260922',
        )
        db.session.add_all([customer, snapshot])
        db.session.flush()
        ids = {}
        for name, commitment, status, baseline_due in [
            ('remaining', 'Uncommitted', 'On Track', datetime(2026, 9, 30, 23, 59, 59)),
            ('blocked', 'Uncommitted', 'Blocked', datetime(2026, 7, 1)),
            ('committed', 'Committed', 'On Track', datetime(2026, 9, 15)),
            ('completed', 'Uncommitted', 'Completed', datetime(2026, 9, 15)),
            ('cancelled', 'Uncommitted', 'Cancelled', datetime(2026, 9, 15)),
            ('hygiene', 'Uncommitted', 'Hygiene/Duplicate', datetime(2026, 9, 15)),
            ('outside', 'Uncommitted', 'On Track', datetime(2026, 10, 1)),
        ]:
            milestone = Milestone(
                customer=customer, title=f'Live {name}', url=f'https://example.com/{name}',
                msx_status=status, customer_commitment=commitment, monthly_usage=1000,
                due_date=datetime(2026, 12, 1), workload='Data: SQL',
            )
            db.session.add(milestone)
            db.session.flush()
            ids[name] = milestone.id
            db.session.add(U2CSnapshotItem(
                snapshot=snapshot, milestone=milestone, customer=customer,
                customer_name='Frozen Customer', milestone_title=f'Frozen {name}',
                due_date=baseline_due, monthly_acr=2000, msx_status='On Track',
                msxi_commitment='Uncommitted', workload='Data: SQL',
            ))
        db.session.add(U2CSnapshotItem(
            snapshot=snapshot, milestone_id=ids['remaining'], customer=customer,
            customer_name='Frozen Customer', milestone_title='Duplicate remaining',
            due_date=datetime(2026, 9, 15), monthly_acr=3000, msx_status='On Track',
            workload='Data: SQL',
        ))
        for name, commitment in [('Missing locally', 'Uncommitted'), ('Converted missing', 'Committed')]:
            db.session.add(U2CSnapshotItem(
                snapshot=snapshot, customer_name='Not Synced Corp', milestone_title=name,
                milestone_number=name, due_date=datetime(2026, 9, 15),
                monthly_acr=4000, msx_status='On Track',
                msxi_status='On Track', msxi_commitment=commitment,
                workload='Data: SQL',
            ))
        unrelated = Milestone(
            customer=customer, title='Not in snapshot', url='https://example.com/unrelated',
            msx_status='On Track', customer_commitment='Uncommitted',
        )
        db.session.add(unrelated)
        db.session.commit()
        return {**ids, 'snapshot_id': snapshot.id, 'unrelated': unrelated.id}


def test_u2c_candidates_reuse_current_quarter_baseline_and_live_details(client, manager_u2c_data):
    """Use baseline quarter eligibility and live commitment, without inventing local records."""
    section = create_section(client)
    response = client.get(
        f"{API}/sections/{section['id']}/candidates?type=milestone&source=u2c",
    )
    assert response.status_code == 200
    payload = response.json
    assert payload['snapshot']['fiscal_quarter'] == 'FY27 Q1'
    assert payload['snapshot']['version_date'] == '2026-09-22'
    assert payload['snapshot']['snapshot_date'].endswith('+00:00')
    local = [row for row in payload['results'] if row['available']]
    assert {row['id'] for row in local} == {
        manager_u2c_data['remaining'], manager_u2c_data['blocked'],
        manager_u2c_data['cancelled'], manager_u2c_data['hygiene'],
    }
    assert len(local) == 4
    assert all(row['selectable'] and not row['already_added'] for row in local)
    assert all(row['customer_name'] == 'Snapshot Alias' for row in local)
    assert all(row['due_date'] == '2026-12-01' for row in local)
    assert all(row['acr'] == 1000 for row in local)
    missing = [row for row in payload['results'] if not row['available']]
    assert len(missing) == 1
    assert missing[0]['id'] is None
    assert not missing[0]['selectable']
    assert missing[0]['reason'] == 'Not synced locally'
    assert missing[0]['title'] == 'Missing locally'


def test_u2c_picker_marks_existing_links_and_keeps_sections_curated(
    client, app, manager_u2c_data,
):
    """Show already-added rows, allow reuse elsewhere, and never auto-remove snapshot links."""
    first = create_section(client)
    second = create_section(client, 'Another focus')
    entity_id = manager_u2c_data['remaining']
    add_work(client, first['id'], 'milestone', [entity_id])
    item = client.get(API).json['sections'][0]['items'][0]
    client.patch(f"{API}/items/{item['id']}", json={'talking_points': 'Keep this agenda'})
    for section, added in [(first, True), (second, False)]:
        rows = client.get(
            f"{API}/sections/{section['id']}/candidates?type=milestone&source=u2c",
        ).json['results']
        row = next(row for row in rows if row['id'] == entity_id)
        assert row['already_added'] is added
        assert row['selectable'] is not added
    with app.app_context():
        db.session.delete(db.session.get(U2CSnapshot, manager_u2c_data['snapshot_id']))
        db.session.commit()
    saved = client.get(API).json['sections'][0]['items'][0]
    assert saved['entity_id'] == entity_id
    assert saved['talking_points'] == 'Keep this agenda'
    assert client.get(API).json['sections'][1]['items'] == []


@pytest.mark.parametrize('search, expected', [
    ('snapshot alias', {'Live remaining', 'Live blocked', 'Live cancelled', 'Live hygiene'}),
    ('data: sql', {'Live remaining', 'Live blocked', 'Live cancelled', 'Live hygiene', 'Missing locally'}),
    ('frozen remaining', {'Live remaining'}),
    ('Not Synced', {'Missing locally'}),
    ('no matching work', set()),
])
def test_u2c_candidates_search_live_and_frozen_context(
    client, manager_u2c_data, search, expected,
):
    """Search works within the assisted source, including not-yet-synced baseline rows."""
    section = create_section(client)
    response = client.get(f"{API}/sections/{section['id']}/candidates", query_string={
        'type': 'milestone', 'source': 'u2c', 'q': search,
    })
    assert {row['title'] for row in response.json['results']} == expected


def test_missing_current_u2c_snapshot_does_not_fall_back_to_an_old_quarter(
    client, app, monkeypatch,
):
    """A missing snapshot is an explicit empty state, not an old-quarter substitution."""
    monkeypatch.setattr('app.services.u2c_snapshot.current_fiscal_quarter', lambda: 'FY27 Q2')
    with app.app_context():
        db.session.add(U2CSnapshot(fiscal_quarter='FY27 Q1'))
        db.session.commit()
    section = create_section(client)
    payload = client.get(
        f"{API}/sections/{section['id']}/candidates?type=milestone&source=u2c",
    ).json
    assert payload['snapshot'] is None
    assert payload['results'] == []
    assert payload['fiscal_quarter'] == 'FY27 Q2'
    assert 'No FY27 Q2 U2C snapshot' in payload['message']


def test_u2c_source_validation_and_normal_search_are_independent(client, manager_u2c_data):
    """U2C is milestone-only and cannot change the existing normal candidate search."""
    section = create_section(client)
    url = f"{API}/sections/{section['id']}/candidates"
    assert client.get(f'{url}?type=engagement&source=u2c').status_code == 400
    assert client.get(f'{url}?type=milestone&source=unknown').status_code == 400
    assert client.get(
        f'{API}/sections/999999/candidates?type=milestone&source=u2c',
    ).status_code == 404
    ids = {row['id'] for row in client.get(f'{url}?type=milestone').json['results']}
    assert manager_u2c_data['unrelated'] in ids


def test_u2c_read_tool_uses_the_same_candidate_service(client, app, manager_u2c_data):
    """SalesIQ can discover the same snapshot selections without duplicate business logic."""
    section = create_section(client)
    expected = client.get(
        f"{API}/sections/{section['id']}/candidates?type=milestone&source=u2c&q=blocked",
    ).json
    with app.test_request_context():
        tool_data = execute_tool('get_manager_u2c_candidates', {'search': 'blocked'})
    assert tool_data == {key: value for key, value in expected.items() if key != 'success'}


def test_section_forms_are_unfilled_and_u2c_controls_are_milestone_specific(
    client, initiative_data,
):
    """Outline unfilled forms and keep the assisted source inside the milestone picker."""
    section = create_section(client)
    add_work(client, section['id'], 'engagement', [initiative_data['engagement']])
    soup = BeautifulSoup(client.get('/reports/manager-one-on-one').data, 'html.parser')
    for panel in soup.select('.section-form, .work-picker'):
        assert 'bg-body-tertiary' not in panel['class']
        assert 'rounded' not in panel['class']
        assert 'border' in panel['class']
        assert 'p-3' in panel['class']
    assert soup.select_one('#newSection .col-5 [name="name"]') is not None
    source = soup.select_one('.work-picker .milestone-source')
    assert 'd-none' in source['class']
    assert source.select_one('[data-source="u2c"]').get_text(strip=True) == 'From U2C'
    assert source.select_one('[href="/reports/u2c"]') is not None


def test_section_panels_have_consistent_header_close_controls(client):
    """Use a labelled non-submit close button in each panel header, not footer Cancel."""
    section = create_section(client)
    soup = BeautifulSoup(client.get('/reports/manager-one-on-one').data, 'html.parser')
    panels = soup.select('.section-form, .work-picker')
    assert len(panels) == 3
    for panel in panels:
        header = panel.select_one('div')
        assert header.select_one('h6') is not None
        close = header.select_one('button.btn-close')
        assert close is not None
        assert close['type'] == 'button'
        assert close['aria-label'].startswith('Close ')
        assert len(panel.select('.btn-close')) == 1
        assert all(button.get_text(strip=True) != 'Cancel' for button in panel.select('button'))
    for target in ('newSection', f"edit-{section['id']}"):
        close = soup.select_one(f'#{target} .btn-close')
        assert close['data-bs-toggle'] == 'collapse'
        assert close['data-bs-target'] == f'#{target}'
        assert close['aria-controls'] == target
    assert soup.select_one('.work-picker .btn-close')['data-action'] == 'close-picker'


def test_u2c_source_is_not_truncated_by_normal_search_limit(client, app, manager_u2c_data):
    """All snapshot candidates stay discoverable, even when adding in 75-item batches."""
    with app.app_context():
        snapshot = db.session.get(U2CSnapshot, manager_u2c_data['snapshot_id'])
        customer = Milestone.query.first().customer
        for number in range(80):
            milestone = Milestone(
                customer=customer, title=f'Bulk snapshot {number}',
                url=f'https://example.com/bulk-{number}', msx_status='On Track',
                customer_commitment='Uncommitted',
            )
            db.session.add(U2CSnapshotItem(
                snapshot=snapshot, milestone=milestone, customer=customer,
                customer_name=customer.name, milestone_title=milestone.title,
                due_date=datetime(2026, 9, 15), monthly_acr=1000, msx_status='On Track',
            ))
        db.session.commit()
    section = create_section(client)
    url = f"{API}/sections/{section['id']}/candidates?type=milestone&source=u2c&q=Bulk"
    rows = client.get(url).json['results']
    assert len(rows) == 80
    assert add_work(
        client, section['id'], 'milestone', [row['id'] for row in rows[:75]],
    ).status_code == 200
    updated = client.get(url).json['results']
    assert len(updated) == 80
    assert sum(row['already_added'] for row in updated) == 75
    assert sum(row['selectable'] for row in updated) == 5


def test_u2c_picker_workload_scope_matches_baseline_not_changed_live_workload(
    client, app, manager_u2c_data,
):
    """The picker applies the U2C page's delimiter-aware filter to snapshot workloads."""
    with app.app_context():
        blocked = U2CSnapshotItem.query.filter_by(
            milestone_id=manager_u2c_data['blocked'],
        ).one()
        blocked.workload = 'Infra: Virtual Machines'
        db.session.get(Milestone, manager_u2c_data['remaining']).workload = 'AI: Changed locally'
        db.session.add(U2CSnapshotItem(
            snapshot_id=manager_u2c_data['snapshot_id'],
            customer_name='Prefix edge case', milestone_title='Not a Data workload',
            workload='Database: Unrelated', due_date=datetime(2026, 9, 15),
            monthly_acr=1000, msxi_status='On Track', msxi_commitment='Uncommitted',
        ))
        db.session.commit()
    section = create_section(client)
    url = f"{API}/sections/{section['id']}/candidates"
    payload = client.get(url, query_string={
        'type': 'milestone', 'source': 'u2c', 'workload_prefix': 'Data',
    }).json
    assert payload['workload_prefix'] == 'Data'
    assert {row['title'] for row in payload['results']} == {
        'Live remaining', 'Live cancelled', 'Live hygiene', 'Missing locally',
    }
    assert next(row for row in payload['results'] if row['available'])['detail'] == 'AI: Changed locally'
    infra = client.get(url, query_string={
        'type': 'milestone', 'source': 'u2c', 'workload_prefix': 'Infra',
    }).json
    assert {row['title'] for row in infra['results']} == {'Live blocked'}
    all_workloads = client.get(f'{url}?type=milestone&source=u2c').json
    assert all_workloads['workload_prefix'] == ''
    assert len(all_workloads['results']) == 6
    with app.test_request_context():
        tool_data = execute_tool('get_manager_u2c_candidates', {'workload_prefix': 'Data'})
    assert tool_data == {key: value for key, value in payload.items() if key != 'success'}


def test_u2c_picker_offers_hygiene_and_cancelled_rows_from_remaining_list(
    client, app, manager_u2c_data,
):
    """Do not silently exclude remaining U2C work by applying normal search status rules."""
    section = create_section(client)
    with app.app_context():
        milestone = db.session.get(Milestone, manager_u2c_data['hygiene'])
        milestone.milestone_number = '7-503838749'
        U2CSnapshotItem.query.filter_by(milestone_id=milestone.id).one().milestone_number = (
            '7-503838749'
        )
        db.session.commit()
    url = f"{API}/sections/{section['id']}/candidates"
    response = client.get(url, query_string={
        'type': 'milestone', 'source': 'u2c', 'workload_prefix': 'Data', 'q': '7-503838749',
    })
    assert response.status_code == 200
    rows = response.json['results']
    assert len(rows) == 1
    assert rows[0]['status'] == 'Hygiene/Duplicate'
    assert rows[0]['selectable']
    assert rows[0]['id'] == manager_u2c_data['hygiene']
    assert add_work(client, section['id'], 'milestone', [
        manager_u2c_data['hygiene'], manager_u2c_data['cancelled'],
    ]).status_code == 200
    selected = client.get(API).json['sections'][0]['items']
    assert {item['status'] for item in selected} == {'Hygiene/Duplicate', 'Cancelled'}
    normal = client.get(f'{url}?type=milestone').json['results']
    assert not {manager_u2c_data['hygiene'], manager_u2c_data['cancelled']}.intersection(
        row['id'] for row in normal
    )
