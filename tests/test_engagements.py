"""
Tests for the Engagement system (Issues #24 and #39).

Tests cover:
- Engagement CRUD operations
- Engagement-note linking
- Engagement-opportunity/milestone linking
- Inline engagement creation
- Engagement display on customer view
- Account context rename (overview -> account_context)
"""
import json
from datetime import datetime, timezone, date

import pytest
from app.models import db, Customer, Seller, Territory, Note, Engagement, EngagementTask, Topic, Milestone


@pytest.fixture
def engagement_data(app):
    """Create sample data for engagement tests including customer, seller, notes."""
    with app.app_context():
        from app.models import User
        test_user = User.query.first()

        territory = Territory(name='Central')
        db.session.add(territory)
        db.session.flush()

        seller = Seller(name='Dana Lee', alias='danal', seller_type='Growth')
        db.session.add(seller)
        db.session.flush()
        seller.territories.append(territory)

        customer = Customer(
            name='Contoso Ltd',
            tpid=5001,
            seller_id=seller.id,
            territory_id=territory.id,
        )
        db.session.add(customer)
        db.session.flush()

        note1 = Note(
            customer_id=customer.id,
            call_date=datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc),
            content='Discussed migration timeline.',
        )
        note2 = Note(
            customer_id=customer.id,
            call_date=datetime(2025, 2, 10, 14, 0, tzinfo=timezone.utc),
            content='Reviewed security requirements.',
        )
        note3 = Note(
            customer_id=customer.id,
            call_date=datetime(2025, 3, 5, 9, 0, tzinfo=timezone.utc),
            content='General check-in call.',
        )
        db.session.add_all([note1, note2, note3])
        db.session.commit()

        return {
            'customer_id': customer.id,
            'seller_id': seller.id,
            'territory_id': territory.id,
            'note1_id': note1.id,
            'note2_id': note2.id,
            'note3_id': note3.id,
        }


