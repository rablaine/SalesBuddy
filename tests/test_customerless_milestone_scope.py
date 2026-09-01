"""Regression tests for customerless CAIP milestone query scoping."""

import json
from datetime import datetime, timezone

from app.models import Milestone, db
from app.services.salesiq_tools import execute_tool


def _create_customerless_milestone(commitment: str = 'Uncommitted') -> Milestone:
    """Create a customerless milestone matching current-book query filters."""
    now = datetime.now(timezone.utc)
    milestone = Milestone(
        msx_milestone_id=f'customerless-{commitment.lower()}',
        url=f'https://example.test/customerless-{commitment.lower()}',
        title=f'Customerless {commitment} Scope Sentinel',
        milestone_category='Production',
        customer_commitment=commitment,
        msx_status='On Track',
        on_my_team=True,
        due_date=now,
        committed_at=now,
        msx_created_on=now,
        msx_modified_on=now,
    )
    db.session.add(milestone)
    db.session.commit()
    return milestone


def test_current_book_web_surfaces_exclude_customerless_milestones(client, app):
    """Current-book pages and APIs must not render customerless milestones."""
    with app.app_context():
        milestone = _create_customerless_milestone()
        sentinel = milestone.title.encode()
        due_date = milestone.due_date

        urls = [
            '/',
            '/action-items',
            '/milestones',
            '/reports/milestone-tracker',
            '/reports/one-on-one',
            '/reports/hygiene',
            '/reports/whats-new',
            f'/api/milestones/calendar?year={due_date.year}&month={due_date.month}',
        ]
        try:
            for url in urls:
                response = client.get(url)
                assert response.status_code == 200, url
                assert sentinel not in response.data, url
        finally:
            db.session.delete(milestone)
            db.session.commit()


def test_tracker_and_stale_queries_exclude_customerless_milestones(app):
    """Tracker and stale milestone data stay scoped to attached customers."""
    from app.routes.main import _find_stale_milestones
    from app.services.milestone_sync import get_milestone_tracker_data

    with app.app_context():
        milestone = _create_customerless_milestone()
        try:
            tracker = get_milestone_tracker_data()
            stale = _find_stale_milestones()

            assert milestone.id not in {row['id'] for row in tracker['milestones']}
            assert milestone.id not in {row.id for row in stale}
        finally:
            db.session.delete(milestone)
            db.session.commit()


def test_salesiq_current_book_tools_exclude_customerless_milestones(app):
    """SalesIQ current-book tools exclude customerless rows and counts."""
    with app.app_context():
        baseline_count = execute_tool('get_portfolio_overview', {})['open_milestones']
        milestone = _create_customerless_milestone(commitment='Committed')
        sentinel = milestone.title
        try:
            results = [
                execute_tool('get_milestone_status', {'on_my_team': True}),
                execute_tool('get_milestones_due_soon', {'on_my_team': True}),
                execute_tool('report_hygiene', {}),
                execute_tool('report_whats_new', {'days': 7}),
                execute_tool('report_one_on_one', {'days': 14}),
            ]

            assert all(sentinel not in json.dumps(result) for result in results)
            assert (
                execute_tool('get_portfolio_overview', {})['open_milestones']
                == baseline_count
            )
        finally:
            db.session.delete(milestone)
            db.session.commit()