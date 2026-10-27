"""Top ACR milestone focus preserves the 1:1 report's existing population."""

from datetime import date, datetime, time, timedelta, timezone

from bs4 import BeautifulSoup
import pytest

from app.models import Customer, Milestone, Seller, db
from app.services.one_on_one_report import get_quarter_milestone_highlights
from app.services.salesiq_tools import execute_tool, get_openai_tools


@pytest.fixture
def milestone_focus_data(app, monkeypatch):
    """Seed ranked milestones, lower ACR amounts and out-of-scope sentinels."""
    reference = date(2026, 10, 27)

    class ReportDate(date):
        """Pin date-only report comparisons without replacing datetime."""

        @classmethod
        def today(cls) -> date:
            """Return the chosen report day."""
            return reference

    monkeypatch.setattr('app.services.one_on_one_report.date', ReportDate)
    monkeypatch.setattr('app.routes.reports.date', ReportDate)
    with app.app_context():
        sellers = [Seller(name='Focus Seller'), Seller(name='Other Focus Seller')]
        customers = [
            Customer(name='Focus Customer', tpid=912600, seller=sellers[0]),
            Customer(name='Other Focus Customer', tpid=912601, seller=sellers[1]),
        ]
        ranked = [
            Milestone(
                title=f'Ranked {index:02d}', customer=customers[index % 2],
                url=f'https://example.test/focus/{index}', on_my_team=True,
                msx_status=['On Track', 'At Risk', 'Blocked'][index % 3],
                monthly_usage=29_000 - index * 1000,
                due_date=datetime.combine(
                    reference - timedelta(days=200) if index == 0 else
                    reference + timedelta(days=index), time.min,
                ),
            )
            for index in range(20)
        ]
        low_value = [
            Milestone(
                title=f'Lower ACR {index}', customer=customers[0],
                url=f'https://example.test/low/{index}', on_my_team=True,
                msx_status='On Track', monthly_usage=amount,
                due_date=datetime.combine(reference, time.min),
                committed_at=datetime.now(timezone.utc),
            )
            for index, amount in enumerate([9999.99, 0, None, -100])
        ]
        excluded = []
        for index, overrides in enumerate([
            {'customer': None}, {'on_my_team': False}, {'due_date': None},
            {'due_date': datetime(2027, 1, 1)},
            {'msx_status': 'Completed'}, {'msx_status': 'Cancelled'},
        ]):
            fields = {
                'title': f'Excluded {index}', 'customer': customers[0],
                'url': f'https://example.test/excluded/{index}',
                'on_my_team': True, 'msx_status': 'On Track',
                'monthly_usage': 1_000_000,
                'due_date': datetime.combine(reference, time.min),
            }
            excluded.append(Milestone(**(fields | overrides)))
        db.session.add_all([*ranked, *low_value, *excluded])
        db.session.commit()
        return {
            'ranked_ids': [row.id for row in ranked],
            'low_ids': [row.id for row in low_value],
            'seller_id': sellers[0].id,
        }


def report_soup(client, focus=None, **options):
    """Render a focus selection through the normal server-side report route."""
    if focus is not None:
        options['milestone_focus'] = focus
    response = client.get('/reports/one-on-one', query_string=options)
    assert response.status_code == 200
    return BeautifulSoup(response.data, 'html.parser')


def quarter_ids(soup):
    """Read the actual exported quarter table membership in rendered order."""
    return [int(row['data-row-id']) for row in soup.select('#oneOnOneQuarter tbody tr')]


@pytest.mark.parametrize('focus,limit', [('all', 24), ('10', 10), ('15', 15)])
def test_focus_uses_ranked_existing_scope(client, milestone_focus_data, focus, limit):
    """Rank before limiting; overdue rows remain eligible and sentinels stay excluded."""
    soup = report_soup(client, focus)
    expected = milestone_focus_data['ranked_ids'][:limit]
    ids = quarter_ids(soup)
    assert len(ids) == limit
    assert ids[:len(expected)] == expected
    if focus == 'all':
        assert set(milestone_focus_data['low_ids']).issubset(ids)
    else:
        assert not set(milestone_focus_data['low_ids']).intersection(ids)
        assert f'Top {focus} by estimated monthly ACR' in soup.select_one(
            '#oneOnOneMilestoneScope',
        ).text
        assert f'Due This Quarter: Top {focus} ACR' in str(soup)
    assert soup.select_one('#oneOnOneMilestoneFocus option[selected]')['value'] == focus
    assert soup.select_one('#ooQuarterTitle .oo-tag').text == str(limit)
    assert len(soup.select('#oneOnOneCommitments tbody tr')) == 4
    assert 'Excluded' not in soup.select_one('#oneOnOneQuarter').text


