"""
Tests for the Add-to-Team feature (GitHub Issue #12).

Covers:
- POST /api/msx/join-milestone-team route
- POST /api/msx/join-deal-team route
- Team indicators in milestone_view, opportunity_view, milestone_tracker templates
"""
import json
from unittest.mock import patch

import pytest


# =============================================================================
# Helpers
# =============================================================================

def _create_milestone(db_session, sample_user, **overrides):
    """Create a Milestone with sensible defaults and return it."""
    from app.models import db, Milestone, Customer

    customer = Customer(name='Team Test Customer', tpid=5555)
    db.session.add(customer)
    db.session.flush()

    defaults = dict(
        url='https://example.com/milestone',
        title='Test Milestone',
        msx_milestone_id='ms-guid-001',
        msx_status='On Track',
        customer_id=customer.id,
        on_my_team=False,
    )
    defaults.update(overrides)
    milestone = Milestone(**defaults)
    db.session.add(milestone)
    db.session.commit()
    return milestone


def _create_opportunity(db_session, sample_user, **overrides):
    """Create an Opportunity with sensible defaults and return it."""
    from app.models import db, Opportunity, Customer

    customer = Customer(name='Opp Test Customer', tpid=6666)
    db.session.add(customer)
    db.session.flush()

    defaults = dict(
        msx_opportunity_id='opp-guid-001',
        opportunity_number='7-TEAM-TEST',
        name='Test Opportunity',
        customer_id=customer.id,
        on_deal_team=False,
    )
    defaults.update(overrides)
    opp = Opportunity(**defaults)
    db.session.add(opp)
    db.session.commit()
    return opp


def _tracker_milestone_dict(milestone_id=1, on_my_team=False, msx_milestone_id='ms-guid-001', **overrides):
    """Return a dict matching the structure from get_milestone_tracker_data()."""
    item = {
        "id": milestone_id,
        "title": "Test Milestone",
        "milestone_number": "7-999999999",
        "status": "On Track",
        "status_sort": 1,
        "opportunity_name": "Test Opp",
        "workload": "Infra: Test",
        "workload_area": "Infra",
        "monthly_usage": 100.0,
        "due_date": None,
        "dollar_value": 1000.0,
        "days_until_due": None,
        "fiscal_quarter": "",
        "fiscal_year": "",
        "urgency": "unknown",
        "url": "https://example.com/milestone",
        "msx_milestone_id": msx_milestone_id,
        "last_synced_at": None,
        "on_my_team": on_my_team,
        "customer": {"id": 1, "name": "Test Customer"},
        "seller": {"id": 1, "name": "Test Seller"},
        "territory": None,
        "opportunity": None,
    }
    item.update(overrides)
    return item


# =============================================================================
# Route Tests — Join Milestone Team
# =============================================================================

