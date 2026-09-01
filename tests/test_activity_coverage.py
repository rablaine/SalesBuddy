"""Tests for meeting-to-MSX activity coverage."""
import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from app.models import (
    ActivityCoveragePopulation,
    CaipActivity,
    Customer,
    DailyMeetingCache,
    Job,
    Milestone,
    MilestoneCoverageDraft,
    MsxTask,
    Note,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
    SyncStatus,
    db,
)
from app.services import activity_coverage
from app.services import activity_enrichment


@pytest.fixture
def coverage_data(app):
    """Create one matched meeting and milestone for coverage tests."""
    with app.app_context():
        customer = Customer(name='Coverage Customer', tpid=987654321)
        db.session.add(customer)
        db.session.flush()
        milestone = Milestone(
            msx_milestone_id='coverage-milestone-guid',
            url='https://example.test/milestone',
            title='Deploy Fabric',
            msx_status='On Track',
            on_my_team=True,
            customer_id=customer.id,
        )
        db.session.add(milestone)
        db.session.flush()
        meeting = PrefetchedMeeting(
            workiq_id='coverage-meeting',
            subject='Fabric architecture workshop',
            start_time=datetime.combine(date.today(), datetime.min.time())
            + timedelta(hours=14, minutes=30),
            end_time=datetime.combine(date.today(), datetime.min.time())
            + timedelta(hours=14, minutes=30)
            + timedelta(minutes=45),
            meeting_date=date.today(),
            customer_id=customer.id,
            expires_at=datetime.now() + timedelta(days=5),
        )
        meeting.attendees.append(PrefetchedMeetingAttendee(
            name='Customer Person',
            email='person@coverage.test',
            domain='coverage.test',
            is_external=True,
        ))
        db.session.add(meeting)
        db.session.commit()
        ids = {
            'customer_id': customer.id,
            'milestone_id': milestone.id,
            'meeting_id': meeting.id,
        }
        yield ids
        MilestoneCoverageDraft.query.filter_by(milestone_id=milestone.id).delete()
        MsxTask.query.filter_by(milestone_id=milestone.id).delete()
        PrefetchedMeetingAttendee.query.filter_by(meeting_id=meeting.id).delete()
        db.session.delete(db.session.get(PrefetchedMeeting, meeting.id))
        db.session.delete(db.session.get(Milestone, milestone.id))
        db.session.delete(db.session.get(Customer, customer.id))
        db.session.commit()


def test_milestone_coverage_surfaces_prior_hok_and_meeting_draft(app, coverage_data):
    """Prior-year HoK remains uncovered while prepared meeting overlap is shown."""
    with app.app_context():
        fiscal_start, _ = activity_coverage.fiscal_year_bounds()
        prior_task = MsxTask(
            msx_task_id='prior-year-hok',
            subject='Prior architecture session',
            task_category=861980004,
            task_category_name='Architecture Design Session',
            duration_minutes=60,
            is_hok=True,
            due_date=datetime.combine(fiscal_start - timedelta(days=1), datetime.min.time()),
            milestone_id=coverage_data['milestone_id'],
        )
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.milestone_id = coverage_data['milestone_id']
        meeting.enrichment_status = 'complete'
        db.session.add(prior_task)
        db.session.commit()

        report = activity_coverage.get_milestone_coverage_data()
        row = next(
            item for item in report['milestone_rows']
            if item['id'] == coverage_data['milestone_id']
        )
        assert row['covered'] is False
        assert row['prior_task'].id == prior_task.id
        assert row['meeting_draft_count'] == 1
        assert row['prepared_meeting_count'] == 1
        prepared = row['prepared_meetings'][0]
        assert prepared['id'] == meeting.id
        assert prepared['customer_id'] == coverage_data['customer_id']
        assert prepared['milestone_id'] == coverage_data['milestone_id']
        assert prepared['meeting_subject'] == 'Fabric architecture workshop'
        assert prepared['activity_subject'] == 'Fabric architecture workshop'
        assert prepared['task_category_name'] == 'Architecture Design Session'
        assert prepared['is_hok'] is True
        assert prepared['duration_minutes'] == 45


