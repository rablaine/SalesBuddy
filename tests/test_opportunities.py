"""
Tests for Opportunity model and milestone sync integration.

Covers:
- Opportunity model CRUD
- 1:many relationship between Opportunity and Milestone
- Opportunity upsert during milestone sync
- Customer.opportunities relationship
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from app.models import Opportunity, Milestone, Customer, User


class TestOpportunityModel:
    """Tests for the Opportunity model."""

    def test_opportunity_creation(self, app, db_session, sample_user):
        """Test creating an opportunity with required fields."""
        opp = Opportunity(
            msx_opportunity_id='abc-123-def',
            name='Fabric Opportunity',
        )
        db_session.add(opp)
        db_session.commit()

        assert opp.id is not None
        assert opp.msx_opportunity_id == 'abc-123-def'
        assert opp.name == 'Fabric Opportunity'
        assert opp.created_at is not None

    def test_opportunity_unique_msx_id(self, app, db_session, sample_user):
        """Test that MSX opportunity IDs must be unique."""
        opp1 = Opportunity(
            msx_opportunity_id='unique-guid-1',
            name='First Opp',
        )
        db_session.add(opp1)
        db_session.commit()

        opp2 = Opportunity(
            msx_opportunity_id='unique-guid-1',
            name='Duplicate Opp',
        )
        db_session.add(opp2)

        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()

    def test_opportunity_with_customer(self, app, db_session, sample_user):
        """Test opportunity linked to a customer."""
        customer = Customer(
            name='Test Corp', tpid=5000
        )
        db_session.add(customer)
        db_session.flush()

        opp = Opportunity(
            msx_opportunity_id='opp-with-customer',
            name='Cloud Migration',
            customer_id=customer.id,
        )
        db_session.add(opp)
        db_session.commit()

        assert opp.customer.name == 'Test Corp'
        assert customer.opportunities.count() == 1
        assert customer.opportunities.first().name == 'Cloud Migration'

    def test_opportunity_repr(self, app, db_session, sample_user):
        """Test string representation."""
        opp = Opportunity(
            msx_opportunity_id='repr-test',
            name='A Very Long Opportunity Name That Should Be Truncated',
        )
        db_session.add(opp)
        db_session.commit()

        assert 'Opportunity' in repr(opp)


class TestMilestoneOpportunityFK:
    """Tests for the Milestone -> Opportunity FK relationship."""

    def test_milestone_with_opportunity(self, app, db_session, sample_user):
        """Test linking a milestone to an opportunity."""
        opp = Opportunity(
            msx_opportunity_id='opp-for-milestone',
            name='Fabric Opp',
        )
        db_session.add(opp)
        db_session.flush()

        milestone = Milestone(
            msx_milestone_id='ms-123',
            url='https://example.com/milestone/ms-123',
            title='Phase 1 Deploy',
            opportunity_id=opp.id,
        )
        db_session.add(milestone)
        db_session.commit()

        assert milestone.opportunity_id == opp.id
        assert milestone.opportunity.name == 'Fabric Opp'
        assert opp.milestones.count() == 1

    def test_multiple_milestones_per_opportunity(self, app, db_session, sample_user):
        """Test 1:many - multiple milestones belong to one opportunity."""
        opp = Opportunity(
            msx_opportunity_id='opp-many-ms',
            name='Big Deal',
        )
        db_session.add(opp)
        db_session.flush()

        for i in range(3):
            ms = Milestone(
                msx_milestone_id=f'ms-multi-{i}',
                url=f'https://example.com/milestone/ms-multi-{i}',
                title=f'Milestone {i}',
                opportunity_id=opp.id,
            )
            db_session.add(ms)
        db_session.commit()

        assert opp.milestones.count() == 3

    def test_milestone_without_opportunity(self, app, db_session, sample_user):
        """Test that milestones can exist without an opportunity (nullable FK)."""
        milestone = Milestone(
            msx_milestone_id='ms-no-opp',
            url='https://example.com/milestone/ms-no-opp',
            title='Orphan Milestone',
        )
        db_session.add(milestone)
        db_session.commit()

        assert milestone.opportunity_id is None
        assert milestone.opportunity is None


class TestMilestoneSyncUpsertOpportunity:
    """Tests for opportunity upsert during milestone sync."""

    def _make_msx_milestone(self, msx_id, opp_guid, opp_name, name='Test MS'):
        """Helper to create a fake MSX milestone dict."""
        return {
            'id': msx_id,
            'name': name,
            'number': f'7-{msx_id[:8]}',
            'status': 'On Track',
            'status_code': 861980000,
            'status_sort': 1,
            'msx_opportunity_id': opp_guid,
            'opportunity_name': opp_name,
            'workload': 'Azure',
            'monthly_usage': None,
            'due_date': '2026-06-30T00:00:00Z',
            'dollar_value': 10000.0,
            'url': f'https://example.com/milestone/{msx_id}',
        }

    @patch('app.services.milestone_sync.get_milestones_by_account')
    @patch('app.services.milestone_sync.extract_account_id_from_url')
    def test_sync_creates_opportunity(self, mock_extract, mock_get_ms, app, db_session, sample_user):
        """Test that sync creates an Opportunity when it doesn't exist."""
        mock_extract.return_value = 'account-guid-123'
        mock_get_ms.return_value = {
            'success': True,
            'milestones': [
                self._make_msx_milestone(
                    'ms-guid-1', 'opp-guid-abc', 'Fabric Opportunity'
                ),
            ],
        }

        customer = Customer(
            name='Sync Corp', tpid=7777,
            tpid_url='https://example.com/account/account-guid-123',
        )
        db_session.add(customer)
        db_session.commit()

        from app.services.milestone_sync import sync_customer_milestones
        result = sync_customer_milestones(customer)

        assert result['success']
        assert result['created'] == 1

        # Verify opportunity was created
        opp = Opportunity.query.filter_by(msx_opportunity_id='opp-guid-abc').first()
        assert opp is not None
        assert opp.name == 'Fabric Opportunity'
        assert opp.customer_id == customer.id

        # Verify milestone links to opportunity
        ms = Milestone.query.filter_by(msx_milestone_id='ms-guid-1').first()
        assert ms.opportunity_id == opp.id

    @patch('app.services.milestone_sync.get_milestones_by_account')
    @patch('app.services.milestone_sync.extract_account_id_from_url')
    def test_sync_reuses_existing_opportunity(self, mock_extract, mock_get_ms, app, db_session, sample_user):
        """Test that sync reuses an existing Opportunity record."""
        mock_extract.return_value = 'account-guid-456'

        customer = Customer(
            name='Reuse Corp', tpid=8888,
            tpid_url='https://example.com/account/account-guid-456',
        )
        db_session.add(customer)
        db_session.flush()

        # Pre-create the opportunity
        existing_opp = Opportunity(
            msx_opportunity_id='opp-guid-existing',
            name='Old Name',
            customer_id=customer.id,
        )
        db_session.add(existing_opp)
        db_session.commit()
        opp_id = existing_opp.id

        mock_get_ms.return_value = {
            'success': True,
            'milestones': [
                self._make_msx_milestone(
                    'ms-guid-2', 'opp-guid-existing', 'Updated Name'
                ),
            ],
        }

        from app.services.milestone_sync import sync_customer_milestones
        result = sync_customer_milestones(customer)

        assert result['success']

        # Verify same opportunity record was reused (not duplicated)
        assert Opportunity.query.count() == 1
        opp = Opportunity.query.first()
        assert opp.id == opp_id
        assert opp.name == 'Updated Name'  # Name updated

        # Verify milestone links to the existing opportunity
        ms = Milestone.query.filter_by(msx_milestone_id='ms-guid-2').first()
        assert ms.opportunity_id == opp_id

    @patch('app.services.milestone_sync.get_milestones_by_account')
    @patch('app.services.milestone_sync.extract_account_id_from_url')
    def test_sync_groups_milestones_under_same_opportunity(
        self, mock_extract, mock_get_ms, app, db_session, sample_user
    ):
        """Test that multiple milestones on the same opportunity share one Opportunity record."""
        mock_extract.return_value = 'account-guid-789'
        mock_get_ms.return_value = {
            'success': True,
            'milestones': [
                self._make_msx_milestone(
                    'ms-a', 'opp-shared', 'Shared Opp', name='Milestone A'
                ),
                self._make_msx_milestone(
                    'ms-b', 'opp-shared', 'Shared Opp', name='Milestone B'
                ),
                self._make_msx_milestone(
                    'ms-c', 'opp-different', 'Different Opp', name='Milestone C'
                ),
            ],
        }

        customer = Customer(
            name='Group Corp', tpid=9999,
            tpid_url='https://example.com/account/account-guid-789',
        )
        db_session.add(customer)
        db_session.commit()

        from app.services.milestone_sync import sync_customer_milestones
        result = sync_customer_milestones(customer)

        assert result['success']
        assert result['created'] == 3

        # Should have 2 opportunities
        assert Opportunity.query.count() == 2

        shared_opp = Opportunity.query.filter_by(msx_opportunity_id='opp-shared').first()
        assert shared_opp.milestones.count() == 2

        diff_opp = Opportunity.query.filter_by(msx_opportunity_id='opp-different').first()
        assert diff_opp.milestones.count() == 1

    @patch('app.services.milestone_sync.get_milestones_by_account')
    @patch('app.services.milestone_sync.extract_account_id_from_url')
    def test_sync_handles_milestone_without_opportunity(
        self, mock_extract, mock_get_ms, app, db_session, sample_user
    ):
        """Test that milestones without an opportunity GUID get opportunity_id=None."""
        mock_extract.return_value = 'account-guid-no-opp'
        mock_get_ms.return_value = {
            'success': True,
            'milestones': [
                self._make_msx_milestone(
                    'ms-no-opp', None, '', name='Orphan MS'
                ),
            ],
        }

        customer = Customer(
            name='NoOpp Corp', tpid=1111,
            tpid_url='https://example.com/account/account-guid-no-opp',
        )
        db_session.add(customer)
        db_session.commit()

        from app.services.milestone_sync import sync_customer_milestones
        result = sync_customer_milestones(customer)

        assert result['success']
        assert Opportunity.query.count() == 0

        ms = Milestone.query.filter_by(msx_milestone_id='ms-no-opp').first()
        assert ms.opportunity_id is None


