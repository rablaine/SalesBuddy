"""
Tests for the Engagement / Milestone Hygiene report and HygieneNote API.
"""
import json

import pytest
from app.models import db, Customer, Seller, Engagement, Milestone, HygieneNote


@pytest.fixture
def hygiene_data(app):
    """Create sample engagements and milestones for hygiene tests."""
    with app.app_context():
        seller = Seller(name='Test Seller')
        db.session.add(seller)
        db.session.flush()

        cust = Customer(name='Acme Corp', tpid=12345, seller_id=seller.id)
        db.session.add(cust)
        db.session.flush()

        # Engagement with NO milestones
        eng_no_ms = Engagement(
            customer_id=cust.id, title='Orphan Engagement', status='Active'
        )
        # Engagement WITH a milestone (should not appear in report)
        eng_with_ms = Engagement(
            customer_id=cust.id, title='Healthy Engagement', status='Active'
        )
        db.session.add_all([eng_no_ms, eng_with_ms])
        db.session.flush()

        # Milestone with NO engagement
        ms_no_eng = Milestone(
            customer_id=cust.id,
            msx_milestone_id='MS-001',
            title='Orphan Milestone',
            url='https://msx.example.com/ms-001',
            msx_status='On Track',
            on_my_team=True,
        )
        # Milestone WITH an engagement (should not appear)
        ms_with_eng = Milestone(
            customer_id=cust.id,
            msx_milestone_id='MS-002',
            title='Healthy Milestone',
            url='https://msx.example.com/ms-002',
            msx_status='On Track',
            on_my_team=True,
        )
        db.session.add_all([ms_no_eng, ms_with_eng])
        db.session.flush()

        # Link the healthy pair
        eng_with_ms.milestones.append(ms_with_eng)
        db.session.commit()

        return {
            'eng_no_ms_id': eng_no_ms.id,
            'eng_with_ms_id': eng_with_ms.id,
            'ms_no_eng_id': ms_no_eng.id,
            'ms_with_eng_id': ms_with_eng.id,
        }


class TestHygieneReport:
    """Tests for GET /reports/hygiene."""

    def test_report_loads(self, client):
        """Should render the hygiene report page."""
        resp = client.get('/reports/hygiene')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Engagement / Milestone Hygiene' in html

    def test_shows_engagement_without_milestone(self, client, hygiene_data):
        """Should list engagements that have no milestones."""
        resp = client.get('/reports/hygiene')
        html = resp.data.decode()
        assert 'Orphan Engagement' in html
        assert 'Healthy Engagement' not in html

    def test_shows_milestone_without_engagement(self, client, hygiene_data):
        """Should list milestones that have no engagements."""
        resp = client.get('/reports/hygiene')
        html = resp.data.decode()
        assert 'Orphan Milestone' in html
        assert 'Healthy Milestone' not in html

    def test_shows_existing_hygiene_notes(self, client, app, hygiene_data):
        """Should display pre-existing hygiene notes inline."""
        with app.app_context():
            hn = HygieneNote(
                entity_type='engagement',
                entity_id=hygiene_data['eng_no_ms_id'],
                note='Waiting on customer approval',
            )
            db.session.add(hn)
            db.session.commit()

        resp = client.get('/reports/hygiene')
        html = resp.data.decode()
        assert 'Waiting on customer approval' in html


class TestHygieneNoteAPI:
    """Tests for POST /api/hygiene-note."""

    def test_create_note(self, client, app, hygiene_data):
        """Should create a new hygiene note."""
        resp = client.post('/api/hygiene-note',
                           data=json.dumps({
                               'entity_type': 'engagement',
                               'entity_id': hygiene_data['eng_no_ms_id'],
                               'note': 'No MSX match yet',
                           }),
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

        with app.app_context():
            hn = HygieneNote.query.filter_by(
                entity_type='engagement',
                entity_id=hygiene_data['eng_no_ms_id'],
            ).first()
            assert hn is not None
            assert hn.note == 'No MSX match yet'

    def test_update_note(self, client, app, hygiene_data):
        """Should update an existing hygiene note."""
        with app.app_context():
            hn = HygieneNote(
                entity_type='milestone',
                entity_id=hygiene_data['ms_no_eng_id'],
                note='Old reason',
            )
            db.session.add(hn)
            db.session.commit()

        resp = client.post('/api/hygiene-note',
                           data=json.dumps({
                               'entity_type': 'milestone',
                               'entity_id': hygiene_data['ms_no_eng_id'],
                               'note': 'Updated reason',
                           }),
                           content_type='application/json')
        assert resp.status_code == 200

        with app.app_context():
            hn = HygieneNote.query.filter_by(
                entity_type='milestone',
                entity_id=hygiene_data['ms_no_eng_id'],
            ).first()
            assert hn.note == 'Updated reason'

    def test_delete_note_on_empty(self, client, app, hygiene_data):
        """Should delete the record when note is empty."""
        with app.app_context():
            hn = HygieneNote(
                entity_type='engagement',
                entity_id=hygiene_data['eng_no_ms_id'],
                note='To be cleared',
            )
            db.session.add(hn)
            db.session.commit()

        resp = client.post('/api/hygiene-note',
                           data=json.dumps({
                               'entity_type': 'engagement',
                               'entity_id': hygiene_data['eng_no_ms_id'],
                               'note': '',
                           }),
                           content_type='application/json')
        assert resp.status_code == 200

        with app.app_context():
            hn = HygieneNote.query.filter_by(
                entity_type='engagement',
                entity_id=hygiene_data['eng_no_ms_id'],
            ).first()
            assert hn is None

    def test_rejects_invalid_entity_type(self, client):
        """Should return 400 for invalid entity_type."""
        resp = client.post('/api/hygiene-note',
                           data=json.dumps({
                               'entity_type': 'bogus',
                               'entity_id': 1,
                               'note': 'test',
                           }),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_rejects_missing_entity_id(self, client):
        """Should return 400 when entity_id is missing."""
        resp = client.post('/api/hygiene-note',
                           data=json.dumps({
                               'entity_type': 'engagement',
                               'note': 'test',
                           }),
                           content_type='application/json')
        assert resp.status_code == 400
