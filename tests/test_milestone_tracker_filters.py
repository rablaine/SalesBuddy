"""Coverage for tracker search, customer filters, and their shared consumers."""

import json
import shutil
import subprocess
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models import Milestone, db
from app.services.milestone_search import get_milestone_search_text
from app.services.milestone_sync import (
    get_milestone_tracker_data,
    get_milestone_tracker_data_for_seller,
)
from app.services.salesiq_tools import execute_tool


@pytest.fixture
def searchable_milestones(app, sample_data):
    """Create two customer-owned milestones with hidden and visible search hits."""
    with app.app_context():
        first = Milestone(
            title='Architecture validation',
            owner_name='POC Architect',
            milestone_category='Proof of concept',
            milestone_number='7-SEARCH-ONE',
            cached_comments_json=json.dumps([
                {'comment': 'Pilot "quoted" <script>alert(1)</script>'},
            ]),
            url='https://example.test/search-first',
            customer_id=sample_data['customer1_id'],
            msx_status='On Track',
            customer_commitment='Uncommitted',
            workload='Data: Fabric',
            on_my_team=True,
            monthly_usage=1234,
            due_date=datetime(2026, 10, 15, 12),
        )
        second = Milestone(
            title='POC for another customer',
            url='https://example.test/search-second',
            customer_id=sample_data['customer2_id'],
            msx_status='Blocked',
            due_date=datetime(2026, 10, 16, 12),
            on_my_team=True,
        )
        db.session.add_all([first, second])
        db.session.commit()
        ids = [first.id, second.id]
        yield ids
        for milestone_id in ids:
            db.session.delete(db.session.get(Milestone, milestone_id))
        db.session.commit()


@pytest.mark.parametrize('column', list(Milestone.__table__.columns), ids=lambda c: c.name)
def test_search_contains_every_stored_milestone_field(column):
    """Future model columns must not silently disappear from milestone search."""
    milestone = Milestone(url='https://example.test/fields')
    value = {
        int: 753921,
        float: 753921.25,
        bool: True,
        datetime: datetime(2026, 10, 15, 12, 34, 56),
    }.get(column.type.python_type, 'Field search sentinel')
    setattr(milestone, column.name, value)
    assert str(value).lower() in get_milestone_search_text(milestone)


def test_search_includes_related_names_and_nulls(searchable_milestones):
    """Search includes related display names without requiring every field."""
    milestone = db.session.get(Milestone, searchable_milestones[0])
    text = get_milestone_search_text(milestone)
    assert 'acme corp' in text
    assert 'alice smith' in text
    assert 'west region' in text
    assert 'oct 15, 2026' in text
    assert 'none' not in get_milestone_search_text(Milestone())


def test_full_and_locked_tracker_share_search_text(searchable_milestones, sample_data):
    """Both the standalone tracker and seller modal include hidden-field text."""
    for result in (
        get_milestone_tracker_data(),
        get_milestone_tracker_data_for_seller(sample_data['seller1_id']),
    ):
        row = next(
            item for item in result['milestones'] if item['id'] == searchable_milestones[0]
        )
        assert 'poc architect' in row['search_text']
        assert 'proof of concept' in row['search_text']
        assert 'pilot' in row['search_text']