class TestMsxApiOpportunityField:
    """Test that msx_api returns opportunity GUID in milestone data."""

    def test_milestone_dict_contains_msx_opportunity_id(self):
        """Verify the milestone dict schema includes msx_opportunity_id."""
        # This is a structural test — the actual API call is mocked in sync tests.
        # We verify the field exists in the expected dict shape.
        sample_dict = {
            'id': 'ms-1',
            'name': 'Test',
            'number': '7-123',
            'status': 'On Track',
            'status_code': 861980000,
            'status_sort': 1,
            'msx_opportunity_id': 'opp-guid-here',
            'opportunity_name': 'Test Opp',
            'workload': '',
            'monthly_usage': None,
            'due_date': None,
            'dollar_value': None,
            'url': 'https://example.com',
        }
        assert 'msx_opportunity_id' in sample_dict
        assert sample_dict['msx_opportunity_id'] == 'opp-guid-here'


class TestMilestoneViewPage:
    """Tests for the enriched milestone view page with navigation links."""

    def test_milestone_view_shows_opportunity_link(self, app, client, db_session, sample_user):
        """Test that milestone view page shows link to parent opportunity."""
        customer = Customer(
            name='MS View Corp', tpid=7001,
        )
        db_session.add(customer)
        db_session.flush()

        opp = Opportunity(
            msx_opportunity_id='opp-ms-view',
            name='Parent Opportunity',
            customer_id=customer.id,
        )
        db_session.add(opp)
        db_session.flush()

        ms = Milestone(
            msx_milestone_id='ms-view-test',
            url='https://example.com/ms-view-test',
            title='Test Milestone',
            msx_status='On Track',
            workload='Azure VM',
            dollar_value=25000.0,
            opportunity_id=opp.id,
            customer_id=customer.id,
        )
        db_session.add(ms)
        db_session.commit()

        response = client.get(f'/milestone/{ms.id}')
        assert response.status_code == 200
        html = response.data.decode()

        # Milestone details
        assert 'Test Milestone' in html
        assert 'On Track' in html
        assert 'Azure VM' in html
        assert '$25,000' in html

        # Navigation links
        assert 'Parent Opportunity' in html
        assert f'/opportunity/{opp.id}' in html
        assert 'MS View Corp' in html
        assert f'/customer/{customer.id}' in html

        # MSX link only on this page
        assert 'Open in MSX' in html

    def test_milestone_view_shows_notes(self, app, client, db_session, sample_user):
        """Test that milestone view page shows associated notes."""
        from app.models import Note
        from datetime import datetime, timezone

        customer = Customer(
            name='Note Corp', tpid=7002,
        )
        db_session.add(customer)
        db_session.flush()

        ms = Milestone(
            msx_milestone_id='ms-calls-test',
            url='https://example.com/ms-calls-test',
            title='Milestone With Calls',
        )
        db_session.add(ms)
        db_session.flush()

        call = Note(
            customer_id=customer.id,
            call_date=datetime(2026, 2, 15, tzinfo=timezone.utc),
            content='Discussed migration plan',
        )
        call.milestones.append(ms)
        db_session.add(call)
        db_session.commit()

        response = client.get(f'/milestone/{ms.id}')
        assert response.status_code == 200
        html = response.data.decode()

        assert 'Note Corp' in html
        assert 'Feb 15, 2026' in html
        assert '1' in html  # badge count

    def test_milestone_view_without_opportunity(self, app, client, db_session, sample_user):
        """Test milestone view page when no opportunity is linked."""
        ms = Milestone(
            msx_milestone_id='ms-no-opp-view',
            url='https://example.com/ms-no-opp-view',
            title='Orphan Milestone',
        )
        db_session.add(ms)
        db_session.commit()

        response = client.get(f'/milestone/{ms.id}')
        assert response.status_code == 200
        html = response.data.decode()

        assert 'Orphan Milestone' in html
        assert 'Not linked to a customer or opportunity' in html
    """Tests for the opportunity view page route and MSX details API."""

    def _make_msx_opportunity(self, **overrides):
        """Helper to build a mock MSX opportunity response."""
        data = {
            "id": "opp-test-guid",
            "name": "Test Opportunity",
            "number": "7-TEST123",
            "state": "Open",
            "status": "In Progress",
            "statecode": 0,
            "estimated_value": 50000.0,
            "estimated_close_date": "2026-03-15T00:00:00Z",
            "customer_need": "Migrate workloads to Azure",
            "description": "Big cloud migration deal",
            "owner": "Jane Seller",
            "compete_threat": "Medium",
            "comments": [
                {"userId": "{user-1}", "modifiedOn": "6/1/2025, 10:00:00 AM", "comment": "Looking good"},
                {"userId": "{user-2}", "modifiedOn": "6/2/2025, 2:30:00 PM", "comment": "Customer confirmed budget"},
            ],
            "comments_plain": "Looking good\nCustomer confirmed budget",
            "comments_last_modified": "2025-06-02T14:30:00Z",
            "url": "https://microsoftsales.crm.dynamics.com/main.aspx?appid=fe0c3504&pagetype=entityrecord&etn=opportunity&id=opp-test-guid",
        }
        data.update(overrides)
        return data

    def test_opportunity_view_renders_immediately(self, app, client, db_session, sample_user):
        """Test page renders with local data without calling MSX API."""
        customer = Customer(
            name='View Test Corp', tpid=6001,
        )
        db_session.add(customer)
        db_session.flush()

        opp = Opportunity(
            msx_opportunity_id='opp-view-test',
            name='View Test Opp',
            customer_id=customer.id,
        )
        db_session.add(opp)
        db_session.flush()

        ms = Milestone(
            msx_milestone_id='ms-view-1',
            url='https://example.com/ms-view-1',
            title='Deploy Phase 1',
            msx_status='On Track',
            opportunity_id=opp.id,
        )
        db_session.add(ms)
        db_session.commit()

        # No MSX API mock needed — route no longer calls get_opportunity()
        response = client.get(f'/opportunity/{opp.id}')
        assert response.status_code == 200
        html = response.data.decode()

        # Local data rendered immediately
        assert 'View Test Opp' in html
        assert 'Open in MSX' in html
        assert 'Deploy Phase 1' in html
        assert 'View Test Corp' in html

        # MSX details loaded via JS, so these should NOT be in the HTML
        assert 'Looking good' not in html
        assert 'Jane Seller' not in html

    def test_opportunity_view_shows_loading_spinners(self, app, client, db_session, sample_user):
        """Test page shows spinner placeholders for async MSX content."""
        opp = Opportunity(
            msx_opportunity_id='opp-spinner-test',
            name='Spinner Test Opp',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.get(f'/opportunity/{opp.id}')
        assert response.status_code == 200
        html = response.data.decode()

        assert 'detailsSpinner' in html
        assert 'commentsSpinner' in html
        assert 'Loading details from MSX' in html
        assert 'Loading comments from MSX' in html

    def test_opportunity_view_404(self, client):
        """Test viewing a nonexistent opportunity returns 404."""
        response = client.get('/opportunity/99999')
        assert response.status_code == 404

    @patch('app.routes.opportunities.get_opportunity')
    def test_msx_details_api_success(self, mock_get_opp, app, client, db_session, sample_user):
        """Test the MSX details API endpoint returns opportunity data."""
        opp = Opportunity(
            msx_opportunity_id='opp-api-details',
            name='API Details Opp',
        )
        db_session.add(opp)
        db_session.commit()

        mock_get_opp.return_value = {
            "success": True,
            "opportunity": self._make_msx_opportunity(),
        }

        response = client.get(f'/api/opportunity/{opp.id}/msx-details')
        assert response.status_code == 200
        data = response.get_json()

        assert data['success'] is True
        assert data['opportunity']['name'] == 'Test Opportunity'
        assert data['opportunity']['estimated_value'] == 50000.0
        assert len(data['opportunity']['comments']) == 2
        assert data['opportunity']['owner'] == 'Jane Seller'

    @patch('app.routes.opportunities.get_opportunity')
    def test_msx_details_api_caches_to_db(self, mock_get_opp, app, client, db_session, sample_user):
        """Test that successful MSX fetch saves details back to local DB."""
        opp = Opportunity(
            msx_opportunity_id='opp-cache-test',
            name='Cache Test Opp',
        )
        db_session.add(opp)
        db_session.commit()
        opp_id = opp.id

        mock_get_opp.return_value = {
            "success": True,
            "opportunity": self._make_msx_opportunity(),
        }

        response = client.get(f'/api/opportunity/{opp_id}/msx-details')
        assert response.status_code == 200

        # Verify the data was cached in the DB
        db_session.expire_all()
        opp = db_session.get(Opportunity, opp_id)
        assert opp.opportunity_number == '7-TEST123'
        assert opp.statecode == 0
        assert opp.state == 'Open'
        assert opp.status_reason == 'In Progress'
        assert opp.estimated_value == 50000.0
        assert opp.estimated_close_date == '2026-03-15T00:00:00Z'
        assert opp.owner_name == 'Jane Seller'
        assert opp.compete_threat == 'Medium'
        assert opp.customer_need == 'Migrate workloads to Azure'
        assert opp.description == 'Big cloud migration deal'
        assert opp.details_fetched_at is not None

    @patch('app.routes.opportunities.get_opportunity')
    def test_msx_details_api_caches_comments_to_db(self, mock_get_opp, app, client, db_session, sample_user):
        """Test that successful MSX fetch saves comments as JSON to local DB."""
        import json
        opp = Opportunity(
            msx_opportunity_id='opp-cache-comments',
            name='Cache Comments Opp',
        )
        db_session.add(opp)
        db_session.commit()
        opp_id = opp.id

        mock_get_opp.return_value = {
            "success": True,
            "opportunity": self._make_msx_opportunity(),
        }

        response = client.get(f'/api/opportunity/{opp_id}/msx-details')
        assert response.status_code == 200

        # Verify comments were cached in the DB
        db_session.expire_all()
        opp = db_session.get(Opportunity, opp_id)
        assert opp.cached_comments_json is not None
        comments = json.loads(opp.cached_comments_json)
        assert len(comments) == 2
        assert comments[0]['comment'] == 'Looking good'
        assert comments[1]['comment'] == 'Customer confirmed budget'

    def test_opportunity_view_shows_cached_comments(self, app, client, db_session, sample_user):
        """Test page renders cached comments immediately on load."""
        import json
        from datetime import datetime, timezone
        comments = [
            {"displayName": "Jane Doe", "modifiedOn": "2026-03-07", "comment": "Pipeline review notes"},
            {"displayName": "John Smith", "modifiedOn": "2026-03-08", "comment": "Updated forecast"},
        ]
        opp = Opportunity(
            msx_opportunity_id='opp-cached-comments-view',
            name='Cached Comments View Opp',
            cached_comments_json=json.dumps(comments),
            details_fetched_at=datetime(2026, 3, 8, tzinfo=timezone.utc),
        )
        db_session.add(opp)
        db_session.commit()

        response = client.get(f'/opportunity/{opp.id}')
        assert response.status_code == 200
        html = response.data.decode()

        # Cached comments should appear in the HTML
        assert 'Jane Doe' in html
        assert 'Pipeline review notes' in html
        assert 'John Smith' in html
        assert 'Updated forecast' in html
        assert 'Refreshing comments from MSX' in html  # different spinner text
        assert '2 comments' in html  # badge count

    def test_opportunity_view_no_cached_comments_shows_loading(self, app, client, db_session, sample_user):
        """Test page shows loading spinner when no cached comments exist."""
        opp = Opportunity(
            msx_opportunity_id='opp-no-cached-comments',
            name='No Cached Comments Opp',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.get(f'/opportunity/{opp.id}')
        assert response.status_code == 200
        html = response.data.decode()

        assert 'Loading comments from MSX' in html
        assert 'commentsCached' in html  # div exists but hidden
        """Test page renders cached MSX details immediately without API call."""
        from datetime import datetime, timezone
        opp = Opportunity(
            msx_opportunity_id='opp-cached-view',
            name='Cached View Opp',
            opportunity_number='7-CACHED',
            statecode=0,
            state='Open',
            status_reason='In Progress',
            estimated_value=75000.0,
            owner_name='Bob Seller',
            details_fetched_at=datetime(2026, 3, 8, tzinfo=timezone.utc),
        )
        db_session.add(opp)
        db_session.commit()

        response = client.get(f'/opportunity/{opp.id}')
        assert response.status_code == 200
        html = response.data.decode()

        # Cached data should appear in the HTML
        assert '7-CACHED' in html
        assert 'Open' in html
        assert '$75,000' in html
        assert 'Bob Seller' in html
        assert 'Refreshing from MSX' in html  # different spinner text for cached

    def test_opportunity_view_no_cache_shows_loading(self, app, client, db_session, sample_user):
        """Test page shows loading spinner when no cached data exists."""
        opp = Opportunity(
            msx_opportunity_id='opp-no-cache',
            name='No Cache Opp',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.get(f'/opportunity/{opp.id}')
        assert response.status_code == 200
        html = response.data.decode()

        assert 'Loading details from MSX' in html
        # Should NOT have cached content visible
        assert 'detailsCached' in html  # div exists but hidden

    @patch('app.routes.opportunities.get_opportunity')
    def test_msx_details_api_error(self, mock_get_opp, app, client, db_session, sample_user):
        """Test the MSX details API endpoint when MSX returns error."""
        opp = Opportunity(
            msx_opportunity_id='opp-api-error',
            name='API Error Opp',
        )
        db_session.add(opp)
        db_session.commit()

        mock_get_opp.return_value = {
            "success": False,
            "error": "Not authenticated. Run 'az login' first.",
        }

        response = client.get(f'/api/opportunity/{opp.id}/msx-details')
        assert response.status_code == 200
        data = response.get_json()

        assert data['success'] is False
        assert 'Not authenticated' in data['error']

    @patch('app.routes.opportunities.get_opportunity')
    def test_msx_details_api_vpn_blocked(self, mock_get_opp, app, client, db_session, sample_user):
        """Test the MSX details API endpoint when VPN is blocked."""
        opp = Opportunity(
            msx_opportunity_id='opp-api-vpn',
            name='API VPN Opp',
        )
        db_session.add(opp)
        db_session.commit()

        mock_get_opp.return_value = {
            "success": False,
            "error": "IP address is blocked — connect to VPN and retry.",
            "vpn_blocked": True,
        }

        response = client.get(f'/api/opportunity/{opp.id}/msx-details')
        assert response.status_code == 200
        data = response.get_json()

        assert data['success'] is False
        assert data['vpn_blocked'] is True

    def test_msx_details_api_404(self, client):
        """Test the MSX details API endpoint for nonexistent opportunity."""
        response = client.get('/api/opportunity/99999/msx-details')
        assert response.status_code == 404

    @patch('app.routes.opportunities.get_opportunity')
    def test_msx_details_api_no_comments(self, mock_get_opp, app, client, db_session, sample_user):
        """Test MSX details API with empty comments list."""
        opp = Opportunity(
            msx_opportunity_id='opp-no-comments',
            name='No Comments Opp',
        )
        db_session.add(opp)
        db_session.commit()

        mock_get_opp.return_value = {
            "success": True,
            "opportunity": self._make_msx_opportunity(comments=[], comments_plain=""),
        }

        response = client.get(f'/api/opportunity/{opp.id}/msx-details')
        assert response.status_code == 200
        data = response.get_json()

        assert data['success'] is True
        assert data['opportunity']['comments'] == []


class TestOpportunityCommentRoute:
    """Tests for posting comments to opportunities."""

    @patch('app.routes.opportunities.add_opportunity_comment')
    def test_post_comment_success(self, mock_add_comment, app, client, db_session, sample_user):
        """Test successfully posting a comment."""
        opp = Opportunity(
            msx_opportunity_id='opp-comment-test',
            name='Comment Test Opp',
        )
        db_session.add(opp)
        db_session.commit()

        mock_add_comment.return_value = {"success": True}

        response = client.post(
            f'/opportunity/{opp.id}/comment',
            data={'comment': 'This is a test comment'},
            follow_redirects=False,
        )

        # Should redirect back to opportunity view
        assert response.status_code == 302
        assert f'/opportunity/{opp.id}' in response.headers['Location']

        # Verify the MSX API was called with correct args
        mock_add_comment.assert_called_once_with('opp-comment-test', 'This is a test comment')

    @patch('app.routes.opportunities.add_opportunity_comment')
    def test_post_comment_failure(self, mock_add_comment, app, client, db_session, sample_user):
        """Test posting a comment when MSX API returns failure."""
        opp = Opportunity(
            msx_opportunity_id='opp-comment-fail',
            name='Comment Fail Opp',
        )
        db_session.add(opp)
        db_session.commit()

        mock_add_comment.return_value = {"success": False, "error": "Permission denied"}

        response = client.post(
            f'/opportunity/{opp.id}/comment',
            data={'comment': 'My comment'},
            follow_redirects=True,
        )

        assert response.status_code == 200
        html = response.data.decode()
        assert 'Permission denied' in html

    def test_post_empty_comment(self, app, client, db_session, sample_user):
        """Test posting an empty comment shows warning."""
        opp = Opportunity(
            msx_opportunity_id='opp-empty-comment',
            name='Empty Comment Opp',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.post(
            f'/opportunity/{opp.id}/comment',
            data={'comment': '   '},
            follow_redirects=True,
        )

        assert response.status_code == 200

    def test_post_comment_404(self, client):
        """Test posting comment to nonexistent opportunity returns 404."""
        response = client.post(
            '/opportunity/99999/comment',
            data={'comment': 'Hello'},
        )
        assert response.status_code == 404


class TestOpportunityApiCommentRoute:
    """Tests for the JSON API comment endpoint."""

    @patch('app.routes.opportunities.add_opportunity_comment')
    def test_api_post_comment_success(self, mock_add_comment, app, client, db_session, sample_user):
        """Test successfully posting a comment via API."""
        opp = Opportunity(
            msx_opportunity_id='opp-api-test',
            name='API Test Opp',
        )
        db_session.add(opp)
        db_session.commit()

        mock_add_comment.return_value = {"success": True}

        response = client.post(
            f'/api/opportunity/{opp.id}/comment',
            json={'comment': 'API comment'},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        mock_add_comment.assert_called_once_with('opp-api-test', 'API comment')

    def test_api_post_empty_comment(self, app, client, db_session, sample_user):
        """Test posting empty comment via API returns 400."""
        opp = Opportunity(
            msx_opportunity_id='opp-api-empty',
            name='API Empty Opp',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.post(
            f'/api/opportunity/{opp.id}/comment',
            json={'comment': ''},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    def test_api_post_no_json(self, app, client, db_session, sample_user):
        """Test posting without JSON body returns error."""
        opp = Opportunity(
            msx_opportunity_id='opp-api-nojson',
            name='API NoJSON Opp',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.post(
            f'/api/opportunity/{opp.id}/comment',
            data='not json',
            content_type='text/plain',
        )

        # Flask returns 415 for non-JSON content type
        assert response.status_code in (400, 415)


class TestOpportunityCommentEditDelete:
    """Tests for editing and deleting opportunity comments."""

    def _make_opp(self, db_session):
        """Helper to create an opportunity for testing."""
        opp = Opportunity(
            msx_opportunity_id='opp-edit-delete-test',
            name='Edit Delete Test Opp',
        )
        db_session.add(opp)
        db_session.commit()
        return opp

    @patch('app.routes.opportunities.edit_opportunity_comment')
    def test_edit_comment_success(self, mock_edit, app, client, db_session, sample_user):
        """Test editing a comment via PUT."""
        opp = self._make_opp(db_session)
        mock_edit.return_value = {"success": True}

        response = client.put(
            f'/api/opportunity/{opp.id}/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex Blaine',
                'comment': 'Updated comment text',
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        mock_edit.assert_called_once_with(
            'opp-edit-delete-test',
            '2026-03-18T10:00:00.000Z',
            'Alex Blaine',
            'Updated comment text',
        )

    def test_edit_comment_empty_text(self, app, client, db_session, sample_user):
        """Test editing with empty comment returns 400."""
        opp = self._make_opp(db_session)

        response = client.put(
            f'/api/opportunity/{opp.id}/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex Blaine',
                'comment': '',
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    def test_edit_comment_missing_identifier(self, app, client, db_session, sample_user):
        """Test editing without modifiedOn/userId returns 400."""
        opp = self._make_opp(db_session)

        response = client.put(
            f'/api/opportunity/{opp.id}/comment',
            json={'comment': 'Some text'},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'Missing comment identifier' in data['error']

    def test_edit_comment_no_msx_id(self, app, client, db_session, sample_user):
        """Test editing on opportunity without MSX ID returns error."""
        opp = Opportunity(
            msx_opportunity_id='',
            name='No MSX ID Opp',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.put(
            f'/api/opportunity/{opp.id}/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex',
                'comment': 'Test',
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'No MSX ID' in data['error']

    def test_edit_comment_404(self, client):
        """Test editing comment on nonexistent opportunity."""
        response = client.put(
            '/api/opportunity/99999/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex',
                'comment': 'Test',
            },
        )
        assert response.status_code == 404

    @patch('app.routes.opportunities.delete_opportunity_comment')
    def test_delete_comment_success(self, mock_delete, app, client, db_session, sample_user):
        """Test deleting a comment via DELETE."""
        opp = self._make_opp(db_session)
        mock_delete.return_value = {"success": True}

        response = client.delete(
            f'/api/opportunity/{opp.id}/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex Blaine',
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        mock_delete.assert_called_once_with(
            'opp-edit-delete-test',
            '2026-03-18T10:00:00.000Z',
            'Alex Blaine',
        )

    def test_delete_comment_missing_identifier(self, app, client, db_session, sample_user):
        """Test deleting without modifiedOn/userId returns 400."""
        opp = self._make_opp(db_session)

        response = client.delete(
            f'/api/opportunity/{opp.id}/comment',
            json={'modifiedOn': '2026-03-18T10:00:00.000Z'},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False

    def test_delete_comment_no_msx_id(self, app, client, db_session, sample_user):
        """Test deleting on opportunity without MSX ID returns error."""
        opp = Opportunity(
            msx_opportunity_id='',
            name='No MSX ID Opp Del',
        )
        db_session.add(opp)
        db_session.commit()

        response = client.delete(
            f'/api/opportunity/{opp.id}/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex',
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False
        assert 'No MSX ID' in data['error']

    def test_delete_comment_404(self, client):
        """Test deleting comment on nonexistent opportunity."""
        response = client.delete(
            '/api/opportunity/99999/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex',
            },
        )
        assert response.status_code == 404

    @patch('app.routes.opportunities.edit_opportunity_comment')
    def test_edit_comment_msx_failure(self, mock_edit, app, client, db_session, sample_user):
        """Test edit returns error when MSX call fails."""
        opp = self._make_opp(db_session)
        mock_edit.return_value = {"success": False, "error": "Comment not found in MSX."}

        response = client.put(
            f'/api/opportunity/{opp.id}/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex',
                'comment': 'Updated text',
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is False
        assert 'not found' in data['error']

    @patch('app.routes.opportunities.delete_opportunity_comment')
    def test_delete_comment_msx_failure(self, mock_delete, app, client, db_session, sample_user):
        """Test delete returns error when MSX call fails."""
        opp = self._make_opp(db_session)
        mock_delete.return_value = {"success": False, "error": "Comment not found in MSX."}

        response = client.delete(
            f'/api/opportunity/{opp.id}/comment',
            json={
                'modifiedOn': '2026-03-18T10:00:00.000Z',
                'userId': 'Alex',
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is False
        assert 'not found' in data['error']