def test_default_and_other_controls_preserve_focus_and_embedding(client, milestone_focus_data):
    """All remains the default; changing either form preserves the other selections."""
    default = report_soup(client)
    assert len(quarter_ids(default)) == 24
    assert default.select_one('#oneOnOneMilestoneFocus option[selected]')['value'] == 'all'
    focused = report_soup(client, '15', days='30', activity_sort='acr', embed='1')
    top_form = focused.select_one('#oneOnOneMilestoneFocus').find_parent('form')
    for name, value in [('days', '30'), ('activity_sort', 'acr'), ('embed', '1')]:
        assert top_form.select_one(f'input[name="{name}"]')['value'] == value
    period_form = focused.select_one('#oneOnOnePeriod').find_parent('form')
    assert period_form.select_one('input[name="milestone_focus"]')['value'] == '15'
    assert period_form.select_one('input[name="embed"]')['value'] == '1'
    assert focused.select_one('#oneOnOneMilestoneFocus')['onchange'] == (
        'this.form.requestSubmit()'
    )
    assert top_form.select_one('noscript button[type="submit"]')
    assert focused.select_one('#oneOnOneQuarter').has_attr('data-sb-sortable')


@pytest.mark.parametrize('focus', ['10', '15'])
def test_top_lists_include_lower_acr_and_shorten_only_when_scope_is_smaller(
    app, client, milestone_focus_data, focus,
):
    """Lower, zero and unknown amounts remain eligible when fewer milestones are in scope."""
    with app.app_context():
        for index, row_id in enumerate(milestone_focus_data['ranked_ids']):
            milestone = db.session.get(Milestone, row_id)
            milestone.monthly_usage = 900 + index
            milestone.on_my_team = index < 2
        db.session.commit()
    expected = quarter_ids(report_soup(client, 'all'))
    soup = report_soup(client, focus)
    assert quarter_ids(soup) == expected
    assert len(expected) == 6
    assert set(milestone_focus_data['low_ids']).issubset(expected)


def test_empty_top_list_is_explicit_and_does_not_hide_commitments(
    app, client, milestone_focus_data,
):
    """An empty active scope does not hide recently committed completed milestones."""
    with app.app_context():
        for row_id in milestone_focus_data['ranked_ids'] + milestone_focus_data['low_ids']:
            db.session.get(Milestone, row_id).msx_status = 'Completed'
        db.session.commit()
    soup = report_soup(client, '10')
    assert soup.select_one('#oneOnOneQuarter') is None
    assert 'No milestones due this quarter on your team.' in soup.select_one(
        '[aria-labelledby="ooQuarterTitle"] .oo-empty',
    ).text
    assert len(soup.select('#oneOnOneCommitments tbody tr')) == 4


def test_salesiq_matches_web_ranking_and_limits_after_seller_scope(
    app, client, milestone_focus_data,
):
    """The report tool reuses the query and applies seller scope before its top limit."""
    web_ids = quarter_ids(report_soup(client, '15'))
    with app.test_request_context():
        result = execute_tool('report_one_on_one', {'milestone_focus': '15'})
        scoped = execute_tool('report_one_on_one', {
            'milestone_focus': '15', 'seller_id': milestone_focus_data['seller_id'],
        })
    assert [row['id'] for row in result['quarter_milestones']] == web_ids
    assert [row['id'] for row in scoped['quarter_milestones']] == (
        milestone_focus_data['ranked_ids'][::2]
        + milestone_focus_data['low_ids'][:1]
        + sorted(milestone_focus_data['low_ids'][1:3])
        + milestone_focus_data['low_ids'][3:]
    )
    definition = next(
        tool for tool in get_openai_tools()
        if tool['function']['name'] == 'report_one_on_one'
    )
    assert definition['function']['parameters']['properties']['milestone_focus']['enum'] == [
        'all', '10', '15',
    ]


@pytest.mark.parametrize('focus', ['', '20%', '20', '0', '-1', 'oops'])
def test_invalid_focus_is_rejected(client, focus):
    """Never silently substitute all milestones for an invalid explicit selection."""
    response = client.get('/reports/one-on-one', query_string={'milestone_focus': focus})
    assert response.status_code == 400


def test_tied_acr_has_stable_id_order_and_does_not_expand_top_limit(app, milestone_focus_data):
    """Ties stay deterministic and never grow a Top 10 into a larger list."""
    with app.app_context():
        for row_id in milestone_focus_data['ranked_ids']:
            db.session.get(Milestone, row_id).monthly_usage = 20_000
        db.session.commit()
        milestones, _, _ = get_quarter_milestone_highlights('10')
        assert [row.id for row in milestones] == sorted(milestone_focus_data['ranked_ids'])[:10]


@pytest.mark.parametrize('month,start,end', [
    (1, '2026-01-01', '2026-03-31'), (3, '2026-01-01', '2026-03-31'),
    (4, '2026-04-01', '2026-06-30'), (6, '2026-04-01', '2026-06-30'),
    (7, '2026-07-01', '2026-09-30'), (9, '2026-07-01', '2026-09-30'),
    (10, '2026-10-01', '2026-12-31'), (12, '2026-10-01', '2026-12-31'),
])
def test_original_quarter_boundaries_are_unchanged(app, month, start, end):
    """Fiscal-year and calendar-year transitions retain the original date scope."""
    with app.app_context():
        _, quarter_start, quarter_end = get_quarter_milestone_highlights(
            reference=date(2026, month, 15),
        )
    assert quarter_start == datetime.combine(date.fromisoformat(start), time.min)
    assert quarter_end == datetime.combine(date.fromisoformat(end), time(23, 59, 59))