class TrackerMarkup(HTMLParser):
    """Collect relevant tracker attributes without relying on string escaping."""

    def __init__(self) -> None:
        """Initialize the milestone rows and control collection."""
        super().__init__()
        self.rows: list[dict[str, str | None]] = []
        self.controls: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record searchable rows and filter controls."""
        attributes = dict(attrs)
        if tag == 'tr' and attributes.get('class') == 'milestone-row':
            self.rows.append(attributes)
        if tag in ('input', 'select') and attributes.get('data-role'):
            self.controls.append(attributes)


def test_tracker_renders_search_customers_and_escaped_text(client, searchable_milestones):
    """Search indexes hidden fields safely and renders exactly one of each control."""
    response = client.get('/reports/milestone-tracker')
    assert response.status_code == 200
    parser = TrackerMarkup()
    parser.feed(response.get_data(as_text=True))
    roles = [control['data-role'] for control in parser.controls]
    assert roles.count('searchFilter') == 1
    assert roles.count('customerFilter') == 1
    assert roles.count('customerSearch') == 1
    assert roles.count('sellerFilter') == 1
    assert any('poc architect' in row['data-search'] for row in parser.rows)
    assert any('<script>alert(1)</script>' in row['data-search'] for row in parser.rows)
    assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in response.data
    assert b'data-role="primaryFilters"' in response.data
    assert b'data-role="secondaryFilters"' in response.data
    assert b'data-role="customerDropdownBtn"' in response.data
    assert b'No matching customers' in response.data


def test_seller_locked_tracker_limits_customer_choices(
    client, searchable_milestones, sample_data,
):
    """Seller mode renders search and only customers in its milestone dataset."""
    with patch(
        'app.routes.milestones.get_seller_mode_seller_id',
        return_value=sample_data['seller1_id'],
    ):
        response = client.get('/reports/milestone-tracker')
    assert response.status_code == 200
    parser = TrackerMarkup()
    parser.feed(response.get_data(as_text=True))
    roles = [control['data-role'] for control in parser.controls]
    assert 'sellerFilter' not in roles
    assert 'customerFilter' in roles
    assert 'searchFilter' in roles
    assert {row['data-customer-id'] for row in parser.rows} == {
        str(sample_data['customer1_id']),
    }
    html = response.get_data(as_text=True)
    customer_options = html.split('data-role="customerFilter"', 1)[1].split('</select>', 1)[0]
    assert f'value="{sample_data["customer1_id"]}"' in customer_options
    assert f'value="{sample_data["customer2_id"]}"' not in customer_options


@pytest.mark.parametrize('search', ['PoC', '  poc  ', 'proof of concept', 'pilot'])
def test_calendar_searches_hidden_fields(client, searchable_milestones, sample_data, search):
    """Calendar combines case-insensitive hidden-field search with customer selection."""
    response = client.get('/api/milestones/calendar', query_string={
        'year': 2026, 'month': 10, 'search': search,
        'customer_id': sample_data['customer1_id'],
    })
    assert response.status_code == 200
    entries = [item for items in response.json['days'].values() for item in items]
    assert [item['id'] for item in entries] == [searchable_milestones[0]]


def test_calendar_filters_combine_and_no_results(client, searchable_milestones, sample_data):
    """Customer, seller, status, and search restrictions intersect rather than override."""
    response = client.get('/api/milestones/calendar', query_string={
        'year': 2026, 'month': 10, 'search': 'POC',
        'customer_id': sample_data['customer1_id'],
        'seller_id': sample_data['seller2_id'],
    })
    assert response.json['days'] == {}
    response = client.get('/api/milestones/calendar', query_string={
        'year': 2026, 'month': 10, 'search': 'POC',
        'customer_id': sample_data['customer1_id'], 'status': 'Blocked',
    })
    assert response.json['days'] == {}


def test_calendar_customer_does_not_bypass_seller_mode(
    client, searchable_milestones, sample_data,
):
    """Selecting a customer outside seller mode cannot broaden the seller scope."""
    with patch(
        'app.routes.milestones.get_seller_mode_seller_id',
        return_value=sample_data['seller1_id'],
    ):
        response = client.get('/api/milestones/calendar', query_string={
            'year': 2026, 'month': 10, 'customer_id': sample_data['customer2_id'],
        })
    assert response.json['days'] == {}


def test_calendar_rejects_invalid_customer_id(client):
    """Invalid customer filters return explicit JSON rather than an HTML exception."""
    response = client.get('/api/milestones/calendar?customer_id=invalid')
    assert response.status_code == 400
    assert response.json == {'success': False, 'error': 'Invalid customer ID'}


@pytest.mark.parametrize('search', ['  POC  ', 'proof of concept', 'pilot', '7-SEARCH-ONE'])
def test_salesiq_uses_same_full_field_search(searchable_milestones, sample_data, search):
    """SalesIQ can find the same hidden-field hits with an existing customer filter."""
    result = execute_tool('get_msx_workspace_milestones', {
        'customer_id': sample_data['customer1_id'], 'search': search,
    })
    assert [item['id'] for item in result['milestones']] == [searchable_milestones[0]]


def test_salesiq_search_matches_after_first_page(searchable_milestones, sample_data):
    """Search scans all scoped candidates before applying the tool's result limit."""
    earlier = [
        Milestone(
            title=f'Earlier milestone {index}',
            url=f'https://example.test/search-pagination-{index}',
            customer_id=sample_data['customer1_id'],
            due_date=datetime(2026, 10, 1),
        )
        for index in range(201)
    ]
    db.session.add_all(earlier)
    db.session.commit()
    try:
        result = execute_tool('get_msx_workspace_milestones', {
            'customer_id': sample_data['customer1_id'], 'search': 'proof of concept',
        })
        assert [item['id'] for item in result['milestones']] == [searchable_milestones[0]]
    finally:
        for milestone in earlier:
            db.session.delete(milestone)
        db.session.commit()


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required for JS tests')
def test_tracker_filter_javascript():
    """Execute actual tracker scripts to test filtering, persistence, and calendar wiring."""
    test_file = Path(__file__).parent / 'js' / 'milestone_tracker_filters.test.cjs'
    result = subprocess.run(
        ['node', '--test', str(test_file)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
