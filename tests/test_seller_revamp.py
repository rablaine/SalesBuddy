"""
Tests for seller page revamp — milestones card, compact customers, engagements API.
"""
import pytest
from datetime import datetime, timezone, timedelta


class TestSellerViewRevamp:
    """Test the revamped seller view page."""

    def test_seller_view_loads_with_milestones_card(self, app, client, sample_data):
        """Seller page shows milestone card when seller has active milestones."""
        with app.app_context():
            from app.models import db, Customer, Milestone

            customer = db.session.get(Customer, sample_data['customer1_id'])
            ms = Milestone(
                title='Deploy AKS cluster',
                url='https://msx.example.com/ms1',
                msx_status='On Track',
                customer_id=customer.id,
                due_date=datetime.now(timezone.utc) + timedelta(days=14),
                monthly_usage=5000.0,
                workload='Infra: Kubernetes',
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        assert response.status_code == 200
        assert b'Milestones' in response.data
        assert b'Deploy AKS cluster' in response.data
        assert b'On Track' in response.data
        assert b'Full View' in response.data

    def test_seller_view_no_milestones_card_when_empty(self, client, sample_data):
        """Milestone card is hidden when the seller has no active milestones."""
        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        assert response.status_code == 200
        # The milestone card header should NOT appear
        assert b'milestone-tracker?seller=' not in response.data

    def test_seller_view_milestone_link_includes_seller_filter(self, app, client, sample_data):
        """Full View link on milestones card links to milestone tracker with seller filter."""
        with app.app_context():
            from app.models import db, Customer, Milestone

            customer = db.session.get(Customer, sample_data['customer1_id'])
            ms = Milestone(
                title='Test milestone',
                url='https://msx.example.com/ms1',
                msx_status='At Risk',
                customer_id=customer.id,
                monthly_usage=1000.0,
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        assert response.status_code == 200
        # Check the link points to milestone tracker with seller param
        expected_link = f'milestone-tracker?seller={sample_data["seller1_id"]}'
        assert expected_link.encode() in response.data

    def test_seller_view_milestones_sorted_by_due_date(self, app, client, sample_data):
        """Milestones are sorted by due date ascending (closest to due first, nulls last)."""
        with app.app_context():
            from app.models import db, Customer, Milestone
            from datetime import date, timedelta

            customer = db.session.get(Customer, sample_data['customer1_id'])
            today = date.today()
            ms_later = Milestone(
                title='Later milestone',
                url='https://msx.example.com/ms1',
                msx_status='On Track',
                customer_id=customer.id,
                monthly_usage=100.0,
                due_date=today + timedelta(days=30),
            )
            ms_sooner = Milestone(
                title='Sooner milestone',
                url='https://msx.example.com/ms2',
                msx_status='On Track',
                customer_id=customer.id,
                monthly_usage=50000.0,
                due_date=today + timedelta(days=5),
            )
            ms_no_date = Milestone(
                title='No date milestone',
                url='https://msx.example.com/ms3',
                msx_status='On Track',
                customer_id=customer.id,
                monthly_usage=25000.0,
            )
            db.session.add_all([ms_later, ms_sooner, ms_no_date])
            db.session.commit()

        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        html = response.data.decode()
        sooner_pos = html.index('Sooner milestone')
        later_pos = html.index('Later milestone')
        no_date_pos = html.index('No date milestone')
        assert sooner_pos < later_pos, 'Sooner due date should appear first'
        assert later_pos < no_date_pos, 'Milestones without due date should appear last'

    def test_seller_view_inactive_milestones_excluded(self, app, client, sample_data):
        """Cancelled/Completed milestones are not shown."""
        with app.app_context():
            from app.models import db, Customer, Milestone

            customer = db.session.get(Customer, sample_data['customer1_id'])
            ms = Milestone(
                title='Cancelled milestone',
                url='https://msx.example.com/ms1',
                msx_status='Cancelled',
                customer_id=customer.id,
                monthly_usage=999.0,
            )
            db.session.add(ms)
            db.session.commit()

        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        assert b'Cancelled milestone' not in response.data


class TestSellerCustomersCompact:
    """Test the compact scrollable customer list."""

    def test_customer_list_uses_note_terminology(self, client, sample_data):
        """Customer list says 'note' instead of 'call'."""
        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        html = response.data.decode()
        # Should use "note" terminology
        assert 'Last Note' in html
        assert 'No notes yet' in html or 'New Note' in html
        # Should NOT use old "call" terminology in the customer table context
        # (other parts of the page may legitimately reference calls)
        assert 'Most Recent Call' not in html

    def test_customer_list_is_scrollable(self, client, sample_data):
        """Customer list has max-height and overflow-y for scrolling."""
        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        html = response.data.decode()
        assert 'max-height' in html
        assert 'overflow-y: auto' in html

    def test_customer_list_shows_customers(self, client, sample_data):
        """Customer list still shows all customers."""
        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        assert response.status_code == 200
        assert b'Acme Corp' in response.data
        assert b'Initech LLC' in response.data


class TestSellerEngagementsAPI:
    """Test the seller-specific engagements API endpoint."""

    def test_api_returns_empty_for_no_engagements(self, client, sample_data):
        """API returns empty list when seller has no engagements."""
        response = client.get(f'/api/seller/{sample_data["seller1_id"]}/engagements')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['count'] == 0
        assert data['engagements'] == []

    def test_api_returns_engagements_for_seller(self, app, client, sample_data):
        """API returns active engagements for the seller's customers."""
        with app.app_context():
            from app.models import db, Engagement

            eng = Engagement(
                customer_id=sample_data['customer1_id'],
                title='AKS Migration',
                status='Active',
            )
            db.session.add(eng)
            db.session.commit()

        response = client.get(f'/api/seller/{sample_data["seller1_id"]}/engagements')
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['count'] == 1
        assert data['engagements'][0]['title'] == 'AKS Migration'
        assert data['engagements'][0]['customer_name'] == 'Acme Corp'

    def test_api_excludes_other_sellers_engagements(self, app, client, sample_data):
        """API does not return engagements from other sellers' customers."""
        with app.app_context():
            from app.models import db, Engagement

            # Create engagement for seller2's customer
            eng = Engagement(
                customer_id=sample_data['customer2_id'],
                title='Other Seller Engagement',
                status='Active',
            )
            db.session.add(eng)
            db.session.commit()

        # Query seller1's engagements — should be empty
        response = client.get(f'/api/seller/{sample_data["seller1_id"]}/engagements')
        data = response.get_json()
        assert data['count'] == 0

    def test_api_filters_by_status(self, app, client, sample_data):
        """API supports status filter parameter."""
        with app.app_context():
            from app.models import db, Engagement

            eng_active = Engagement(
                customer_id=sample_data['customer1_id'],
                title='Active Eng',
                status='Active',
            )
            eng_hold = Engagement(
                customer_id=sample_data['customer1_id'],
                title='On Hold Eng',
                status='On Hold',
            )
            db.session.add_all([eng_active, eng_hold])
            db.session.commit()

        # Filter active only
        response = client.get(f'/api/seller/{sample_data["seller1_id"]}/engagements?status=Active')
        data = response.get_json()
        assert data['count'] == 1
        assert data['engagements'][0]['title'] == 'Active Eng'

        # Filter on hold only
        response = client.get(f'/api/seller/{sample_data["seller1_id"]}/engagements?status=On Hold')
        data = response.get_json()
        assert data['count'] == 1
        assert data['engagements'][0]['title'] == 'On Hold Eng'

    def test_api_excludes_won_lost_engagements(self, app, client, sample_data):
        """API only returns Active and On Hold engagements."""
        with app.app_context():
            from app.models import db, Engagement

            eng = Engagement(
                customer_id=sample_data['customer1_id'],
                title='Won Engagement',
                status='Won',
            )
            db.session.add(eng)
            db.session.commit()

        response = client.get(f'/api/seller/{sample_data["seller1_id"]}/engagements')
        data = response.get_json()
        assert data['count'] == 0

    def test_api_returns_404_for_invalid_seller(self, client):
        """API returns 404 for non-existent seller."""
        response = client.get('/api/seller/99999/engagements')
        assert response.status_code == 404

    def test_api_engagement_includes_expected_fields(self, app, client, sample_data):
        """API returns all expected fields on each engagement."""
        with app.app_context():
            from app.models import db, Engagement
            from datetime import date

            eng = Engagement(
                customer_id=sample_data['customer1_id'],
                title='Full Fields Test',
                status='Active',
                estimated_acr=50000,
                target_date=date(2026, 6, 15),
                key_individuals='John Doe',
                technical_problem='Migration issues',
                business_impact='Revenue delay',
            )
            db.session.add(eng)
            db.session.commit()

        response = client.get(f'/api/seller/{sample_data["seller1_id"]}/engagements')
        data = response.get_json()
        eng_data = data['engagements'][0]

        assert 'id' in eng_data
        assert eng_data['title'] == 'Full Fields Test'
        assert eng_data['status'] == 'Active'
        assert eng_data['customer_name'] == 'Acme Corp'
        assert eng_data['customer_id'] == sample_data['customer1_id']
        assert eng_data['estimated_acr'] == 50000
        assert eng_data['target_date'] == '2026-06-15'
        assert eng_data['story_completeness'] == 83  # 5 of 6 fields filled
        assert 'linked_note_count' in eng_data
        assert 'opportunity_count' in eng_data
        assert 'milestone_count' in eng_data
        assert 'updated_at' in eng_data


class TestSellerEngagementsUI:
    """Test the engagements card renders on the seller page."""

    def test_engagements_card_present(self, client, sample_data):
        """Engagements card with filter controls is present on the page."""
        response = client.get(f'/seller/{sample_data["seller1_id"]}')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'Engagements' in html
        assert 'engagementsBody_statusFilter' in html
        assert 'engagementsBody_sortSelect' in html
        assert 'Loading engagements...' in html

    def test_engagements_js_uses_correct_api_url(self, client, sample_data):
        """JavaScript fetches from the seller-specific engagements API."""
        seller_id = sample_data['seller1_id']
        response = client.get(f'/seller/{seller_id}')
        html = response.data.decode()
        # API URL is passed directly as a JSON string via Jinja
        assert f'/api/seller/{seller_id}/engagements' in html
