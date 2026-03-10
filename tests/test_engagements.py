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
from app.models import db, Customer, Seller, Territory, Note, Engagement, Topic


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
                'estimated_acr': '$50k/month',
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
            assert eng.estimated_acr == '$50k/month'
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
                estimated_acr='$100k',
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
                estimated_acr='$50k',
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
        assert e['estimated_acr'] == '$50k'
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