def test_current_fy_hok_controls_covered_filter(app, coverage_data):
    """Current-FY HoK hides milestone by default and appears with covered filter."""
    with app.app_context():
        task = MsxTask(
            msx_task_id='current-year-hok',
            subject='Current workshop',
            task_category=861980001,
            task_category_name='Workshop',
            duration_minutes=60,
            is_hok=True,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        default_ids = {
            row['id'] for row in activity_coverage.get_milestone_coverage_data()[
                'milestone_rows'
            ]
        }
        assert coverage_data['milestone_id'] not in default_ids
        report = activity_coverage.get_milestone_coverage_data(include_covered=True)
        row = next(
            item for item in report['milestone_rows']
            if item['id'] == coverage_data['milestone_id']
        )
        assert row['covered'] is True
        assert row['current_task'].id == task.id


def test_fy_hok_excludes_customerless_team_milestones(app):
    """FY HoK stays scoped to milestones attached to the current book."""
    with app.app_context():
        milestone = Milestone(
            msx_milestone_id='fy-customerless',
            url='https://example.test/fy-customerless',
            title='Outside current book',
            milestone_category='Production',
            msx_status='On Track',
            on_my_team=True,
        )
        db.session.add(milestone)
        db.session.commit()

        fy_report = activity_coverage.get_milestone_coverage_data(
            include_covered=True,
            include_inactive=True,
        )
        caip_report = activity_coverage.get_caip_coverage_data()

        assert milestone.id not in {
            row['id'] for row in fy_report['milestone_rows']
        }
        assert milestone.id in {
            row['id']
            for group in caip_report['caip_groups']
            for row in group['rows']
        }


def test_caip_coverage_uses_category_denominator_and_strict_hok(app):
    """CAIP includes all team CAIP milestones and applies strict HoK evidence."""
    with app.app_context():
        included = Milestone(
            msx_milestone_id='caip-included',
            url='https://example.test/caip-included',
            title='Customerless production milestone',
            milestone_category='Production',
            msx_status='Completed',
            on_my_team=True,
            due_date=datetime(2025, 8, 1),
        )
        excluded = Milestone(
            msx_milestone_id='caip-excluded',
            url='https://example.test/caip-excluded',
            title='Non-CAIP milestone',
            milestone_category='Other',
            on_my_team=True,
        )
        db.session.add_all([included, excluded])
        db.session.flush()
        db.session.add(CaipActivity(
            msx_activity_id='caip-general-activity',
            activity_type='appointment',
            subject='Old activity still counts',
            created_on=datetime(2020, 1, 1),
            milestone_id=included.id,
        ))
        db.session.add(MsxTask(
            msx_task_id='caip-hok',
            subject='Completed workshop',
            task_category=861980001,
            task_category_name='Workshop',
            duration_minutes=60,
            is_hok=True,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            actual_end=datetime.combine(date.today(), datetime.min.time()),
            statecode=1,
            statuscode=5,
            milestone_id=included.id,
        ))
        db.session.commit()

        report = activity_coverage.get_caip_coverage_data()

        assert report['caip_summary'] == {
            'total': 1,
            'activities_logged': 1,
            'activities_percent': 100,
            'hok_covered': 1,
            'hok_percent': 100,
        }
        row = report['caip_groups'][0]['rows'][0]
        assert row['milestone'].customer is None
        assert row['activity_logged'] is True
        assert row['hok_covered'] is True


def test_caip_lens_renders_separate_methodology(app, client):
    """CAIP subview renders dedicated metrics without replacing FY controls."""
    with app.app_context():
        db.session.add(Milestone(
            msx_milestone_id='caip-render',
            url='https://example.test/caip-render',
            title='Render CAIP milestone',
            milestone_category='POC/Pilot',
            msx_status='Cancelled',
            on_my_team=True,
        ))
        db.session.commit()

    response = client.get(
        '/reports/activity-coverage?lens=milestones&coverage=caip'
    )
    html = response.data.decode('utf-8')
    assert response.status_code == 200
    assert 'FY HoK Coverage' in html
    assert 'CAIP Coverage' in html
    assert 'Render CAIP milestone' in html
    assert 'activities logged' in html
    assert 'HoK coverage' in html
    assert 'Show covered' not in html
    soup = BeautifulSoup(response.data, 'html.parser')
    fiscal_year_toggle = soup.select_one('[data-caip-group-toggle]')
    assert fiscal_year_toggle is not None
    assert fiscal_year_toggle['aria-expanded'] == 'true'
    assert fiscal_year_toggle['aria-controls'].startswith('caip-group-')
    assert 'salesbuddy_caip_collapsed_fiscal_years' in html


def test_caip_coverage_sorts_most_recent_targets_first(app):
    """CAIP groups and milestones sort newest first with undated rows last."""
    with app.app_context():
        milestones = [
            Milestone(
                msx_milestone_id='caip-oldest',
                url='https://example.test/caip-oldest',
                title='Oldest',
                milestone_category='Production',
                on_my_team=True,
                due_date=datetime(2024, 8, 1),
            ),
            Milestone(
                msx_milestone_id='caip-newest',
                url='https://example.test/caip-newest',
                title='Newest',
                milestone_category='Production',
                on_my_team=True,
                due_date=datetime(2026, 8, 1),
            ),
            Milestone(
                msx_milestone_id='caip-same-fy-older',
                url='https://example.test/caip-same-fy-older',
                title='Same FY Older',
                milestone_category='Production',
                on_my_team=True,
                due_date=datetime(2026, 7, 1),
            ),
            Milestone(
                msx_milestone_id='caip-undated',
                url='https://example.test/caip-undated',
                title='Undated',
                milestone_category='Production',
                on_my_team=True,
            ),
        ]
        db.session.add_all(milestones)
        db.session.commit()

        report = activity_coverage.get_caip_coverage_data()

        assert [group['label'] for group in report['caip_groups']] == [
            'FY27',
            'FY25',
            'No target FY',
        ]
        assert [
            row['milestone'].title for row in report['caip_groups'][0]['rows']
        ] == ['Newest', 'Same FY Older']


def test_create_standalone_milestone_hok_is_idempotent(app, coverage_data):
    """Saved standalone draft creates one unlinked HoK task and retries return it."""
    with app.app_context():
        scheduled_start = datetime.combine(
            date.today(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        draft = activity_coverage.update_milestone_coverage_draft(
            coverage_data['milestone_id'],
            {
                'subject': 'Fabric solution whiteboarding',
                'description': 'Built target architecture with customer engineering.',
                'task_category': 606820008,
                'duration_minutes': 90,
                'scheduled_start': scheduled_start.isoformat(),
            },
        )
        assert draft.id is not None
        result = {
            'success': True,
            'task_id': 'standalone-hok-guid',
            'task_url': 'https://example.test/standalone-task',
        }
        with (
            patch('app.services.msx_api.create_task', return_value=result) as create,
            patch(
                'app.services.msx_api.close_task',
                return_value={'success': True},
            ) as close,
        ):
            first = activity_coverage.create_milestone_hok_activity(
                coverage_data['milestone_id'],
            )
            second = activity_coverage.create_milestone_hok_activity(
                coverage_data['milestone_id'],
            )

        assert first.id == second.id
        assert first.is_hok is True
        assert first.statecode == 1
        assert first.statuscode == 5
        assert first.meeting_id is None
        assert first.note_id is None
        assert MilestoneCoverageDraft.query.filter_by(
            milestone_id=coverage_data['milestone_id'],
        ).first() is None
        create.assert_called_once()
        assert close.call_count == 2
        close.assert_called_with('standalone-hok-guid')


def test_milestone_draft_rejects_non_hok_category(app, coverage_data):
    """Standalone milestone coverage accepts HoK task categories only."""
    with app.app_context(), pytest.raises(
        ValueError,
        match='Hands-on-Keyboard',
    ):
        activity_coverage.update_milestone_coverage_draft(
            coverage_data['milestone_id'],
            {
                'subject': 'Customer follow-up',
                'description': 'Followed up with customer.',
                'task_category': 861980000,
                'duration_minutes': 30,
                'scheduled_start': datetime.combine(
                    date.today(),
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ).isoformat(),
            },
        )


def test_milestone_coverage_lens_renders_filters_and_hok_form(
    app,
    client,
    coverage_data,
):
    """Milestone lens defaults to active uncovered rows with HoK controls."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.milestone_id = coverage_data['milestone_id']
        meeting.enrichment_status = 'complete'
        db.session.commit()
        response = client.get('/reports/activity-coverage?lens=milestones')

    assert response.status_code == 200
    html = response.data.decode('utf-8')
    assert 'Show covered' in html
    assert 'Show inactive' in html
    assert 'No HoK activity' in html
    assert 'Create HoK Task' in html
    assert '1 prepared meeting' in html
    assert '1 prepared / 1 linked' not in html
    assert 'Prepared meeting activities' in html
    assert 'Meeting' in html
    assert 'Activity subject' in html
    assert 'Activity type' in html
    assert 'Description' in html
    assert 'name="duration_minutes"' in html
    assert 'save-prepared-meeting-draft' in html
    assert 'Create Activity' in html
    assert 'Or create a standalone HoK activity' in html
    assert 'savePreparedMeeting' in html
    assert "'/api/reports/activity-coverage/meetings/'" in html
    assert 'Architecture Design Session' in html
    soup = BeautifulSoup(response.data, 'html.parser')
    standalone_category = soup.select_one('[id^="milestone-hok-category-"]')
    standalone_options = [option.get_text(strip=True) for option in standalone_category.select('option')]
    assert 'Customer Engagement' not in standalone_options


def test_milestone_coverage_draft_api_rejects_non_hok(
    app,
    client,
    coverage_data,
):
    """Milestone draft endpoint returns a user-facing validation error."""
    payload = {
        'subject': 'Customer follow-up',
        'description': 'Followed up with customer.',
        'task_category': 861980000,
        'duration_minutes': 30,
        'scheduled_start': datetime.combine(
            date.today(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).isoformat(),
    }
    response = client.patch(
        f"/api/reports/activity-coverage/milestones/{coverage_data['milestone_id']}/draft",
        json=payload,
    )

    assert response.status_code == 400
    assert 'Hands-on-Keyboard' in response.get_json()['error']


def test_milestone_coverage_create_api_creates_unlinked_hok(
    app,
    client,
    coverage_data,
):
    """Create endpoint turns saved draft into an unlinked current-FY HoK task."""
    with app.app_context():
        activity_coverage.update_milestone_coverage_draft(
            coverage_data['milestone_id'],
            {
                'subject': 'Technical workshop delivery',
                'description': 'Led customer engineers through implementation.',
                'task_category': 606820007,
                'duration_minutes': 120,
                'scheduled_start': datetime.combine(
                    date.today(),
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ).isoformat(),
            },
        )

    result = {
        'success': True,
        'task_id': 'route-standalone-hok-guid',
        'task_url': 'https://example.test/route-hok',
    }
    with (
        patch('app.services.msx_api.create_task', return_value=result),
        patch(
            'app.services.msx_api.close_task',
            return_value={'success': True},
        ) as close,
    ):
        response = client.post(
            f"/api/reports/activity-coverage/milestones/{coverage_data['milestone_id']}/create"
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['subject'] == 'Technical workshop delivery'
    assert payload['category'] == 'Technical Workshop'
    assert payload['summary']['covered'] == 1
    assert payload['summary']['uncovered'] == 0
    close.assert_called_once_with('route-standalone-hok-guid')
    with app.app_context():
        task = MsxTask.query.filter_by(msx_task_id='route-standalone-hok-guid').one()
        assert task.is_hok is True
        assert task.meeting_id is None


def test_dismiss_series_api_returns_affected_ids_and_summary(
    app, client, coverage_data,
):
    """Dismiss endpoint identifies every series row removed from the report."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.is_recurring = True
        meeting.recurring_key = 'coverage-series'
        sibling = PrefetchedMeeting(
            workiq_id='coverage-series-sibling',
            subject='Fabric architecture workshop follow-up',
            start_time=meeting.start_time - timedelta(days=1),
            meeting_date=meeting.meeting_date - timedelta(days=1),
            customer_id=coverage_data['customer_id'],
            is_recurring=True,
            recurring_key='coverage-series',
            expires_at=datetime.now() + timedelta(days=5),
        )
        db.session.add(sibling)
        db.session.commit()
        sibling_id = sibling.id

    response = client.post(
        f'/api/reports/activity-coverage/meetings/{coverage_data["meeting_id"]}/dismiss',
        json={'series': True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload['dismissed_ids']) == {
        coverage_data['meeting_id'], sibling_id,
    }
    assert payload['summary']['total'] == 0

    with app.app_context():
        db.session.delete(db.session.get(PrefetchedMeeting, sibling_id))
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.dismissed = False
        meeting.is_recurring = False
        meeting.recurring_key = None
        db.session.commit()


def test_meeting_lens_can_filter_by_milestone(app, client, coverage_data):
    """Milestone meeting handoff renders only meetings targeting that milestone."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.milestone_id = coverage_data['milestone_id']
        db.session.commit()

    response = client.get(
        '/reports/activity-coverage?view=all'
        f"&milestone={coverage_data['milestone_id']}"
    )

    assert response.status_code == 200
    html = response.data.decode('utf-8')
    assert 'Fabric architecture workshop' in html
    assert '<i class="bi bi-funnel"></i> Deploy Fabric' in html


def test_report_status_and_defaults(app, coverage_data):
    """Matched meeting starts ready only after milestone selection."""
    with app.app_context():
        report = activity_coverage.get_report_data()
        row = next(item for item in report['meetings']
                   if item['id'] == coverage_data['meeting_id'])
        assert row['status'] == 'needs_milestone'
        assert row['draft_task_category'] == 861980004
        assert row['draft_duration_minutes'] == 45
        assert 'Customer Person' in row['draft_description']

        activity_coverage.update_meeting_draft(row['id'], {
            'customer_id': coverage_data['customer_id'],
            'milestone_id': coverage_data['milestone_id'],
            'subject': 'Architecture session with Coverage Customer',
            'description': 'Reviewed target Fabric architecture.',
            'task_category': 861980004,
            'duration_minutes': 45,
        })
        updated = activity_coverage.get_report_data()
        updated_row = next(item for item in updated['meetings']
                           if item['id'] == row['id'])
        assert updated_row['status'] == 'ready'


def test_create_activity_is_idempotent(app, coverage_data):
    """One meeting can create at most one MSX activity."""
    with app.app_context():
        activity_coverage.update_meeting_draft(coverage_data['meeting_id'], {
            'customer_id': coverage_data['customer_id'],
            'milestone_id': coverage_data['milestone_id'],
            'subject': 'Coverage activity',
            'description': 'Customer call details',
            'task_category': 861980000,
            'duration_minutes': 30,
        })
        result = {
            'success': True,
            'task_id': 'coverage-task-guid',
            'task_url': 'https://example.test/task',
        }
        with (
            patch('app.services.msx_api.create_task', return_value=result) as create,
            patch(
                'app.services.msx_api.close_task',
                return_value={'success': True},
            ) as close,
        ):
            first = activity_coverage.create_meeting_activity(
                coverage_data['meeting_id'],
            )
            second = activity_coverage.create_meeting_activity(
                coverage_data['meeting_id'],
            )

        assert first.id == second.id
        assert first.statecode == 1
        assert first.statuscode == 5
        assert close.call_count == 2
        close.assert_called_with('coverage-task-guid')
        create.assert_called_once_with(
            milestone_id='coverage-milestone-guid',
            subject='Coverage activity',
            task_category=861980000,
            duration_minutes=30,
            description='Customer call details',
            start_date=f'{date.today().isoformat()}T14:30:00+00:00',
            due_date=f'{date.today().isoformat()}T15:00:00+00:00',
        )
        assert first.due_date == datetime.combine(
            date.today(), datetime.min.time(),
        ) + timedelta(hours=15)


def test_create_activity_endpoint_returns_inline_render_fields(
    client, coverage_data,
):
    """Create endpoint returns complete activity data for row-local rendering."""
    task = SimpleNamespace(
        id=321,
        msx_task_url='https://example.test/inline-created-task',
        subject='Created inline activity',
        task_category_name='Architecture Design Session',
        milestone=SimpleNamespace(display_text='Deploy Fabric'),
    )
    with patch(
        'app.services.activity_coverage.create_meeting_activity',
        return_value=task,
    ):
        response = client.post(
            f'/api/reports/activity-coverage/meetings/'
            f'{coverage_data["meeting_id"]}/create',
        )

    payload = response.get_json()
    assert response.status_code == 200, payload
    assert payload['success'] is True
    assert payload['task_id'] == 321
    assert payload['task_url'] == 'https://example.test/inline-created-task'
    assert payload['subject'] == 'Created inline activity'
    assert payload['category'] == 'Architecture Design Session'
    assert payload['milestone'] == 'Deploy Fabric'


def test_link_existing_activity(app, client, coverage_data):
    """Imported MSX task can be confirmed as meeting coverage."""
    with app.app_context():
        created_at = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)
        msx_created_on = datetime(2026, 6, 30, 18, 15, tzinfo=timezone.utc)
        task = MsxTask(
            msx_task_id='existing-coverage-task',
            subject='Existing customer activity',
            task_category=861980000,
            task_category_name='Customer Engagement',
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            milestone_id=coverage_data['milestone_id'],
            created_at=created_at,
            msx_created_on=msx_created_on,
        )
        db.session.add(task)
        db.session.commit()

        report = activity_coverage.get_report_data()
        row = next(
            item for item in report['meetings']
            if item['id'] == coverage_data['meeting_id']
        )
        candidate = next(
            item for item in row['candidate_tasks'] if item['id'] == task.id
        )
        assert candidate['activity_date'] == date.today().isoformat()
        assert candidate['created_on'] == '2026-06-30'

        response = client.get('/reports/activity-coverage')
        html = response.get_data(as_text=True)
        assert 'candidate-picker-input' in html
        assert f'data-activity-date="{date.today().isoformat()}"' in html
        assert f'data-milestone="{candidate["milestone"]}"' in html
        assert 'data-created-on="2026-06-30"' in html
        assert 'Created in MSX:' in html
        assert 'Same customer and activity date as this meeting.' in html

        linked = activity_coverage.link_existing_activity(
            coverage_data['meeting_id'], task.id,
        )
        assert linked.meeting_id == coverage_data['meeting_id']
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        assert meeting.milestone_id == coverage_data['milestone_id']
        db.session.delete(task)
        db.session.commit()


def test_link_activity_endpoint_returns_inline_render_fields(
    app, client, coverage_data,
):
    """Link endpoint returns complete activity data for row-local rendering."""
    with app.app_context():
        task = MsxTask(
            msx_task_id='inline-linked-task',
            msx_task_url='https://example.test/inline-linked-task',
            subject='Linked inline activity',
            task_category=861980004,
            task_category_name='Architecture Design Session',
            duration_minutes=60,
            is_hok=True,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()
        task_id = task.id

    response = client.post(
        f'/api/reports/activity-coverage/meetings/{coverage_data["meeting_id"]}/link',
        json={'task_id': task_id},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        'success': True,
        'task_id': task_id,
        'task_url': 'https://example.test/inline-linked-task',
        'subject': 'Linked inline activity',
        'category': 'Architecture Design Session',
        'milestone': 'Deploy Fabric',
    }

    with app.app_context():
        db.session.delete(db.session.get(MsxTask, task_id))
        db.session.commit()


def test_reconcile_uses_note_call_date(app, coverage_data):
    """Unique note-backed activity links even when its due date is next day."""
    with app.app_context():
        note = Note(
            customer_id=coverage_data['customer_id'],
            call_date=datetime.combine(date.today(), datetime.min.time()),
            content='Customer meeting notes',
        )
        db.session.add(note)
        db.session.flush()
        task = MsxTask(
            msx_task_id='historical-note-task',
            subject='Different but valid activity subject',
            task_category=861980000,
            task_category_name='Customer Engagement',
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(
                date.today() + timedelta(days=1), datetime.min.time(),
            ),
            note_id=note.id,
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        result = activity_coverage.reconcile_existing_activities()

        assert result['linked'] == 1
        assert task.meeting_id == coverage_data['meeting_id']
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        assert meeting.note_id == note.id
        db.session.delete(task)
        db.session.delete(note)
        db.session.commit()


def test_reconcile_leaves_ambiguous_note_activity_unlinked(app, coverage_data):
    """Multiple same-customer meetings require explicit user confirmation."""
    with app.app_context():
        second_meeting = PrefetchedMeeting(
            workiq_id='second-coverage-meeting',
            subject='Second customer meeting',
            start_time=datetime.combine(date.today(), datetime.min.time()),
            meeting_date=date.today(),
            customer_id=coverage_data['customer_id'],
            expires_at=datetime.now() + timedelta(days=5),
        )
        note = Note(
            customer_id=coverage_data['customer_id'],
            call_date=datetime.combine(date.today(), datetime.min.time()),
            content='Ambiguous customer meeting notes',
        )
        db.session.add_all([second_meeting, note])
        db.session.flush()
        task = MsxTask(
            msx_task_id='ambiguous-note-task',
            subject='Customer follow-up',
            task_category=861980000,
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            note_id=note.id,
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        result = activity_coverage.reconcile_existing_activities()

        assert result['linked'] == 0
        assert result['ambiguous'] == 1
        assert task.meeting_id is None
        db.session.delete(task)
        db.session.delete(second_meeting)
        db.session.delete(note)
        db.session.commit()


def test_sync_and_reconcile_refreshes_tasks_before_matching(app):
    """Reconciliation refreshes local MSX tasks before matching meetings."""
    def task_sync():
        yield 1, 1, 'Tasks batch 1/1', 'ok'
        return {
            'success': True,
            'tasks_created': 2,
            'tasks_updated': 3,
            'error': '',
        }

    with app.app_context(), patch(
            'app.services.milestone_sync._sync_team_milestones',
            return_value={'success': True},
        ), patch(
        'app.services.milestone_sync._sync_all_tasks',
        side_effect=task_sync,
        ), patch(
            'app.services.milestone_sync._sync_caip_activities',
            return_value={'success': True, 'activities_synced': 4},
    ), patch(
        'app.services.activity_coverage.reconcile_existing_activities',
        return_value={'scanned': 5, 'linked': 4, 'ambiguous': 1},
    ) as reconcile:
        activity_coverage._sync_and_reconcile()

    reconcile.assert_called_once_with()
    state = activity_coverage.get_reconciliation_status()
    assert state['tasks_created'] == 2
    assert state['tasks_updated'] == 3
    assert state['linked'] == 4
    assert state['ambiguous'] == 1


def test_enrichment_prefers_on_team_milestones():
    """Matcher tries team milestones before considering off-team milestones."""
    milestones = [
        {
            'local_id': 1, 'id': 'team-id', 'name': 'Team milestone',
            'status': 'On Track', 'opportunity': '', 'workload': 'Fabric',
            'on_my_team': True,
        },
        {
            'local_id': 2, 'id': 'other-id', 'name': 'Other milestone',
            'status': 'On Track', 'opportunity': '', 'workload': 'AI',
            'on_my_team': False,
        },
    ]
    with patch('app.services.activity_enrichment.gateway_call', return_value={
        'milestone_id': 'team-id',
        'reason': 'Strong team match',
    }) as gateway:
        result = activity_enrichment._match_milestone('Fabric workshop', milestones)

    assert result['milestone_id'] == 1
    assert result['on_my_team'] is True
    gateway.assert_called_once()


def test_enrichment_allows_off_team_fallback():
    """Matcher considers off-team milestones when team choices have no fit."""
    milestones = [
        {
            'local_id': 1, 'id': 'team-id', 'name': 'Unrelated milestone',
            'status': 'On Track', 'opportunity': '', 'workload': 'Security',
            'on_my_team': True,
        },
        {
            'local_id': 2, 'id': 'other-id', 'name': 'Relevant milestone',
            'status': 'At Risk', 'opportunity': '', 'workload': 'Fabric',
            'on_my_team': False,
        },
    ]
    with patch('app.services.activity_enrichment.gateway_call', side_effect=[
        {'milestone_id': None},
        {'milestone_id': 'other-id', 'reason': 'Best content match'},
    ]) as gateway:
        result = activity_enrichment._match_milestone('Fabric workshop', milestones)

    assert result['milestone_id'] == 2
    assert result['on_my_team'] is False
    assert gateway.call_count == 2


@pytest.mark.parametrize(('text', 'expected'), [
    ('Fabric architecture design session', 861980004),
    ('Customer L300 demo', 606820009),
    ('Resolve deployment blocker', 861980006),
    ('Azure adoption planning', 861980007),
    ('Build a rapid prototype', 606820006),
])
def test_enrichment_prefers_hok_task_categories(text, expected):
    """Prepared activity types always prefer an HoK-credit category."""
    category = activity_enrichment._category_for_text(text)

    assert category == expected
    assert category in activity_enrichment.HOK_TASK_CATEGORIES


@pytest.mark.parametrize(('text', 'expected'), [
    ('Complete customer readiness assessment', 861980014),
    ('Review RFP response', 861980009),
    ('Routine customer conversation', 861980000),
])
def test_enrichment_uses_non_hok_fallback_when_needed(text, expected):
    """Unmatched intent keeps an accurate non-HoK category."""
    category = activity_enrichment._category_for_text(text)

    assert category == expected
    assert category not in activity_enrichment.HOK_TASK_CATEGORIES


def test_enrichment_refreshes_local_milestones_before_matching():
    """Preparation consumes the batched sync and requires its completion."""
    events = iter([
        'event: start\ndata: {"total": 2}\n\n',
        'event: complete\ndata: {"success": true, "synced": 2}\n\n',
    ])
    with patch(
        'app.services.milestone_sync.sync_all_customer_milestones_stream',
        return_value=events,
    ) as sync:
        result = activity_enrichment._refresh_local_milestones()

    sync.assert_called_once_with()
    assert result == {'success': True, 'synced': 2}


def test_enrichment_detects_improved_account_sync(app):
    """Only current-version successful account syncs satisfy prerequisite."""
    with app.app_context():
        SyncStatus.mark_started('accounts')
        SyncStatus.mark_completed(
            'accounts',
            success=True,
            details='{"sync_version": 2}',
        )

        assert activity_enrichment._account_sync_is_current() is True


def test_enrichment_runs_improved_account_sync_once(app):
    """Missing version marker triggers account sync before milestone refresh."""
    with app.app_context(), patch(
        'app.routes.msx.run_account_sync_headless',
        side_effect=lambda: SyncStatus.mark_completed(
            'accounts',
            success=True,
            details='{"sync_version": 2}',
        ),
    ) as account_sync:
        SyncStatus.mark_started('accounts')
        SyncStatus.mark_completed('accounts', success=True, details='legacy sync')

        assert activity_enrichment._ensure_current_account_sync() is True
        assert activity_enrichment._ensure_current_account_sync() is False

    account_sync.assert_called_once_with()


def test_enrichment_status_reports_msx_refresh_phase(app, coverage_data):
    """Running job with meetings still queued reports MSX refresh feedback."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_QUEUED
        job = Job(
            job_type=activity_enrichment.JOB_TYPE,
            status=Job.STATUS_RUNNING,
            dedupe_key=activity_enrichment.JOB_DEDUPE_KEY,
        )
        db.session.add(job)
        SyncStatus.mark_started('accounts')
        SyncStatus.mark_completed(
            'accounts',
            success=True,
            details='{"sync_version": 2}',
        )
        db.session.commit()

        status = activity_enrichment.get_enrichment_status()

        assert status['phase'] == 'refreshing_msx'
        db.session.delete(job)
        meeting.enrichment_status = None
        db.session.commit()


def test_enrichment_status_reports_account_sync_phase(app, coverage_data):
    """Unversioned account data reports account priming before milestones."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_QUEUED
        job = Job(
            job_type=activity_enrichment.JOB_TYPE,
            status=Job.STATUS_RUNNING,
            dedupe_key=activity_enrichment.JOB_DEDUPE_KEY,
        )
        db.session.add(job)
        db.session.commit()

        status = activity_enrichment.get_enrichment_status()

        assert status['phase'] == 'syncing_accounts'
        db.session.delete(job)
        meeting.enrichment_status = None
        db.session.commit()


def test_enrichment_job_persists_result(app, coverage_data):
    """Completed enrichment fills drafts and suggested milestone durably."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_QUEUED
        db.session.commit()
        enriched = {
            'summary': 'Discussed Fabric architecture and implementation steps.',
            'task_subject': 'Document Fabric architecture',
            'task_description': 'Capture the agreed implementation plan.',
            'task_category': 861980004,
            'milestone_id': coverage_data['milestone_id'],
            'match_reason': 'Architecture work advances this milestone.',
            'matched_on_team': True,
            'used_fallback': False,
        }
        with patch(
            'app.services.activity_enrichment._enrich_external',
            return_value=enriched,
        ), patch(
            'app.services.activity_enrichment._ensure_current_account_sync',
            return_value=False,
        ), patch(
            'app.services.activity_enrichment._refresh_local_milestones',
            return_value={'success': True},
        ):
            result = activity_enrichment.process_enrichment_job({
                'meeting_ids': [meeting.id],
            })

        assert result == {'total': 1, 'completed': 1, 'failed': 0}
        assert meeting.enrichment_status == activity_enrichment.STATUS_COMPLETE
        assert meeting.enrichment_summary.startswith('Discussed Fabric')
        assert meeting.suggested_milestone_id == coverage_data['milestone_id']
        assert meeting.milestone_id == coverage_data['milestone_id']
        assert meeting.draft_subject == 'Document Fabric architecture'
        assert meeting.draft_task_category == 861980004
        meeting.enrichment_status = None
        meeting.enrichment_summary = None
        meeting.suggested_milestone_id = None
        meeting.milestone_id = None
        meeting.draft_subject = None
        meeting.draft_description = None
        meeting.draft_task_category = None
        meeting.enriched_at = None
        db.session.commit()


def test_enrichment_preserves_manual_draft_choices(app, coverage_data):
    """Batch stores its suggestion without replacing user-reviewed fields."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_QUEUED
        meeting.milestone_id = coverage_data['milestone_id']
        meeting.draft_subject = 'Keep reviewed subject'
        meeting.draft_description = 'Keep reviewed description'
        meeting.draft_task_category = 861980002
        db.session.commit()
        enriched = {
            'summary': 'Stored source context.',
            'task_subject': 'Replacement subject',
            'task_description': 'Replacement description',
            'task_category': 861980004,
            'milestone_id': coverage_data['milestone_id'],
            'match_reason': 'Suggested from transcript.',
            'matched_on_team': True,
            'used_fallback': False,
        }
        with patch(
            'app.services.activity_enrichment._enrich_external',
            return_value=enriched,
        ), patch(
            'app.services.activity_enrichment._ensure_current_account_sync',
            return_value=False,
        ), patch(
            'app.services.activity_enrichment._refresh_local_milestones',
            return_value={'success': True},
        ):
            activity_enrichment.process_enrichment_job({
                'meeting_ids': [meeting.id],
            })

        assert meeting.draft_subject == 'Keep reviewed subject'
        assert meeting.draft_description == 'Keep reviewed description'
        assert meeting.draft_task_category == 861980002
        assert meeting.suggested_milestone_id == coverage_data['milestone_id']
        meeting.enrichment_status = None
        meeting.enrichment_summary = None
        meeting.suggested_milestone_id = None
        meeting.milestone_id = None
        meeting.draft_subject = None
        meeting.draft_description = None
        meeting.draft_task_category = None
        meeting.enriched_at = None
        db.session.commit()


def test_enrichment_job_resumes_interrupted_running_row(app, coverage_data):
    """A reclaimed durable job processes rows left running by a dead worker."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_RUNNING
        db.session.commit()
        enriched = {
            'summary': 'Recovered summary.',
            'task_subject': 'Recovered task',
            'task_description': 'Recovered description',
            'task_category': 861980000,
            'milestone_id': coverage_data['milestone_id'],
            'match_reason': 'Recovered match.',
            'matched_on_team': True,
            'used_fallback': False,
        }
        with patch(
            'app.services.activity_enrichment._enrich_external',
            return_value=enriched,
        ), patch(
            'app.services.activity_enrichment._ensure_current_account_sync',
            return_value=False,
        ), patch(
            'app.services.activity_enrichment._refresh_local_milestones',
            return_value={'success': True},
        ):
            result = activity_enrichment.process_enrichment_job({
                'meeting_ids': [meeting.id],
            })

        assert result['completed'] == 1
        assert meeting.enrichment_status == activity_enrichment.STATUS_COMPLETE
        meeting.enrichment_status = None
        meeting.enrichment_summary = None
        meeting.suggested_milestone_id = None
        meeting.milestone_id = None
        meeting.draft_subject = None
        meeting.draft_description = None
        meeting.draft_task_category = None
        meeting.enriched_at = None
        db.session.commit()


def test_population_imports_fiscal_year_then_catches_up(app):
    """First run starts July 1; later run starts after durable checkpoint."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()
        try:
            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as sync:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert result == {'completed_count': 3, 'error': None}
            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-01', '2026-07-02', '2026-07-03',
            ]
            row = db.session.get(ActivityCoveragePopulation, 1)
            assert row.populated_through == date(2026, 7, 3)

            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as sync:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 6))

            assert result == {'completed_count': 1, 'error': None}
            sync.assert_called_once_with('2026-07-06')
            assert row.populated_through == date(2026, 7, 6)
            status = activity_coverage.get_population_status(date(2026, 7, 6))
            assert status['label'] == 'Up to date'
            assert status['can_start'] is False
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_population_retries_transient_failure(app):
    """A transient date failure recovers without pausing the population."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()
        try:
            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                side_effect=[
                    ([], None),
                    ([], 'Temporary Outlook error'),
                    ([], None),
                    ([], None),
                ],
            ) as sync, patch(
                'app.services.activity_coverage._wait_before_retry',
            ) as wait:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert result == {'completed_count': 3, 'error': None}
            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-01', '2026-07-02', '2026-07-02', '2026-07-03',
            ]
            wait.assert_called_once_with(2)
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_population_pauses_after_retries_and_resumes(app):
    """Retry exhaustion preserves checkpoint so Catch up retries failed date."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()
        try:
            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                side_effect=[
                    ([], None),
                    ([], 'WorkIQ timeout'),
                    ([], 'WorkIQ timeout'),
                    ([], 'WorkIQ timeout'),
                ],
            ) as sync, patch(
                'app.services.activity_coverage._wait_before_retry',
            ) as wait:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert result['completed_count'] == 1
            assert result['error'] == (
                'Paused at 2026-07-02 after 3 attempts: WorkIQ timeout'
            )
            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-01', '2026-07-02', '2026-07-02', '2026-07-02',
            ]
            assert [call.args[0] for call in wait.call_args_list] == [2, 5]
            row = db.session.get(ActivityCoveragePopulation, 1)
            assert row.populated_through == date(2026, 7, 1)
            status = activity_coverage.get_population_status(date(2026, 7, 3))
            assert status['label'] == 'Retry Calendar Import'
            assert status['can_start'] is True

            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as sync:
                activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-02', '2026-07-03',
            ]
            assert row.populated_through == date(2026, 7, 3)
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_workiq_population_fetches_five_days_concurrently(app):
    """WorkIQ network calls overlap while database writes remain date ordered."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()
        barrier = threading.Barrier(5)
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def fetch_day(date_str):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            barrier.wait(timeout=5)
            with state_lock:
                active -= 1
            return [{'subject': date_str}], None

        try:
            with patch(
                'app.services.meeting_prefetch.fetch_workiq_meetings_for_date',
                side_effect=fetch_day,
            ), patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as store:
                result = activity_coverage._populate_fiscal_year_from_workiq(
                    date(2026, 7, 7),
                )

            assert result == {
                'completed_count': 5,
                'error': None,
                'source': 'workiq',
            }
            assert max_active == 5
            assert [call.args[0] for call in store.call_args_list] == [
                '2026-07-01',
                '2026-07-02',
                '2026-07-03',
                '2026-07-06',
                '2026-07-07',
            ]
            assert all('prefetched_meetings' in call.kwargs for call in store.call_args_list)
            row = db.session.get(ActivityCoveragePopulation, 1)
            assert row.populated_through == date(2026, 7, 7)
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_workiq_population_retry_resumes_from_failed_date(app):
    """A failed parallel batch retains earlier serial checkpoints for retry."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()

        def fetch_day(date_str):
            if date_str == '2026-07-03':
                return None, 'WorkIQ timeout'
            return [{'subject': date_str}], None

        try:
            with patch(
                'app.services.meeting_prefetch.fetch_workiq_meetings_for_date',
                side_effect=fetch_day,
            ), patch(
                'app.services.activity_coverage._wait_before_retry',
            ), patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as store:
                with pytest.raises(RuntimeError, match='Paused at 2026-07-03'):
                    activity_coverage._populate_fiscal_year_from_workiq(
                        date(2026, 7, 7),
                    )

            assert [call.args[0] for call in store.call_args_list] == [
                '2026-07-01',
                '2026-07-02',
            ]
            row = db.session.get(ActivityCoveragePopulation, 1)
            assert row.populated_through == date(2026, 7, 2)

            with patch(
                'app.services.meeting_prefetch.fetch_workiq_meetings_for_date',
                side_effect=lambda date_str: ([{'subject': date_str}], None),
            ) as fetch, patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ):
                result = activity_coverage._populate_fiscal_year_from_workiq(
                    date(2026, 7, 7),
                )

            assert result['completed_count'] == 3
            assert [call.args[0] for call in fetch.call_args_list] == [
                '2026-07-03',
                '2026-07-06',
                '2026-07-07',
            ]
            assert row.populated_through == date(2026, 7, 7)
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_population_status_reads_durable_pending_job(app):
    """Progress survives web navigation and process restart through SQLite."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        Job.query.filter_by(job_type='activity_coverage_population').delete()
        row = ActivityCoveragePopulation(
            id=1,
            fiscal_year_end=2027,
            populated_through=date(2026, 7, 2),
        )
        job = Job(
            job_type='activity_coverage_population',
            status=Job.STATUS_PENDING,
            payload=(
                '{"through":"2026-07-07","dates":['
                '"2026-07-01","2026-07-02","2026-07-03",'
                '"2026-07-06","2026-07-07"]}'
            ),
        )
        db.session.add_all([row, job])
        db.session.commit()
        try:
            status = activity_coverage.get_population_status(date(2026, 7, 7))

            assert status['running'] is True
            assert status['completed_count'] == 2
            assert status['total_dates'] == 5
            assert status['current_date'] == '2026-07-03'
            assert status['can_start'] is False
        finally:
            Job.query.filter_by(job_type='activity_coverage_population').delete()
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_start_population_enqueues_one_durable_job(app):
    """Calendar start uses queue dedupe instead of a web-process thread."""
    from app.services.job_queue import get_handler

    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        Job.query.filter_by(job_type='activity_coverage_population').delete()
        db.session.commit()
        try:
            assert activity_coverage.start_population(app) is True
            assert activity_coverage.start_population(app) is False

            jobs = Job.query.filter_by(
                job_type='activity_coverage_population',
            ).all()
            assert len(jobs) == 1
            assert jobs[0].status == Job.STATUS_PENDING
            assert jobs[0].dedupe_key == 'activity-coverage-population'
            assert get_handler('activity_coverage_population') is (
                activity_coverage.process_population_job
            )
        finally:
            Job.query.filter_by(job_type='activity_coverage_population').delete()
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_population_job_uses_outlook_when_corporate_calendar_is_available(app):
    """Fast Outlook imports remain sequential inside durable job handler."""
    with app.app_context(), patch(
        'app.services.outlook_calendar.corporate_outlook_available',
        return_value=True,
    ), patch(
        'app.services.activity_coverage.populate_fiscal_year',
        return_value={'completed_count': 5, 'error': None},
    ) as populate:
        result = activity_coverage.process_population_job({
            'through': '2026-07-07',
        })

    assert result == {
        'completed_count': 5,
        'error': None,
        'source': 'outlook',
    }
    populate.assert_called_once_with(date(2026, 7, 7))


def test_population_job_can_force_workiq_in_development(app, monkeypatch):
    """Development override exercises WorkIQ even when Outlook is available."""
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SALESBUDDY_CALENDAR_SOURCE', 'workiq')
    with app.app_context(), patch(
        'app.services.outlook_calendar.corporate_outlook_available',
    ) as outlook_available, patch(
        'app.services.activity_coverage._populate_fiscal_year_from_workiq',
        return_value={'completed_count': 5, 'error': None, 'source': 'workiq'},
    ) as populate:
        result = activity_coverage.process_population_job({
            'through': '2026-07-07',
        })

    assert result['source'] == 'workiq'
    outlook_available.assert_not_called()
    populate.assert_called_once_with(date(2026, 7, 7))


def test_population_job_ignores_workiq_override_outside_development(
    app,
    monkeypatch,
):
    """Production keeps automatic Outlook-first source selection."""
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('SALESBUDDY_CALENDAR_SOURCE', 'workiq')
    with app.app_context(), patch(
        'app.services.outlook_calendar.corporate_outlook_available',
        return_value=True,
    ) as outlook_available, patch(
        'app.services.activity_coverage.populate_fiscal_year',
        return_value={'completed_count': 5, 'error': None},
    ) as populate:
        result = activity_coverage.process_population_job({
            'through': '2026-07-07',
        })

    assert result['source'] == 'outlook'
    outlook_available.assert_called_once_with()
    populate.assert_called_once_with(date(2026, 7, 7))


def test_workiq_development_override_enables_five_day_reimport(
    app,
    monkeypatch,
):
    """Up-to-date dev databases expose a repeatable five-worker test run."""
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SALESBUDDY_CALENDAR_SOURCE', 'workiq')
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        Job.query.filter_by(job_type='activity_coverage_population').delete()
        db.session.add(ActivityCoveragePopulation(
            id=1,
            fiscal_year_end=2027,
            populated_through=date.today(),
        ))
        db.session.commit()
        try:
            status = activity_coverage.get_population_status()
            assert status['can_start'] is True
            assert status['label'] == 'Test WorkIQ Import'
            assert status['detail'] == 'Re-import latest 5 weekdays'

            assert activity_coverage.start_population(app) is True
            job = Job.query.filter_by(
                job_type='activity_coverage_population',
            ).one()
            payload = json.loads(job.payload)
            expected_dates = activity_coverage._recent_weekdays(date.today(), 5)
            assert payload['dates'] == [value.isoformat() for value in expected_dates]
            row = db.session.get(ActivityCoveragePopulation, 1)
            assert row.populated_through == expected_dates[0] - timedelta(days=1)
        finally:
            Job.query.filter_by(job_type='activity_coverage_population').delete()
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_report_week_is_clamped_to_current_fiscal_year(app):
    """Report navigation cannot escape current fiscal-year boundaries."""
    with app.app_context():
        data = activity_coverage.get_report_data(date(2020, 1, 1))
        fiscal_start, _ = activity_coverage.fiscal_year_bounds()
        assert data['week_start'] == activity_coverage.normalize_week_start(fiscal_start)
        assert data['can_go_previous'] is False


def test_full_fiscal_year_view_includes_other_weeks(app, coverage_data):
    """Full FY mode returns meetings outside the selected week."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.meeting_date = date.today() - timedelta(days=14)
        meeting.start_time = datetime.combine(meeting.meeting_date, datetime.min.time())
        db.session.commit()

        weekly = activity_coverage.get_report_data(date.today())
        full_year = activity_coverage.get_report_data(date.today(), view_all=True)

        assert all(row['id'] != meeting.id for row in weekly['meetings'])
        assert any(row['id'] == meeting.id for row in full_year['meetings'])
        assert full_year['view_all'] is True


def test_calendar_resync_preserves_logged_meeting(app, coverage_data):
    """Canceled-meeting cleanup must retain durable activity coverage rows."""
    with app.app_context():
        task = MsxTask(
            msx_task_id='resync-preserved-task',
            subject='Logged activity',
            task_category=861980000,
            task_category_name='Customer Engagement',
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            meeting_id=coverage_data['meeting_id'],
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        with patch(
            'app.services.meeting_prefetch.prefetch_for_date_full',
            return_value=(1, [{'id': 'different-meeting'}], None),
        ):
            from app.services.meeting_sync import sync_meetings_for_date
            _, error = sync_meetings_for_date(date.today().isoformat())

        assert error is None
        assert db.session.get(PrefetchedMeeting, coverage_data['meeting_id']) is not None
        db.session.delete(task)
        DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
        db.session.commit()


def test_report_page_and_hub_registration(client, coverage_data):
    """Report renders meeting workbench and is discoverable from reports hub."""
    response = client.get('/reports/activity-coverage')
    assert response.status_code == 200
    assert b'Activity Coverage' in response.data
    assert b'customer-picker-input' in response.data
    assert b'milestone-picker-input' in response.data
    assert b'\xe2\x98\x85 Architecture Design Session' in response.data
    assert b'Fabric architecture workshop' in response.data
    assert b'Create Activity' in response.data
    assert b'Match Milestones' in response.data
    assert b'Import calendar meetings from the last completed day through today' in response.data
    assert b'id="coverageFilter"' in response.data
    assert b'All meetings' in response.data
    assert b'salesbuddy_activity_coverage_view' in response.data
    assert b'activityCoveragePreferences' in response.data
    assert b'data-lens="meetings"' in response.data
    assert b'data-view="weekly"' in response.data
    assert b'aria-label="Expand all visible meetings"' in response.data
    assert b'Weekly' in response.data
    assert b'Full FY' in response.data
    assert b'event.persisted' in response.data

    full_year = client.get('/reports/activity-coverage?view=all')
    assert full_year.status_code == 200
    assert b'coverage-view-toggle' in full_year.data

    hub = client.get('/reports')
    assert hub.status_code == 200
    assert b'/reports/activity-coverage' in hub.data


def test_report_separates_selected_values_from_picker_search(
    app, client, coverage_data,
):
    """Selected customer and milestone render apart from empty search fields."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.milestone_id = coverage_data['milestone_id']
        db.session.commit()

    response = client.get('/reports/activity-coverage')
    html = response.get_data(as_text=True)

    assert 'customer-picker-selected' in html
    assert 'milestone-picker-selected' in html
    assert 'separateSelection: true' in html
    assert 'Search to change customer...' in html
    assert 'Search to change milestone...' in html
    assert 'value="Deploy Fabric"' not in html


def test_meeting_drafts_autosave_without_manual_enrich_or_save(
    client, coverage_data,
):
    """Meeting drafts autosave and refresh values shown in collapsed summaries."""
    response = client.get('/reports/activity-coverage')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'draft-save-status' in html
    assert 'queueAutosave' in html
    assert 'flushAutosave' in html
    assert 'createActivity' in html
    assert 'updateMeetingSummary(form, payload)' in html
    assert "row.querySelector('.meeting-customer')" in html
    assert "row.querySelector('.meeting-milestone')" in html
    assert 'aria-label="Clear selected customer"' in html
    assert 'aria-label="Clear selected milestone"' in html
    assert "select.dispatchEvent(new Event('change', { bubbles: true }))" in html
    assert 'save-draft' not in html
    assert 'enrich-draft' not in html


def test_create_and_link_update_meeting_inline_without_page_reload(
    client, coverage_data,
):
    """Create and link actions render logged state without losing page context."""
    response = client.get('/reports/activity-coverage')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'renderLoggedActivity(form, activity)' in html
    assert 'updateCoverageSummary(previousStatus)' in html
    assert "form.replaceWith(loggedDetail)" in html
    assert "applyCoverageFilter();" in html
    assert ".then(function(activity) {\n            renderLoggedActivity(form, activity);" in html
    assert ".then(function(activity) { renderLoggedActivity(form, activity); })" in html
    create_handler = html[html.index('function createActivity(form)'):
                          html.index("document.querySelectorAll('.create-activity')")]
    link_handler = html[html.index("document.querySelectorAll('.link-activity')"):
                        html.index("document.querySelectorAll('.dismiss-meeting')")]
    assert 'window.location.reload()' not in create_handler
    assert 'window.location.reload()' not in link_handler


def test_dismiss_updates_meeting_rows_inline_without_page_reload(
    client, coverage_data,
):
    """Dismiss removes returned meeting IDs and refreshes summary in place."""
    response = client.get('/reports/activity-coverage')
    html = response.get_data(as_text=True)
    handler = html[html.index("document.querySelectorAll('.dismiss-meeting')"):
                   html.index('var coverageFilter')]

    assert 'data.dismissed_ids.forEach' in handler
    assert 'setMeetingCoverageSummary(data.summary)' in handler
    assert 'row.remove()' in handler
    assert 'window.location.reload()' not in handler


def test_standalone_hok_updates_milestone_row_without_page_reload(
    client, coverage_data,
):
    """Standalone HoK creation renders covered state without navigation."""
    response = client.get('/reports/activity-coverage?lens=milestones')
    html = response.get_data(as_text=True)
    create_path = html[html.index('function renderStandaloneHok'):
                       html.index("document.querySelectorAll('.save-milestone-hok')")]

    assert 'setMilestoneCoverageSummary(task.summary)' in create_path
    assert 'renderStandaloneHok(form, task)' in create_path
    assert 'row.remove()' in create_path
    assert 'if (createAfter)' in create_path


def test_expand_all_keeps_individual_row_toggles_independent(
    client, coverage_data,
):
    """Rows stop enforcing accordion behavior after bulk expansion."""
    response = client.get('/reports/activity-coverage')
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'var bulkExpansionActive = false;' in html
    assert 'if (!bulkExpansionActive)' in html
    assert 'function syncExpandAllControl()' in html
    assert 'bulkExpansionActive = shouldExpand;' in html
    assert 'syncExpandAllControl();' in html


def test_customer_milestones_rank_team_then_active(app, client, coverage_data):
    """Picker ranks on-team milestones first, then active milestones."""
    with app.app_context():
        milestones = [
            Milestone(
                title='Off Team Completed',
                url='https://example.test/completed',
                msx_milestone_id='picker-completed',
                msx_status='Completed',
                on_my_team=False,
                due_date=datetime(2026, 12, 1),
                customer_id=coverage_data['customer_id'],
            ),
            Milestone(
                title='Off Team Active',
                url='https://example.test/active',
                msx_milestone_id='picker-active',
                msx_status='On Track',
                on_my_team=False,
                due_date=datetime(2026, 10, 1),
                customer_id=coverage_data['customer_id'],
            ),
            Milestone(
                title='On Team Completed',
                url='https://example.test/team',
                msx_milestone_id='picker-team',
                msx_status='Completed',
                on_my_team=True,
                due_date=datetime(2026, 8, 1),
                customer_id=coverage_data['customer_id'],
            ),
        ]
        db.session.add_all(milestones)
        db.session.commit()

    response = client.get(
        '/api/reports/activity-coverage/customers/'
        f'{coverage_data["customer_id"]}/milestones'
    )
    labels = [item['label'] for item in response.get_json()['milestones']]

    assert response.status_code == 200
    assert labels == [
        'Deploy Fabric',
        'On Team Completed',
        'Off Team Active',
        'Off Team Completed',
    ]

    with app.app_context():
        Milestone.query.filter(
            Milestone.msx_milestone_id.in_({
                'picker-completed', 'picker-active', 'picker-team',
            })
        ).delete(synchronize_session=False)
        db.session.commit()


def test_f1_help_explains_activity_coverage_workflow():
    """Contextual help distinguishes imports, matching, and creation."""
    help_script = Path('static/js/page-help.js').read_text(encoding='utf-8')

    assert "title: 'Activity Coverage'" in help_script
    assert '<strong>Re-run Matching</strong>' in help_script
    assert '<strong>Catch Up Calendar</strong>' in help_script
    assert '<strong>Full FY</strong>' in help_script
    assert 'qualifies for HoK credit' in help_script
    assert 'Nothing is created until you click it' in help_script


def test_reconciliation_routes(client):
    """Report can start reconciliation and poll its status."""
    with patch(
        'app.services.activity_coverage.start_reconciliation',
        return_value=True,
    ) as start:
        response = client.post('/api/reports/activity-coverage/reconcile')

    assert response.status_code == 200
    assert response.get_json()['success'] is True
    start.assert_called_once()

    status = client.get('/api/reports/activity-coverage/reconcile-status')
    assert status.status_code == 200
    assert status.get_json()['success'] is True


def test_enrichment_routes(client):
    """Report can queue enrichment and poll durable progress."""
    with patch(
        'app.services.activity_enrichment.start_enrichment',
        return_value={'started': True, 'job_id': 42, 'queued': 8},
    ) as start:
        response = client.post(
            '/api/reports/activity-coverage/match-milestones',
            json={'force': True},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        'success': True,
        'started': True,
        'job_id': 42,
        'queued': 8,
    }
    start.assert_called_once_with(force=True)

    status = client.get('/api/reports/activity-coverage/match-status')
    assert status.status_code == 200
    assert status.get_json()['success'] is True


def test_force_enrichment_clears_prior_preparation(app, coverage_data):
    """Re-running replaces generated drafts but preserves meeting identity and customer."""
    with app.app_context(), patch(
        'app.services.activity_enrichment.enqueue',
    ) as enqueue:
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.milestone_id = coverage_data['milestone_id']
        meeting.draft_subject = 'Old generated subject'
        meeting.draft_description = 'Old generated description'
        meeting.draft_task_category = 861980000
        meeting.enrichment_status = activity_enrichment.STATUS_COMPLETE
        meeting.enrichment_summary = 'Old summary'
        meeting.suggested_milestone_id = coverage_data['milestone_id']
        meeting.milestone_match_reason = 'Old reason'
        meeting.enrichment_attempts = 2
        db.session.commit()
        enqueue.return_value.id = 99

        result = activity_enrichment.start_enrichment(force=True)

        assert result == {'started': True, 'job_id': 99, 'queued': 1}
        assert meeting.customer_id == coverage_data['customer_id']
        assert meeting.milestone_id is None
        assert meeting.draft_subject is None
        assert meeting.draft_description is None
        assert meeting.draft_task_category is None
        assert meeting.enrichment_summary is None
        assert meeting.suggested_milestone_id is None
        assert meeting.milestone_match_reason is None
        assert meeting.enrichment_attempts == 0
        assert meeting.enrichment_status == activity_enrichment.STATUS_QUEUED
        enqueue.assert_called_once()


def test_milestone_options_include_team_membership(client, coverage_data):
    """Manual milestone choices expose team preference metadata."""
    response = client.get(
        '/api/reports/activity-coverage/customers/'
        f"{coverage_data['customer_id']}/milestones"
    )

    assert response.status_code == 200
    option = next(
        item for item in response.get_json()['milestones']
        if item['id'] == coverage_data['milestone_id']
    )
    assert isinstance(option['on_my_team'], bool)


def test_update_route_validates_required_subject(client, coverage_data):
    """Draft endpoint returns useful validation errors."""
    response = client.patch(
        f"/api/reports/activity-coverage/meetings/{coverage_data['meeting_id']}",
        json={
            'customer_id': coverage_data['customer_id'],
            'milestone_id': coverage_data['milestone_id'],
            'subject': '',
            'task_category': 861980000,
            'duration_minutes': 60,
        },
    )
    assert response.status_code == 400
    assert response.get_json()['error'] == 'Activity subject is required'