class TestJoinMilestoneTeam:
    """Tests for POST /api/msx/join-milestone-team."""

    @patch('app.routes.msx.add_user_to_milestone_team')
    def test_join_milestone_team_success(self, mock_join, app, client, db_session, sample_user):
        """Successful join sets on_my_team=True and returns success."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user)
            ms_id = ms.id

            mock_join.return_value = {"success": True}

            resp = client.post(
                '/api/msx/join-milestone-team',
                json={"milestone_id": ms_id},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True

            # Verify local flag was updated
            from app.models import Milestone
            updated = Milestone.query.get(ms_id)
            assert updated.on_my_team is True

    @patch('app.routes.msx.add_user_to_milestone_team')
    def test_join_milestone_team_already_member(self, mock_join, app, client, db_session, sample_user):
        """Already-on-team response still returns success and sets flag."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user)
            ms_id = ms.id

            mock_join.return_value = {"success": True, "already_on_team": True}

            resp = client.post(
                '/api/msx/join-milestone-team',
                json={"milestone_id": ms_id},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert data["already_on_team"] is True

            from app.models import Milestone
            assert Milestone.query.get(ms_id).on_my_team is True

    @patch('app.routes.msx.add_user_to_milestone_team')
    def test_join_milestone_team_api_error(self, mock_join, app, client, db_session, sample_user):
        """API failure returns error and does NOT set the flag."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user)
            ms_id = ms.id

            mock_join.return_value = {"success": False, "error": "CRM blew up"}

            resp = client.post(
                '/api/msx/join-milestone-team',
                json={"milestone_id": ms_id},
            )
            data = resp.get_json()
            assert data["success"] is False
            assert "CRM blew up" in data["error"]

            from app.models import Milestone
            assert Milestone.query.get(ms_id).on_my_team is False

    def test_join_milestone_team_not_found(self, app, client):
        """Non-existent milestone returns 404."""
        resp = client.post(
            '/api/msx/join-milestone-team',
            json={"milestone_id": 99999},
        )
        assert resp.status_code == 404

    def test_join_milestone_team_no_msx_id(self, app, client, db_session, sample_user):
        """Milestone without msx_milestone_id returns 400."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user, msx_milestone_id=None)
            ms_id = ms.id

            resp = client.post(
                '/api/msx/join-milestone-team',
                json={"milestone_id": ms_id},
            )
            assert resp.status_code == 400
            assert "no MSX ID" in resp.get_json()["error"]

    def test_join_milestone_team_missing_id(self, app, client):
        """Missing milestone_id in body returns 400."""
        resp = client.post(
            '/api/msx/join-milestone-team',
            json={},
        )
        assert resp.status_code == 400

    def test_join_milestone_team_no_json(self, app, client):
        """Non-JSON request returns 400."""
        resp = client.post(
            '/api/msx/join-milestone-team',
            data="not json",
            content_type='text/plain',
        )
        assert resp.status_code == 400


# =============================================================================
# Route Tests — Leave Milestone Team
# =============================================================================

class TestLeaveMilestoneTeam:
    """Tests for POST /api/msx/leave-milestone-team."""

    @patch('app.routes.msx.remove_user_from_milestone_team')
    def test_leave_milestone_team_success(self, mock_leave, app, client, db_session, sample_user):
        """Successful leave sets on_my_team=False and returns success."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user, on_my_team=True)
            ms_id = ms.id

            mock_leave.return_value = {"success": True}

            resp = client.post(
                '/api/msx/leave-milestone-team',
                json={"milestone_id": ms_id},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True

            from app.models import Milestone
            updated = Milestone.query.get(ms_id)
            assert updated.on_my_team is False

    @patch('app.routes.msx.remove_user_from_milestone_team')
    def test_leave_milestone_team_not_on_team(self, mock_leave, app, client, db_session, sample_user):
        """Not-on-team response still returns success and clears flag."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user, on_my_team=True)
            ms_id = ms.id

            mock_leave.return_value = {"success": True, "not_on_team": True}

            resp = client.post(
                '/api/msx/leave-milestone-team',
                json={"milestone_id": ms_id},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True

            from app.models import Milestone
            assert Milestone.query.get(ms_id).on_my_team is False

    @patch('app.routes.msx.remove_user_from_milestone_team')
    def test_leave_milestone_team_api_error(self, mock_leave, app, client, db_session, sample_user):
        """API failure returns error and does NOT clear the flag."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user, on_my_team=True)
            ms_id = ms.id

            mock_leave.return_value = {"success": False, "error": "CRM error"}

            resp = client.post(
                '/api/msx/leave-milestone-team',
                json={"milestone_id": ms_id},
            )
            data = resp.get_json()
            assert data["success"] is False
            assert "CRM error" in data["error"]

            from app.models import Milestone
            assert Milestone.query.get(ms_id).on_my_team is True

    def test_leave_milestone_team_not_found(self, app, client):
        """Non-existent milestone returns 404."""
        resp = client.post(
            '/api/msx/leave-milestone-team',
            json={"milestone_id": 99999},
        )
        assert resp.status_code == 404

    def test_leave_milestone_team_no_msx_id(self, app, client, db_session, sample_user):
        """Milestone without msx_milestone_id returns 400."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user, msx_milestone_id=None, on_my_team=True)
            ms_id = ms.id

            resp = client.post(
                '/api/msx/leave-milestone-team',
                json={"milestone_id": ms_id},
            )
            assert resp.status_code == 400
            assert "no MSX ID" in resp.get_json()["error"]

    def test_leave_milestone_team_missing_id(self, app, client):
        """Missing milestone_id in body returns 400."""
        resp = client.post(
            '/api/msx/leave-milestone-team',
            json={},
        )
        assert resp.status_code == 400

    def test_leave_milestone_team_no_json(self, app, client):
        """Non-JSON request returns 400."""
        resp = client.post(
            '/api/msx/leave-milestone-team',
            data="not json",
            content_type='text/plain',
        )
        assert resp.status_code == 400


# =============================================================================
# Route Tests — Join Deal Team
# =============================================================================

class TestJoinDealTeam:
    """Tests for POST /api/msx/join-deal-team."""

    @patch('app.services.msx_api.add_user_to_deal_team')
    def test_join_deal_team_success(self, mock_join, app, client, db_session, sample_user):
        """Successful join sets on_deal_team=True and returns success."""
        with app.app_context():
            opp = _create_opportunity(db_session, sample_user)
            opp_id = opp.id

            mock_join.return_value = {"success": True}

            resp = client.post(
                '/api/msx/join-deal-team',
                json={"opportunity_id": opp_id},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True

            from app.models import Opportunity
            assert Opportunity.query.get(opp_id).on_deal_team is True

    @patch('app.services.msx_api.add_user_to_deal_team')
    def test_join_deal_team_already_member(self, mock_join, app, client, db_session, sample_user):
        """Already on team still returns success."""
        with app.app_context():
            opp = _create_opportunity(db_session, sample_user)
            opp_id = opp.id

            mock_join.return_value = {"success": True, "already_on_team": True}

            resp = client.post(
                '/api/msx/join-deal-team',
                json={"opportunity_id": opp_id},
            )
            data = resp.get_json()
            assert data["success"] is True
            assert data["already_on_team"] is True

            from app.models import Opportunity
            assert Opportunity.query.get(opp_id).on_deal_team is True

    @patch('app.services.msx_api.add_user_to_deal_team')
    def test_join_deal_team_api_error(self, mock_join, app, client, db_session, sample_user):
        """API failure returns error without setting flag."""
        with app.app_context():
            opp = _create_opportunity(db_session, sample_user)
            opp_id = opp.id

            mock_join.return_value = {"success": False, "error": "Auth expired"}

            resp = client.post(
                '/api/msx/join-deal-team',
                json={"opportunity_id": opp_id},
            )
            data = resp.get_json()
            assert data["success"] is False

            from app.models import Opportunity
            assert Opportunity.query.get(opp_id).on_deal_team is False

    def test_join_deal_team_not_found(self, app, client):
        """Non-existent opportunity returns 404."""
        resp = client.post(
            '/api/msx/join-deal-team',
            json={"opportunity_id": 99999},
        )
        assert resp.status_code == 404

    def test_join_deal_team_no_msx_id(self, app, client, db_session, sample_user):
        """Opportunity with empty msx_opportunity_id returns 400."""
        with app.app_context():
            from app.models import db, Opportunity, Customer

            customer = Customer(name='No MSX Cust', tpid=7777)
            db.session.add(customer)
            db.session.flush()

            # Use empty string — the route checks `if not opportunity.msx_opportunity_id`
            opp = Opportunity(
                msx_opportunity_id='',
                name='No MSX Opp',
                customer_id=customer.id,
            )
            db.session.add(opp)
            db.session.commit()
            opp_id = opp.id

            resp = client.post(
                '/api/msx/join-deal-team',
                json={"opportunity_id": opp_id},
            )
            assert resp.status_code == 400
            assert "no MSX ID" in resp.get_json()["error"]

    def test_join_deal_team_missing_id(self, app, client):
        """Missing opportunity_id in body returns 400."""
        resp = client.post(
            '/api/msx/join-deal-team',
            json={},
        )
        assert resp.status_code == 400

    def test_join_deal_team_no_json(self, app, client):
        """Non-JSON request returns 400."""
        resp = client.post(
            '/api/msx/join-deal-team',
            data="not json",
            content_type='text/plain',
        )
        assert resp.status_code == 400


# =============================================================================
# Template Tests — Milestone View
# =============================================================================

class TestMilestoneViewTeamIndicator:
    """Team indicator on the milestone detail page."""

    def test_milestone_view_shows_on_team_badge(self, app, client, db_session, sample_user):
        """When on_my_team is True, page shows 'On Team' badge and Leave button."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user, on_my_team=True)
            resp = client.get(f'/milestone/{ms.id}')
            assert resp.status_code == 200
            html = resp.data.decode()
            assert 'On Team' in html
            assert 'bg-success' in html
            assert 'leaveMilestoneTeam' in html
            assert 'Leave' in html

    def test_milestone_view_shows_join_button(self, app, client, db_session, sample_user):
        """When on_my_team is False and has MSX ID, page shows Join button."""
        with app.app_context():
            ms = _create_milestone(db_session, sample_user, on_my_team=False)
            resp = client.get(f'/milestone/{ms.id}')
            assert resp.status_code == 200
            html = resp.data.decode()
            assert 'Join Team' in html
            assert 'joinMilestoneTeam' in html


# =============================================================================
# Template Tests — Opportunity View
# =============================================================================

class TestOpportunityViewTeamIndicator:
    """Deal team indicator on the opportunity detail page."""

    @patch('app.routes.opportunities.get_opportunity')
    def test_opportunity_view_shows_on_team_badge(self, mock_get, app, client, db_session, sample_user):
        """When on_deal_team is True, page shows 'On Team' badge."""
        mock_get.return_value = {"success": False, "error": "test skip"}
        with app.app_context():
            opp = _create_opportunity(db_session, sample_user, on_deal_team=True)
            resp = client.get(f'/opportunity/{opp.id}')
            assert resp.status_code == 200
            html = resp.data.decode()
            assert 'On Team' in html

    @patch('app.routes.opportunities.get_opportunity')
    def test_opportunity_view_shows_join_button(self, mock_get, app, client, db_session, sample_user):
        """When on_deal_team is False, page shows Join Deal Team button."""
        mock_get.return_value = {"success": True, "opportunity": {
            "name": "Test Opp", "number": "7-TEST", "status": "Open",
            "value": 1000, "comments": [], "url": "https://example.com",
        }}
        with app.app_context():
            opp = _create_opportunity(db_session, sample_user, on_deal_team=False)
            resp = client.get(f'/opportunity/{opp.id}')
            assert resp.status_code == 200
            html = resp.data.decode()
            assert 'Join Deal Team' in html
            assert 'joinDealTeam' in html


# =============================================================================
# Template Tests — Milestone Tracker
# =============================================================================

class TestMilestoneTrackerTeamColumn:
    """Team column in the milestone tracker table."""

    @patch('app.services.milestone_sync.get_milestone_tracker_data')
    def test_tracker_shows_team_column_header(self, mock_data, app, client):
        """Tracker table has a 'Team' column header."""
        mock_data.return_value = {
            "milestones": [_tracker_milestone_dict()],
            "summary": {"total_count": 1, "total_monthly_usage": 100, "past_due_count": 0, "this_week_count": 0},
            "last_sync": None,
            "sellers": [],
            "areas": [],
            "quarters": [],
        }
        resp = client.get('/milestone-tracker')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert '>Team<' in html

    @patch('app.services.milestone_sync.get_milestone_tracker_data')
    def test_tracker_on_team_badge(self, mock_data, app, client):
        """Milestone with on_my_team=True shows green badge in Team column."""
        mock_data.return_value = {
            "milestones": [_tracker_milestone_dict(on_my_team=True)],
            "summary": {"total_count": 1, "total_monthly_usage": 100, "past_due_count": 0, "this_week_count": 0},
            "last_sync": None,
            "sellers": [],
            "areas": [],
            "quarters": [],
        }
        resp = client.get('/milestone-tracker')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'bi-people-fill' in html

    @patch('app.services.milestone_sync.get_milestone_tracker_data')
    def test_tracker_join_button(self, mock_data, app, client):
        """Milestone with on_my_team=False and MSX ID shows join button."""
        mock_data.return_value = {
            "milestones": [_tracker_milestone_dict(on_my_team=False, msx_milestone_id='ms-guid-001')],
            "summary": {"total_count": 1, "total_monthly_usage": 100, "past_due_count": 0, "this_week_count": 0},
            "last_sync": None,
            "sellers": [],
            "areas": [],
            "quarters": [],
        }
        resp = client.get('/milestone-tracker')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'joinTrackerTeam' in html

    @patch('app.services.milestone_sync.get_milestone_tracker_data')
    def test_tracker_no_msx_id_shows_dash(self, mock_data, app, client):
        """Milestone without MSX ID shows dash (no join button)."""
        mock_data.return_value = {
            "milestones": [_tracker_milestone_dict(on_my_team=False, msx_milestone_id=None)],
            "summary": {"total_count": 1, "total_monthly_usage": 100, "past_due_count": 0, "this_week_count": 0},
            "last_sync": None,
            "sellers": [],
            "areas": [],
            "quarters": [],
        }
        resp = client.get('/milestone-tracker')
        assert resp.status_code == 200
        html = resp.data.decode()
        # Should NOT have a join button in any table cell
        assert 'onclick="joinTrackerTeam' not in html


# =============================================================================
# Model Tests — on_deal_team field
# =============================================================================

class TestOpportunityDealTeamField:
    """Verify the on_deal_team field on the Opportunity model."""

    def test_on_deal_team_defaults_false(self, app, db_session, sample_user):
        """New opportunities default to on_deal_team=False."""
        with app.app_context():
            opp = _create_opportunity(db_session, sample_user)
            assert opp.on_deal_team is False

    def test_on_deal_team_can_be_set_true(self, app, db_session, sample_user):
        """on_deal_team can be toggled to True."""
        with app.app_context():
            from app.models import db, Opportunity
            opp = _create_opportunity(db_session, sample_user)
            opp.on_deal_team = True
            db.session.commit()

            refreshed = Opportunity.query.get(opp.id)
            assert refreshed.on_deal_team is True


# =============================================================================
# Auto-join milestone team on note save
# =============================================================================

class TestAutoJoinTeamOnNoteSave:
    """Saving a note with a milestone should auto-join the milestone team."""

    @patch('app.routes.notes.add_user_to_milestone_team')
    def test_note_create_auto_joins_milestone_team(self, mock_join, client, app, sample_data):
        """Creating a note with a milestone calls add_user_to_milestone_team."""
        mock_join.return_value = {'success': True}

        response = client.post('/note/new', data={
            'customer_id': sample_data['customer1_id'],
            'call_date': '2026-03-15',
            'content': '<p>Test auto join</p>',
            'milestone_msx_id': 'auto-join-guid-001',
            'milestone_url': 'https://example.com/milestone/auto-join-guid-001',
            'milestone_name': 'Auto Join Test',
            'milestone_status': 'On Track',
            'milestone_status_code': '1',
            'milestone_opportunity_name': 'Test Opp',
        }, follow_redirects=True)

        assert response.status_code == 200
        mock_join.assert_called_once_with('auto-join-guid-001')

        # Verify on_my_team was set
        with app.app_context():
            from app.models import Milestone
            ms = Milestone.query.filter_by(msx_milestone_id='auto-join-guid-001').first()
            assert ms is not None
            assert ms.on_my_team is True

    @patch('app.routes.notes.add_user_to_milestone_team')
    def test_note_create_already_on_team_skips_join(self, mock_join, client, app, sample_data):
        """If already on_my_team, should not call add_user_to_milestone_team."""
        # Pre-create the milestone with on_my_team=True
        with app.app_context():
            from app.models import db, Milestone
            ms = Milestone(
                msx_milestone_id='existing-team-guid',
                url='https://example.com/milestone/existing-team-guid',
                title='Already On Team',
                msx_status='On Track',
                on_my_team=True,
            )
            db.session.add(ms)
            db.session.commit()

        response = client.post('/note/new', data={
            'customer_id': sample_data['customer1_id'],
            'call_date': '2026-03-15',
            'content': '<p>Already on team test</p>',
            'milestone_msx_id': 'existing-team-guid',
            'milestone_url': 'https://example.com/milestone/existing-team-guid',
            'milestone_name': 'Already On Team',
            'milestone_status': 'On Track',
            'milestone_status_code': '1',
        }, follow_redirects=True)

        assert response.status_code == 200
        mock_join.assert_not_called()

    @patch('app.routes.notes.add_user_to_milestone_team')
    def test_auto_join_failure_does_not_block_note_save(self, mock_join, client, app, sample_data):
        """If team join fails, note should still save successfully."""
        mock_join.side_effect = Exception('MSX unreachable')

        response = client.post('/note/new', data={
            'customer_id': sample_data['customer1_id'],
            'call_date': '2026-03-15',
            'content': '<p>Join failed but note saves</p>',
            'milestone_msx_id': 'fail-join-guid',
            'milestone_url': 'https://example.com/milestone/fail-join-guid',
            'milestone_name': 'Fail Join Test',
            'milestone_status': 'On Track',
            'milestone_status_code': '1',
        }, follow_redirects=True)

        assert response.status_code == 200
        # Note should still be saved
        with app.app_context():
            from app.models import Note
            note = Note.query.filter(Note.content.contains('Join failed')).first()
            assert note is not None
            assert len(note.milestones) == 1