class TestEngagementCRUD:
    """Test engagement create, read, update, delete operations."""

    def test_create_engagement(self, client, app, engagement_data):
        """Create a new engagement via form POST."""
        cid = engagement_data['customer_id']
        resp = client.post(
            f'/customer/{cid}/engagement/new',
            data={
                'title': 'Cloud Migration',
                'status': 'Active',
                'key_individuals': 'Jane Smith (CTO), Bob (Infra Lead)',
                'technical_problem': 'Legacy on-prem workloads need modernization',
                'business_impact': 'Reducing data center costs by 40%',
                'solution_resources': 'Azure Migrate, App Service',
                'estimated_acr': '50000',
                'target_date': '2025-06-30',
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = Engagement.query.filter_by(title='Cloud Migration').first()
            assert eng is not None
            assert eng.customer_id == cid
            assert eng.status == 'Active'
            assert eng.key_individuals == 'Jane Smith (CTO), Bob (Infra Lead)'
            assert eng.technical_problem == 'Legacy on-prem workloads need modernization'
            assert eng.business_impact == 'Reducing data center costs by 40%'
            assert eng.solution_resources == 'Azure Migrate, App Service'
            assert eng.estimated_acr == 50000
            assert eng.target_date == date(2025, 6, 30)
            assert eng.story_completeness == 100

    def test_create_engagement_minimal(self, client, app, engagement_data):
        """Create engagement with just title and status (minimal required fields)."""
        cid = engagement_data['customer_id']
        resp = client.post(
            f'/customer/{cid}/engagement/new',
            data={'title': 'Quick Thread', 'status': 'Active'},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = Engagement.query.filter_by(title='Quick Thread').first()
            assert eng is not None
            assert eng.story_completeness == 0
            assert eng.linked_note_count == 0

    def test_create_engagement_requires_title(self, client, app, engagement_data):
        """Attempting to create engagement without title should fail with flash."""
        cid = engagement_data['customer_id']
        resp = client.post(
            f'/customer/{cid}/engagement/new',
            data={'title': '', 'status': 'Active'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'Engagement title is required' in resp.data

    def test_view_engagement(self, client, app, engagement_data):
        """View an engagement detail page."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(
                customer_id=cid,
                title='View Test Engagement',
                status='Active',
                key_individuals='Alice',
            )
            db.session.add(eng)
            db.session.commit()
            eng_id = eng.id

        resp = client.get(f'/engagement/{eng_id}')
        assert resp.status_code == 200
        assert b'View Test Engagement' in resp.data
        assert b'Alice' in resp.data

    def test_edit_engagement(self, client, app, engagement_data):
        """Edit an existing engagement."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(
                customer_id=cid,
                title='Original Title',
                status='Active',
            )
            db.session.add(eng)
            db.session.commit()
            eng_id = eng.id

        resp = client.post(
            f'/engagement/{eng_id}/edit',
            data={
                'title': 'Updated Title',
                'status': 'On Hold',
                'technical_problem': 'New problem description',
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = db.session.get(Engagement, eng_id)
            assert eng.title == 'Updated Title'
            assert eng.status == 'On Hold'
            assert eng.technical_problem == 'New problem description'

    def test_delete_engagement(self, client, app, engagement_data):
        """Delete an engagement."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(
                customer_id=cid,
                title='To Be Deleted',
                status='Active',
            )
            db.session.add(eng)
            db.session.commit()
            eng_id = eng.id

        resp = client.post(
            f'/engagement/{eng_id}/delete',
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            assert db.session.get(Engagement, eng_id) is None

    def test_engagement_form_page_loads(self, client, engagement_data):
        """GET request for new engagement form should render."""
        cid = engagement_data['customer_id']
        resp = client.get(f'/customer/{cid}/engagement/new')
        assert resp.status_code == 200
        assert b'New Engagement' in resp.data or b'engagement' in resp.data.lower()

    def test_engagement_edit_form_loads(self, client, app, engagement_data):
        """GET request for edit form should render with existing data."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(
                customer_id=cid,
                title='Edit Form Test',
                status='Active',
                key_individuals='Test Person',
            )
            db.session.add(eng)
            db.session.commit()
            eng_id = eng.id

        resp = client.get(f'/engagement/{eng_id}/edit')
        assert resp.status_code == 200
        assert b'Edit Form Test' in resp.data
        assert b'Test Person' in resp.data


class TestEngagementNoteLinking:
    """Test linking notes to engagements."""

    def test_create_engagement_with_notes(self, client, app, engagement_data):
        """Create engagement with selected notes linked."""
        cid = engagement_data['customer_id']
        n1 = engagement_data['note1_id']
        n2 = engagement_data['note2_id']
        resp = client.post(
            f'/customer/{cid}/engagement/new',
            data={
                'title': 'Linked Engagement',
                'status': 'Active',
                'note_ids': [str(n1), str(n2)],
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = Engagement.query.filter_by(title='Linked Engagement').first()
            assert eng is not None
            assert eng.linked_note_count == 2
            note_ids = {n.id for n in eng.notes}
            assert n1 in note_ids
            assert n2 in note_ids

    def test_assign_notes_to_engagement(self, client, app, engagement_data):
        """Assign existing notes to an engagement via the assign endpoint."""
        cid = engagement_data['customer_id']
        n1 = engagement_data['note1_id']
        n3 = engagement_data['note3_id']

        with app.app_context():
            eng = Engagement(customer_id=cid, title='Assign Test', status='Active')
            db.session.add(eng)
            db.session.commit()
            eng_id = eng.id

        resp = client.post(
            f'/engagement/{eng_id}/assign-notes',
            data={'note_ids': [str(n1), str(n3)]},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = db.session.get(Engagement, eng_id)
            assert eng.linked_note_count == 2

    def test_note_form_engagement_select(self, client, app, engagement_data):
        """Note create form should show engagement selection when customer has engagements."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Form Select Engagement', status='Active')
            db.session.add(eng)
            db.session.commit()

        resp = client.get(f'/note/new?customer_id={cid}')
        assert resp.status_code == 200
        assert b'Form Select Engagement' in resp.data

    def test_create_note_with_engagement(self, client, app, engagement_data):
        """Creating a note should link it to selected engagements."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Note Link Eng', status='Active')
            db.session.add(eng)
            db.session.commit()
            eng_id = eng.id

        resp = client.post(
            '/note/new',
            data={
                'customer_id': str(cid),
                'call_date': '2025-04-01',
                'call_time': '10:00',
                'content': 'Test note with engagement link.',
                'engagement_ids': [str(eng_id)],
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            eng = db.session.get(Engagement, eng_id)
            assert eng.linked_note_count == 1
            assert eng.notes[0].content == 'Test note with engagement link.'


class TestInlineEngagementCreation:
    """Test inline engagement creation from note form."""

    def test_create_inline_engagement(self, client, app, engagement_data):
        """Create engagement inline via form POST."""
        cid = engagement_data['customer_id']
        resp = client.post(
            f'/customer/{cid}/engagement/create-inline',
            data={'title': 'Inline Created'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['title'] == 'Inline Created'
        assert 'id' in data

        with app.app_context():
            eng = Engagement.query.filter_by(title='Inline Created').first()
            assert eng is not None
            assert eng.customer_id == cid
            assert eng.status == 'Active'

    def test_inline_create_requires_title(self, client, app, engagement_data):
        """Inline creation without title should fail."""
        cid = engagement_data['customer_id']
        resp = client.post(
            f'/customer/{cid}/engagement/create-inline',
            data={'title': ''},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False


class TestCustomerViewEngagements:
    """Test engagement display on customer view page."""

    def test_customer_view_shows_engagements(self, client, app, engagement_data):
        """Customer view should show engagement cards."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(
                customer_id=cid,
                title='Visible Engagement',
                status='Active',
                key_individuals='Test Person',
            )
            db.session.add(eng)
            db.session.commit()

        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'Visible Engagement' in resp.data
        assert b'Active' in resp.data

    def test_customer_view_shows_metrics(self, client, app, engagement_data):
        """Customer view should show key essentials and engagement details."""
        cid = engagement_data['customer_id']
        with app.app_context():
            Engagement(customer_id=cid, title='Active 1', status='Active')
            Engagement(customer_id=cid, title='On Hold 1', status='On Hold')
            db.session.add_all([
                Engagement(customer_id=cid, title='Active 1', status='Active'),
                Engagement(customer_id=cid, title='On Hold 1', status='On Hold'),
            ])
            db.session.commit()

        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        # Should show essentials panel fields and engagements section
        assert b'Last Contact' in resp.data
        assert b'Engagements' in resp.data
        assert b'Active 1' in resp.data

    def test_customer_view_empty_engagements_cta(self, client, app, engagement_data):
        """Customer view without engagements should show create CTA."""
        cid = engagement_data['customer_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'Create First Engagement' in resp.data

    def test_customer_view_shows_new_engagement_button(self, client, engagement_data):
        """Customer view should have New Engagement button in header."""
        cid = engagement_data['customer_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'New Engagement' in resp.data

    def test_notes_show_engagement_badges(self, client, app, engagement_data):
        """Notes in customer view should show linked engagement badges."""
        cid = engagement_data['customer_id']
        n1 = engagement_data['note1_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Badge Test Eng', status='Active')
            db.session.add(eng)
            db.session.flush()
            note = db.session.get(Note, n1)
            note.engagements.append(eng)
            db.session.commit()

        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'Badge Test Eng' in resp.data


class TestEngagementAPI:
    """Test engagement API endpoints for inline creation flyouts."""

    def test_get_customer_engagements(self, client, app, engagement_data):
        """GET /api/customer/<id>/engagements returns JSON list."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng1 = Engagement(customer_id=cid, title='API Eng 1', status='Active')
            eng2 = Engagement(customer_id=cid, title='API Eng 2', status='Completed')
            db.session.add_all([eng1, eng2])
            db.session.commit()

        resp = client.get(f'/api/customer/{cid}/engagements')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        titles = {e['title'] for e in data}
        assert titles == {'API Eng 1', 'API Eng 2'}
        assert all('id' in e and 'status' in e for e in data)

    def test_get_customer_engagements_empty(self, client, engagement_data):
        """Returns empty list when customer has no engagements."""
        cid = engagement_data['customer_id']
        resp = client.get(f'/api/customer/{cid}/engagements')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_get_customer_engagements_invalid_customer(self, client):
        """Returns 404 for non-existent customer."""
        resp = client.get('/api/customer/99999/engagements')
        assert resp.status_code == 404

    def test_create_inline_engagement_json(self, client, app, engagement_data):
        """Create engagement via JSON body (flyout mode)."""
        cid = engagement_data['customer_id']
        resp = client.post(
            f'/customer/{cid}/engagement/create-inline',
            data=json.dumps({'title': 'JSON Created'}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['title'] == 'JSON Created'
        assert 'id' in data

        with app.app_context():
            eng = Engagement.query.filter_by(title='JSON Created').first()
            assert eng is not None
            assert eng.customer_id == cid


class TestAccountContext:
    """Test the overview -> account_context rename."""

    def test_update_account_context(self, client, app, engagement_data):
        """Update account context via POST."""
        cid = engagement_data['customer_id']
        resp = client.post(
            f'/customer/{cid}/overview',
            data={'account_context': 'Strategic account with cloud-first initiative.'},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['account_context'] == 'Strategic account with cloud-first initiative.'

        with app.app_context():
            customer = db.session.get(Customer, cid)
            assert customer.account_context == 'Strategic account with cloud-first initiative.'

    def test_customer_view_shows_account_context(self, client, app, engagement_data):
        """Customer view should display the account context card."""
        cid = engagement_data['customer_id']
        with app.app_context():
            customer = db.session.get(Customer, cid)
            customer.account_context = 'Important account notes here.'
            db.session.commit()

        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'Account Context' in resp.data
        assert b'Important account notes here.' in resp.data

    def test_customer_view_shows_empty_context_message(self, client, app, engagement_data):
        """Customer view with no account context should show placeholder message."""
        cid = engagement_data['customer_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'This space is yours' in resp.data


class TestStoryCompleteness:
    """Test engagement story completeness calculation."""

    def test_empty_story(self, app, engagement_data):
        """Engagement with no story fields should be 0%."""
        with app.app_context():
            eng = Engagement(
                customer_id=engagement_data['customer_id'],
                title='Empty Story',
                status='Active',
            )
            db.session.add(eng)
            db.session.commit()
            assert eng.story_completeness == 0

    def test_partial_story(self, app, engagement_data):
        """Engagement with some fields should show partial %."""
        with app.app_context():
            eng = Engagement(
                customer_id=engagement_data['customer_id'],
                title='Partial Story',
                status='Active',
                key_individuals='Alice',
                technical_problem='Need to migrate',
                target_date=date(2025, 12, 31),
            )
            db.session.add(eng)
            db.session.commit()
            assert eng.story_completeness == 50  # 3 of 6 fields

    def test_full_story(self, app, engagement_data):
        """Engagement with all fields should be 100%."""
        with app.app_context():
            eng = Engagement(
                customer_id=engagement_data['customer_id'],
                title='Full Story',
                status='Active',
                key_individuals='Alice',
                technical_problem='Legacy systems',
                business_impact='Cost reduction',
                solution_resources='Azure Migrate',
                estimated_acr=100000,
                target_date=date(2025, 12, 31),
            )
            db.session.add(eng)
            db.session.commit()
            assert eng.story_completeness == 100


class TestActiveEngagementsAPI:
    """Test the /api/engagements/active endpoint for the homepage tab."""

    def test_active_engagements_api_empty(self, client, app, engagement_data):
        """API returns empty list when no engagements exist."""
        resp = client.get('/api/engagements/active')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['count'] == 0
        assert data['engagements'] == []

    def test_active_engagements_api_returns_active(self, client, app, engagement_data):
        """API returns active engagements with all expected fields."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(
                customer_id=cid,
                title='Cloud Migration',
                status='Active',
                estimated_acr=50000,
                target_date=date(2026, 6, 30),
            )
            db.session.add(eng)
            db.session.commit()
            eng_id = eng.id

        resp = client.get('/api/engagements/active')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 1
        e = data['engagements'][0]
        assert e['id'] == eng_id
        assert e['title'] == 'Cloud Migration'
        assert e['status'] == 'Active'
        assert e['customer_name'] == 'Contoso Ltd'
        assert e['estimated_acr'] == 50000
        assert e['target_date'] == '2026-06-30'
        assert 'story_completeness' in e
        assert 'linked_note_count' in e
        assert 'opportunity_count' in e
        assert 'milestone_count' in e
        assert 'updated_at' in e
        assert 'seller_name' in e

    def test_active_engagements_excludes_won_lost(self, client, app, engagement_data):
        """API excludes Won and Lost engagements."""
        cid = engagement_data['customer_id']
        with app.app_context():
            db.session.add_all([
                Engagement(customer_id=cid, title='Active One', status='Active'),
                Engagement(customer_id=cid, title='On Hold One', status='On Hold'),
                Engagement(customer_id=cid, title='Won One', status='Won'),
                Engagement(customer_id=cid, title='Lost One', status='Lost'),
            ])
            db.session.commit()

        resp = client.get('/api/engagements/active')
        data = resp.get_json()
        assert data['count'] == 2
        titles = {e['title'] for e in data['engagements']}
        assert titles == {'Active One', 'On Hold One'}

    def test_active_engagements_filter_by_status(self, client, app, engagement_data):
        """API supports status query param filter."""
        cid = engagement_data['customer_id']
        with app.app_context():
            db.session.add_all([
                Engagement(customer_id=cid, title='Active One', status='Active'),
                Engagement(customer_id=cid, title='On Hold One', status='On Hold'),
            ])
            db.session.commit()

        resp = client.get('/api/engagements/active?status=Active')
        data = resp.get_json()
        assert data['count'] == 1
        assert data['engagements'][0]['title'] == 'Active One'

        resp = client.get('/api/engagements/active?status=On Hold')
        data = resp.get_json()
        assert data['count'] == 1
        assert data['engagements'][0]['title'] == 'On Hold One'

    def test_homepage_shows_engagements_tab(self, client, app, engagement_data):
        """Homepage renders the engagements tab when active engagements exist."""
        cid = engagement_data['customer_id']
        with app.app_context():
            db.session.add(Engagement(
                customer_id=cid, title='Active Eng', status='Active',
            ))
            db.session.commit()

        resp = client.get('/')
        assert resp.status_code == 200
        assert b'engagements-tab' in resp.data
        assert b'Engagements' in resp.data

    def test_homepage_hides_engagements_tab_when_none(self, client, app, engagement_data):
        """Homepage does NOT render the engagements tab when no active engagements."""
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'engagements-tab' not in resp.data


class TestEngagementsHub:
    """Test the engagements hub page and all-engagements API."""

    def test_hub_page_loads(self, client, engagement_data):
        """Hub page returns 200 and shows header."""
        resp = client.get('/engagements')
        assert resp.status_code == 200
        assert b'Engagements' in resp.data

    def test_hub_shows_stats(self, client, app, engagement_data):
        """Hub page shows summary stat cards with correct counts."""
        cid = engagement_data['customer_id']
        with app.app_context():
            db.session.add_all([
                Engagement(customer_id=cid, title='E1', status='Active'),
                Engagement(customer_id=cid, title='E2', status='Active'),
                Engagement(customer_id=cid, title='E3', status='On Hold'),
                Engagement(customer_id=cid, title='E4', status='Won'),
                Engagement(customer_id=cid, title='E5', status='Lost'),
            ])
            db.session.commit()

        resp = client.get('/engagements')
        assert resp.status_code == 200
        html = resp.data.decode()
        # Check stat numbers appear in the page
        assert 'Active' in html
        assert 'On Hold' in html
        assert 'Won' in html
        assert 'Lost' in html
        assert 'Story Health' in html

    def test_all_engagements_api_returns_all_statuses(self, client, app, engagement_data):
        """API returns engagements of all statuses."""
        cid = engagement_data['customer_id']
        with app.app_context():
            db.session.add_all([
                Engagement(customer_id=cid, title='Active', status='Active'),
                Engagement(customer_id=cid, title='Won', status='Won'),
                Engagement(customer_id=cid, title='Lost', status='Lost'),
            ])
            db.session.commit()

        resp = client.get('/api/engagements/all')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['count'] == 3
        statuses = {e['status'] for e in data['engagements']}
        assert statuses == {'Active', 'Won', 'Lost'}

    def test_all_engagements_api_filter_by_status(self, client, app, engagement_data):
        """API supports filtering by status query param."""
        cid = engagement_data['customer_id']
        with app.app_context():
            db.session.add_all([
                Engagement(customer_id=cid, title='A1', status='Active'),
                Engagement(customer_id=cid, title='W1', status='Won'),
            ])
            db.session.commit()

        resp = client.get('/api/engagements/all?status=Won')
        data = resp.get_json()
        assert data['count'] == 1
        assert data['engagements'][0]['title'] == 'W1'

    def test_all_engagements_api_empty(self, client, engagement_data):
        """API returns empty list when no engagements exist."""
        resp = client.get('/api/engagements/all')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['count'] == 0

    def test_all_engagements_api_includes_expected_fields(self, client, app, engagement_data):
        """API response includes all expected fields for each engagement."""
        cid = engagement_data['customer_id']
        with app.app_context():
            db.session.add(Engagement(
                customer_id=cid, title='Field Check', status='Active',
                target_date=date(2026, 3, 1),
            ))
            db.session.commit()

        resp = client.get('/api/engagements/all')
        data = resp.get_json()
        e = data['engagements'][0]
        expected_fields = [
            'id', 'title', 'status', 'customer_name', 'customer_id',
            'seller_name', 'estimated_acr', 'target_date',
            'story_completeness', 'linked_note_count',
            'opportunity_count', 'milestone_count', 'updated_at',
        ]
        for field in expected_fields:
            assert field in e, f"Missing field: {field}"

    def test_navbar_has_engagements_link(self, client, engagement_data):
        """Navbar should have an Engagements link."""
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'navEngagements' in resp.data
        assert b'/engagements' in resp.data


class TestEngagementMilestonesAPI:
    """Test /api/engagements/milestones endpoint for milestone auto-suggest."""

    def test_returns_milestones_for_engagement(self, client, app, engagement_data):
        """Returns milestones linked to an engagement."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Has Milestones', status='Active')
            ms = Milestone(
                title='Migrate to Azure',
                msx_milestone_id='ms-001',
                milestone_number='7-100',
                msx_status='On Track',
                msx_status_code=861980000,
                url='https://msx.example.com/ms-001',
                customer_id=cid,
                workload='Azure Compute',
                on_my_team=True,
            )
            db.session.add_all([eng, ms])
            db.session.flush()
            eng.milestones.append(ms)
            db.session.commit()
            eid = eng.id

        resp = client.get(f'/api/engagements/milestones?ids={eid}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]['id'] == 'ms-001'
        assert data[0]['name'] == 'Migrate to Azure'
        assert data[0]['number'] == '7-100'
        assert data[0]['status'] == 'On Track'
        assert data[0]['workload'] == 'Azure Compute'
        assert data[0]['on_my_team'] is True
        assert data[0]['url'] == 'https://msx.example.com/ms-001'

    def test_deduplicates_shared_milestones(self, client, app, engagement_data):
        """Same milestone on two engagements is only returned once."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng1 = Engagement(customer_id=cid, title='Eng A', status='Active')
            eng2 = Engagement(customer_id=cid, title='Eng B', status='Active')
            ms = Milestone(
                title='Shared Milestone',
                msx_milestone_id='ms-shared',
                url='https://msx.example.com/ms-shared',
                customer_id=cid,
            )
            db.session.add_all([eng1, eng2, ms])
            db.session.flush()
            eng1.milestones.append(ms)
            eng2.milestones.append(ms)
            db.session.commit()
            eid1, eid2 = eng1.id, eng2.id

        resp = client.get(f'/api/engagements/milestones?ids={eid1},{eid2}')
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]['id'] == 'ms-shared'

    def test_returns_multiple_milestones(self, client, app, engagement_data):
        """Returns all distinct milestones across selected engagements."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Multi-MS', status='Active')
            ms1 = Milestone(
                title='Milestone A', msx_milestone_id='ms-a',
                url='https://msx.example.com/a', customer_id=cid,
            )
            ms2 = Milestone(
                title='Milestone B', msx_milestone_id='ms-b',
                url='https://msx.example.com/b', customer_id=cid,
            )
            db.session.add_all([eng, ms1, ms2])
            db.session.flush()
            eng.milestones.extend([ms1, ms2])
            db.session.commit()
            eid = eng.id

        resp = client.get(f'/api/engagements/milestones?ids={eid}')
        data = resp.get_json()
        assert len(data) == 2
        ids = {m['id'] for m in data}
        assert ids == {'ms-a', 'ms-b'}

    def test_empty_ids_returns_empty(self, client):
        """Empty or missing ids returns empty list."""
        resp = client.get('/api/engagements/milestones')
        assert resp.status_code == 200
        assert resp.get_json() == []

        resp = client.get('/api/engagements/milestones?ids=')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_invalid_ids_returns_empty(self, client):
        """Non-integer ids return empty list."""
        resp = client.get('/api/engagements/milestones?ids=abc')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_no_milestones_returns_empty(self, client, app, engagement_data):
        """Engagement with no milestones returns empty list."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='No MS', status='Active')
            db.session.add(eng)
            db.session.commit()
            eid = eng.id

        resp = client.get(f'/api/engagements/milestones?ids={eid}')
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_nonexistent_ids_returns_empty(self, client):
        """Non-existent engagement IDs return empty list."""
        resp = client.get('/api/engagements/milestones?ids=99999')
        assert resp.status_code == 200
        assert resp.get_json() == []


class TestMilestoneEngagementCrossLink:
    """Test that saving a note cross-links its milestone to its engagements."""

    def test_create_note_links_milestone_to_engagement(self, client, app, engagement_data):
        """Creating a note with both engagement and milestone links the milestone to the engagement."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Cross Link Eng', status='Active')
            ms = Milestone(
                title='Cross Link MS',
                msx_milestone_id='ms-cross-1',
                url='https://msx.example.com/cross-1',
                customer_id=cid,
            )
            db.session.add_all([eng, ms])
            db.session.commit()
            eng_id, ms_id = eng.id, ms.msx_milestone_id

        resp = client.post('/note/new', data={
            'customer_id': str(cid),
            'call_date': '2026-01-29',
            'call_time': '10:00',
            'content': 'Note with both engagement and milestone.',
            'engagement_ids': [str(eng_id)],
            'milestone_msx_id': ms_id,
            'milestone_url': 'https://msx.example.com/cross-1',
            'milestone_name': 'Cross Link MS',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            eng = db.session.get(Engagement, eng_id)
            ms_titles = [m.title for m in eng.milestones]
            assert 'Cross Link MS' in ms_titles

    def test_edit_note_links_milestone_to_engagement(self, client, app, engagement_data):
        """Editing a note to add a milestone also links it to the engagement."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Edit Cross Eng', status='Active')
            ms = Milestone(
                title='Edit Cross MS',
                msx_milestone_id='ms-cross-edit',
                url='https://msx.example.com/cross-edit',
                customer_id=cid,
            )
            note = Note(
                customer_id=cid,
                call_date=datetime(2026, 1, 29, 10, 0, tzinfo=timezone.utc),
                content='Original content.',
            )
            db.session.add_all([eng, ms, note])
            db.session.flush()
            note.engagements.append(eng)
            db.session.commit()
            eng_id = eng.id
            note_id = note.id
            ms_msx_id = ms.msx_milestone_id

        resp = client.post(f'/note/{note_id}/edit', data={
            'customer_id': str(cid),
            'call_date': '2026-01-29',
            'call_time': '10:00',
            'content': 'Updated content with milestone.',
            'engagement_ids': [str(eng_id)],
            'milestone_msx_id': ms_msx_id,
            'milestone_url': 'https://msx.example.com/cross-edit',
            'milestone_name': 'Edit Cross MS',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            eng = db.session.get(Engagement, eng_id)
            ms_titles = [m.title for m in eng.milestones]
            assert 'Edit Cross MS' in ms_titles

    def test_no_duplicate_link_if_already_attached(self, client, app, engagement_data):
        """If milestone is already on engagement, no duplicate is created."""
        cid = engagement_data['customer_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Already Linked', status='Active')
            ms = Milestone(
                title='Already There MS',
                msx_milestone_id='ms-already',
                url='https://msx.example.com/already',
                customer_id=cid,
            )
            db.session.add_all([eng, ms])
            db.session.flush()
            eng.milestones.append(ms)
            db.session.commit()
            eng_id = eng.id

        resp = client.post('/note/new', data={
            'customer_id': str(cid),
            'call_date': '2026-01-29',
            'call_time': '10:00',
            'content': 'Note where milestone already on engagement.',
            'engagement_ids': [str(eng_id)],
            'milestone_msx_id': 'ms-already',
            'milestone_url': 'https://msx.example.com/already',
            'milestone_name': 'Already There MS',
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            eng = db.session.get(Engagement, eng_id)
            assert len(eng.milestones) == 1  # no duplicate


class TestEngagementTasks:
    """Test engagement task CRUD operations."""

    @pytest.fixture
    def eng_with_note(self, app, engagement_data):
        """Create an engagement with a linked note."""
        cid = engagement_data['customer_id']
        nid = engagement_data['note1_id']
        with app.app_context():
            eng = Engagement(customer_id=cid, title='Task Test Eng', status='Active')
            note = db.session.get(Note, nid)
            eng.notes.append(note)
            db.session.add(eng)
            db.session.commit()
            return {'engagement_id': eng.id, 'note_id': nid, 'customer_id': cid}

    def test_create_task(self, client, app, eng_with_note):
        """Create a task via JSON API."""
        eid = eng_with_note['engagement_id']
        resp = client.post(
            f'/engagement/{eid}/tasks',
            data=json.dumps({'title': 'Follow up on POC', 'priority': 'high'}),
            content_type='application/json',
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['success'] is True
        assert data['task']['title'] == 'Follow up on POC'
        assert data['task']['priority'] == 'high'
        assert data['task']['status'] == 'open'

        with app.app_context():
            task = EngagementTask.query.filter_by(engagement_id=eid).first()
            assert task is not None
            assert task.title == 'Follow up on POC'

    def test_create_task_with_due_date(self, client, app, eng_with_note):
        """Create a task with a due date."""
        eid = eng_with_note['engagement_id']
        resp = client.post(
            f'/engagement/{eid}/tasks',
            data=json.dumps({'title': 'Send proposal', 'due_date': '2026-04-15'}),
            content_type='application/json',
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['task']['due_date'] == '2026-04-15'

    def test_create_task_with_note_id(self, client, app, eng_with_note):
        """Create a task linked to a specific note."""
        eid = eng_with_note['engagement_id']
        nid = eng_with_note['note_id']
        resp = client.post(
            f'/engagement/{eid}/tasks',
            data=json.dumps({'title': 'From note', 'note_id': nid}),
            content_type='application/json',
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['task']['note_id'] == nid

    def test_create_task_requires_title(self, client, eng_with_note):
        """Task creation without title should fail."""
        eid = eng_with_note['engagement_id']
        resp = client.post(
            f'/engagement/{eid}/tasks',
            data=json.dumps({'title': ''}),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert resp.get_json()['error'] == 'Title is required.'

    def test_get_task(self, client, app, eng_with_note):
        """Get a single task via JSON API."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            task = EngagementTask(engagement_id=eid, title='Fetch me')
            db.session.add(task)
            db.session.commit()
            tid = task.id

        resp = client.get(f'/task/{tid}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['task']['title'] == 'Fetch me'

    def test_update_task(self, client, app, eng_with_note):
        """Update a task via PUT."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            task = EngagementTask(engagement_id=eid, title='Old title')
            db.session.add(task)
            db.session.commit()
            tid = task.id

        resp = client.put(
            f'/task/{tid}',
            data=json.dumps({
                'title': 'New title',
                'contact': 'Jane Doe',
                'priority': 'high',
                'due_date': '2026-05-01',
                'description': '<p>Updated details</p>',
            }),
            content_type='application/json',
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['task']['title'] == 'New title'
        assert data['task']['contact'] == 'Jane Doe'
        assert data['task']['priority'] == 'high'

        with app.app_context():
            task = db.session.get(EngagementTask, tid)
            assert task.title == 'New title'
            assert task.due_date == date(2026, 5, 1)
            assert task.description == '<p>Updated details</p>'

    def test_toggle_task(self, client, app, eng_with_note):
        """Toggle task between open and completed."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            task = EngagementTask(engagement_id=eid, title='Toggle me')
            db.session.add(task)
            db.session.commit()
            tid = task.id

        # Toggle to completed
        resp = client.post(f'/task/{tid}/toggle')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['task']['status'] == 'completed'
        assert data['task']['completed_at'] is not None

        # Toggle back to open
        resp = client.post(f'/task/{tid}/toggle')
        data = resp.get_json()
        assert data['task']['status'] == 'open'
        assert data['task']['completed_at'] is None

    def test_delete_task(self, client, app, eng_with_note):
        """Delete a task."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            task = EngagementTask(engagement_id=eid, title='Delete me')
            db.session.add(task)
            db.session.commit()
            tid = task.id

        resp = client.delete(f'/task/{tid}')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        with app.app_context():
            assert db.session.get(EngagementTask, tid) is None

    def test_open_task_count_property(self, app, eng_with_note):
        """Engagement.open_task_count returns only open tasks."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            t1 = EngagementTask(engagement_id=eid, title='Open 1')
            t2 = EngagementTask(engagement_id=eid, title='Open 2')
            t3 = EngagementTask(engagement_id=eid, title='Done', status='completed')
            db.session.add_all([t1, t2, t3])
            db.session.commit()

            eng = db.session.get(Engagement, eid)
            assert eng.open_task_count == 2

    def test_is_overdue_property(self, app, eng_with_note):
        """EngagementTask.is_overdue returns True for past-due open tasks."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            past = EngagementTask(
                engagement_id=eid, title='Overdue', due_date=date(2020, 1, 1)
            )
            future = EngagementTask(
                engagement_id=eid, title='Future', due_date=date(2099, 12, 31)
            )
            no_date = EngagementTask(engagement_id=eid, title='No date')
            done = EngagementTask(
                engagement_id=eid, title='Done overdue',
                due_date=date(2020, 1, 1), status='completed',
            )
            db.session.add_all([past, future, no_date, done])
            db.session.commit()

            assert past.is_overdue is True
            assert future.is_overdue is False
            assert no_date.is_overdue is False
            assert done.is_overdue is False

    def test_task_cascade_delete_with_engagement(self, client, app, eng_with_note):
        """Tasks should be deleted when their engagement is deleted."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            task = EngagementTask(engagement_id=eid, title='Cascade me')
            db.session.add(task)
            db.session.commit()
            tid = task.id

        resp = client.post(f'/engagement/{eid}/delete', follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            assert db.session.get(EngagementTask, tid) is None

    def test_task_count_in_api(self, client, app, eng_with_note):
        """API endpoint should include open_task_count."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            db.session.add(EngagementTask(engagement_id=eid, title='T1'))
            db.session.add(EngagementTask(engagement_id=eid, title='T2', status='completed'))
            db.session.commit()

        resp = client.get('/api/engagements/all')
        data = resp.get_json()
        eng_data = next(e for e in data['engagements'] if e['id'] == eid)
        assert eng_data['open_task_count'] == 1

    def test_engagement_view_shows_tasks(self, client, app, eng_with_note):
        """Engagement view page should display tasks."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            db.session.add(EngagementTask(engagement_id=eid, title='Visible Task'))
            db.session.commit()

        resp = client.get(f'/engagement/{eid}')
        assert resp.status_code == 200
        assert b'Visible Task' in resp.data
        assert b'check2-square' in resp.data

    def test_reorder_tasks(self, client, app, eng_with_note):
        """Reorder tasks via POST and verify sort_order is persisted."""
        eid = eng_with_note['engagement_id']
        with app.app_context():
            t1 = EngagementTask(engagement_id=eid, title='First', sort_order=0)
            t2 = EngagementTask(engagement_id=eid, title='Second', sort_order=1)
            t3 = EngagementTask(engagement_id=eid, title='Third', sort_order=2)
            db.session.add_all([t1, t2, t3])
            db.session.commit()
            ids = [t3.id, t1.id, t2.id]  # new order: Third, First, Second

        resp = client.post(
            f'/engagement/{eid}/tasks/reorder',
            data=json.dumps({'task_ids': ids}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        with app.app_context():
            tasks = EngagementTask.query.filter_by(engagement_id=eid).order_by(
                EngagementTask.sort_order
            ).all()
            assert [t.title for t in tasks] == ['Third', 'First', 'Second']
