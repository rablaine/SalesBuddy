"""Tests for the Engagement / Milestone Hygiene report and reason-note API."""
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
        # Off-team milestone with no engagement (visible when toggle is off)
        ms_off_team = Milestone(
            customer_id=cust.id,
            msx_milestone_id='MS-003',
            title='OffTeam Milestone',
            url='https://msx.example.com/ms-003',
            msx_status='On Track',
            on_my_team=False,
        )
        db.session.add_all([ms_no_eng, ms_with_eng, ms_off_team])
        db.session.flush()

        # Link the healthy pair
        eng_with_ms.milestones.append(ms_with_eng)
        db.session.commit()

        return {
            'eng_no_ms_id': eng_no_ms.id,
            'eng_with_ms_id': eng_with_ms.id,
            'ms_no_eng_id': ms_no_eng.id,
            'ms_with_eng_id': ms_with_eng.id,
            'ms_off_team_id': ms_off_team.id,
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

    def test_off_team_milestones_in_html_but_hidden(self, client, hygiene_data):
        """Off-team milestones should be in the HTML (for toggle) but hidden by default."""
        resp = client.get('/reports/hygiene')
        html = resp.data.decode()
        assert 'OffTeam Milestone' in html
        assert 'data-on-team="false"' in html
        # On-team toggle should be present
        assert 'onTeamToggle' in html

    def test_milestones_can_be_grouped_by_seller(self, client, hygiene_data):
        """Milestone rows should expose seller data to the persisted grouping control."""
        resp = client.get('/reports/hygiene')
        html = resp.data.decode()
        assert 'groupBySellerToggle' in html
        assert 'data-seller-name="Test Seller"' in html
        assert 'salesbuddy_hygiene_milestone_view' in html
        assert '--bs-table-bg: var(--bs-tertiary-bg)' in html
        assert "row.className = 'ms-seller-group table-light'" not in html
        assert 'milestoneGlobalHeader' in html
        assert "row.className = 'ms-group-columns'" in html
        assert 'tbody.appendChild(createColumnHeader())' in html
        engagement_section = html.split('Active Engagements without Milestones', 1)[1]
        engagement_table = engagement_section.split('</table>', 1)[0]
        milestone_section = html.split('Milestones without Engagements', 1)[1]
        milestone_table = milestone_section.split('</table>', 1)[0]
        assert 'id="milestoneGlobalHeader"' not in engagement_table
        assert 'id="milestoneGlobalHeader"' in milestone_table

    def test_report_renders_remediation_actions(self, client, hygiene_data):
        """Gap rows should expose inline milestone and engagement fixes."""
        resp = client.get('/reports/hygiene')
        html = resp.data.decode()
        assert 'data-link-engagement-id=' in html
        assert 'data-add-existing-milestone-id=' in html
        assert 'data-create-engagement-milestone-id=' in html
        assert 'id="linkMilestonesModal"' in html
        assert 'id="addExistingEngagementModal"' in html
        assert 'id="existingEngagementSelect"' in html
        assert 'id="createEngagementFromEmptyState"' in html
        assert 'id="createEngagementModal"' in html
        assert 'id="engagementAnnotatedCount"' in html
        assert 'data-create-engagement-milestone-status=' in html
        assert 'data-create-engagement-milestone-msx-id=' in html
        assert 'modal-dialog modal-lg hygiene-picker-overflow-dialog' in html
        assert 'modal-dialog modal-xl hygiene-picker-overflow-dialog' in html
        assert '.hygiene-picker-overflow-dialog .modal-body' in html
        assert 'modal-xl modal-dialog-scrollable' not in html
        assert 'min-height: min(720px' not in html
        assert 'z-index: 1090 !important' in html
        assert 'js/milestone-multi-picker.js' in html
        assert 'removeResolvedMilestones' in html
        assert '/api/customer/' in html
        assert "'/milestones/' + activeMilestoneId + '/add'" in html
        assert "existingModalElement.addEventListener('hidden.bs.modal'" in html
        assert 'openCreateEngagement(createButton)' in html
        assert 'function updateHygieneSummaryCounts()' in html
        assert html.count('updateHygieneSummaryCounts();') == 3
        assert 'Why no milestone?' in html
        assert 'data-entity-type="milestone"' not in html

    def test_report_embeds_client_sort_values(self, client, hygiene_data):
        """Milestone rows should expose customer and ACR values for redraw sorting."""
        resp = client.get('/reports/hygiene')
        html = resp.data.decode()
        assert 'data-customer-name="Acme Corp"' in html
        assert 'data-acr=' in html
        assert 'customerOrder' in html
        assert 'acrOrder' in html

    def test_shared_picker_keeps_scrollbar_open_and_colors_statuses(self, client):
        """Shared picker should preserve scroll interaction and standard badge colors."""
        resp = client.get('/static/js/milestone-multi-picker.js')
        assert resp.status_code == 200
        javascript = resp.data.decode()
        assert "case 'On Track': return 'bg-success'" in javascript
        assert "case 'At Risk': return 'bg-warning text-dark'" in javascript
        assert "case 'Blocked': return 'bg-danger'" in javascript
        assert "this.results.addEventListener('pointerdown'" in javascript
        assert "if (!picker.resultsInteracting) picker.closeResults()" in javascript
        assert 'this.closeResults();' in javascript
        assert 'this.input.blur();' in javascript
        assert "this.input.addEventListener('click'" in javascript
        assert 'milestone.local_milestone_id != null' in javascript
        assert 'Number.isInteger(milestoneId) && milestoneId > 0' in javascript
        assert 'initialSelections' in javascript
        assert 'Loading optional additional milestones...' in javascript
        assert "'card border-success mb-2'" in javascript
        assert 'bg-success-subtle' not in javascript
        assert 'card-body d-flex justify-content-between' in javascript
        assert 'bi bi-check-circle-fill text-success' in javascript

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
    """Tests for engagement reason notes posted to /api/hygiene-note."""

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
                entity_type='engagement',
                entity_id=hygiene_data['eng_no_ms_id'],
                note='Old reason',
            )
            db.session.add(hn)
            db.session.commit()

        resp = client.post('/api/hygiene-note',
                           data=json.dumps({
                               'entity_type': 'engagement',
                               'entity_id': hygiene_data['eng_no_ms_id'],
                               'note': 'Updated reason',
                           }),
                           content_type='application/json')
        assert resp.status_code == 200

        with app.app_context():
            hn = HygieneNote.query.filter_by(
                entity_type='engagement',
                entity_id=hygiene_data['eng_no_ms_id'],
            ).first()
            assert hn.note == 'Updated reason'

    def test_rejects_removed_milestone_reason_notes(self, client, hygiene_data):
        """Milestones should no longer accept obsolete reason notes."""
        resp = client.post('/api/hygiene-note', json={
            'entity_type': 'milestone',
            'entity_id': hygiene_data['ms_no_eng_id'],
            'note': 'Obsolete reason',
        })
        assert resp.status_code == 400

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
