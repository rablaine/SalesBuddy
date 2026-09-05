"""Tests for persistent one-on-one notes and agenda workspaces."""
import pytest

from app.models import (
    Customer,
    Engagement,
    Milestone,
    Note,
    OneOnOneAgendaItem,
    OneOnOneWorkspace,
    Seller,
    db,
)


@pytest.fixture
def one_on_one_data(app):
    """Create two seller books with agenda candidates."""
    with app.app_context():
        seller = Seller(name='Workspace Seller', alias='workspace')
        other_seller = Seller(name='Other Workspace Seller', alias='otherworkspace')
        customer = Customer(name='Workspace Customer', tpid=91001, seller=seller)
        other_customer = Customer(
            name='Other Workspace Customer',
            tpid=91002,
            seller=other_seller,
        )
        milestone = Milestone(
            customer=customer,
            msx_milestone_id='WORKSPACE-MS-1',
            title='Discuss migration milestone',
            url='https://msx.example.com/workspace-ms-1',
            msx_status='On Track',
            monthly_usage=25000,
            on_my_team=True,
        )
        lower_priority_team_milestone = Milestone(
            customer=customer,
            msx_milestone_id='WORKSPACE-MS-TEAM-2',
            title='Second team milestone',
            url='https://msx.example.com/workspace-ms-team-2',
            msx_status='On Track',
            monthly_usage=5000,
            on_my_team=True,
        )
        high_acr_off_team_milestone = Milestone(
            customer=customer,
            msx_milestone_id='WORKSPACE-MS-OFF-TEAM',
            title='Higher ACR off-team milestone',
            url='https://msx.example.com/workspace-ms-off-team',
            msx_status='On Track',
            monthly_usage=100000,
            on_my_team=False,
        )
        other_milestone = Milestone(
            customer=other_customer,
            msx_milestone_id='WORKSPACE-MS-2',
            title='Other seller milestone',
            url='https://msx.example.com/workspace-ms-2',
            msx_status='At Risk',
        )
        engagement = Engagement(
            customer=customer,
            title='Discuss architecture engagement',
            status='Active',
            estimated_acr=18000,
        )
        db.session.add_all([
            seller,
            other_seller,
            customer,
            other_customer,
            milestone,
            lower_priority_team_milestone,
            high_acr_off_team_milestone,
            other_milestone,
            engagement,
        ])
        db.session.commit()
        return {
            'seller_id': seller.id,
            'other_seller_id': other_seller.id,
            'milestone_id': milestone.id,
            'lower_priority_team_milestone_id': lower_priority_team_milestone.id,
            'high_acr_off_team_milestone_id': high_acr_off_team_milestone.id,
            'other_milestone_id': other_milestone.id,
            'engagement_id': engagement.id,
        }


class TestOneOnOneWorkspaceNavigation:
    """Workspace entry points should stay contextual to existing pages."""

    def test_unsurfaced_workspace_hub(self, client, one_on_one_data):
        """Direct hub URL should list existing workspaces and seller quick starts."""
        initial = client.get('/one-on-one/')
        assert initial.status_code == 200
        assert b'1:1 notes' in initial.data
        assert b'Workspace Seller' in initial.data
        assert b'Start another workspace' in initial.data

        client.get(f"/seller/{one_on_one_data['seller_id']}/one-on-one")
        updated = client.get('/one-on-one/')
        html = updated.data.decode()
        assert 'Your workspaces' in html
        assert '/one-on-one/' in html
        assert 'No standing notes yet.' in html

    def test_seller_route_creates_one_workspace_once(self, client, app, one_on_one_data):
        """Seller entry should create once and reuse the linked workspace."""
        seller_id = one_on_one_data['seller_id']
        first = client.get(f'/seller/{seller_id}/one-on-one')
        second = client.get(f'/seller/{seller_id}/one-on-one')
        assert first.status_code == 302
        assert second.status_code == 302
        assert first.location == second.location

        with app.app_context():
            workspaces = OneOnOneWorkspace.query.filter_by(seller_id=seller_id).all()
            assert len(workspaces) == 1
            assert workspaces[0].person_name == 'Workspace Seller'

    def test_create_standalone_manager_workspace(self, client, app):
        """People absent from Seller should get standalone workspaces."""
        response = client.post('/one-on-one/new', data={
            'person_name': 'Manager Person',
            'person_type': 'Manager',
        })
        assert response.status_code == 302
        assert '/one-on-one/' in response.location

        with app.app_context():
            workspace = OneOnOneWorkspace.query.filter_by(
                person_name='Manager Person'
            ).one()
            assert workspace.person_type == 'Manager'
            assert workspace.seller_id is None

    def test_report_has_workspace_launcher(self, client, one_on_one_data):
        """Existing report should expose notes without becoming a new nav section."""
        response = client.get('/reports/one-on-one')
        assert response.status_code == 200
        html = response.data.decode()
        assert 'Open 1:1 Notes' in html
        assert 'oneOnOneWorkspaceLauncher' in html
        assert 'Manager or other person' in html

    def test_seller_page_has_workspace_link(self, client, one_on_one_data, monkeypatch):
        """Seller detail should link directly to that seller's notes."""
        monkeypatch.setattr(
            'app.services.revenue_analysis.get_seller_alerts', lambda name: []
        )
        monkeypatch.setattr(
            'app.services.milestone_sync.get_milestone_tracker_data_for_seller',
            lambda seller_id: {
                'milestones': [],
                'summary': {},
                'areas': [],
                'quarters': [],
            },
        )
        monkeypatch.setattr('app.routes.main._find_stale_milestones', lambda **kwargs: [])
        response = client.get(f"/seller/{one_on_one_data['seller_id']}")
        assert response.status_code == 200
        html = response.data.decode()
        assert '1:1 Notes' in html
        assert f"/seller/{one_on_one_data['seller_id']}/one-on-one" in html


class TestOneOnOneWorkspaceAPI:
    """Workspace APIs should persist notes and safely manage agenda items."""

    def _workspace(self, client, app, seller_id):
        """Create and return one seller workspace ID through the public route."""
        client.get(f'/seller/{seller_id}/one-on-one')
        with app.app_context():
            return OneOnOneWorkspace.query.filter_by(seller_id=seller_id).one().id

    def test_workspace_page_and_notes_autosave(self, client, app, one_on_one_data):
        """Workspace should render and persist standing notes."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        page = client.get(f'/one-on-one/{workspace_id}')
        assert page.status_code == 200
        assert b'Standing notes' in page.data
        assert b'Next conversation' in page.data
        html = page.data.decode()
        engagement_button = html.index('data-item-type="engagement"')
        milestone_button = html.index('data-item-type="milestone"')
        assert engagement_button < milestone_button
        assert "let activeType = 'engagement';" in html
        assert 'salesbuddy_one_on_one_notes_height_' in html
        assert 'new ResizeObserver' in html
        assert 'quill/2.0.0-dev.3/quill.min.js' in html
        assert 'new Quill(workspaceNotes' in html
        assert "['bold', 'italic', 'underline', 'strike']" in html
        assert 'quill-better-table@1.2.10' in html
        assert "insertTable(rows, columns)" in html
        assert 'id="workspaceTableRows"' in html
        assert 'id="workspaceTableColumns"' in html
        assert "querySelector('table, img')" in html
        assert "['clean']" not in html

        response = client.patch(
            f'/api/one-on-one/{workspace_id}',
            json={'notes': '<p><strong>Ask</strong> about career goals and blockers.</p>'},
        )
        assert response.status_code == 200
        with app.app_context():
            workspace = db.session.get(OneOnOneWorkspace, workspace_id)
            assert workspace.notes == (
                '<p><strong>Ask</strong> about career goals and blockers.</p>'
            )
        hub_html = client.get('/one-on-one/').data.decode()
        assert 'Ask about career goals and blockers.' in hub_html
        assert '&lt;strong&gt;Ask&lt;/strong&gt;' not in hub_html

        table_html = (
            '<table><tbody><tr><td>Topic</td><td>Owner</td></tr></tbody></table>'
        )
        assert client.patch(
            f'/api/one-on-one/{workspace_id}',
            json={'notes': table_html},
        ).status_code == 200
        reloaded_html = client.get(f'/one-on-one/{workspace_id}').data.decode()
        assert table_html in reloaded_html

    def test_engagement_item_exposes_inline_editor(
        self, client, app, one_on_one_data
    ):
        """Engagement agenda items should offer the details editor."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        client.post(f'/api/one-on-one/{workspace_id}/agenda', json={
            'item_type': 'engagement',
            'entity_id': one_on_one_data['engagement_id'],
        })

        response = client.get(f'/one-on-one/{workspace_id}')
        html = response.data.decode()

        assert response.status_code == 200
        assert 'id="editEngagementModal"' in html
        assert f'data-edit-engagement="{one_on_one_data["engagement_id"]}"' in html
        assert 'Technical/Business Problem' in html
        assert 'Save changes' in html

    def test_seller_candidates_are_scoped(self, client, app, one_on_one_data):
        """Seller workspaces should only search that seller's customer book."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        response = client.get(
            f'/api/one-on-one/{workspace_id}/candidates?type=milestone'
        )
        assert response.status_code == 200
        results = response.get_json()['results']
        ids = {item['id'] for item in results}
        assert one_on_one_data['milestone_id'] in ids
        assert one_on_one_data['other_milestone_id'] not in ids
        assert results[0]['id'] == one_on_one_data['milestone_id']
        assert results[0]['on_my_team'] is True
        assert results[1]['id'] == one_on_one_data['lower_priority_team_milestone_id']
        assert results[2]['id'] == one_on_one_data['high_acr_off_team_milestone_id']
        assert results[2]['on_my_team'] is False

    def test_add_discuss_restore_and_delete_item(self, client, app, one_on_one_data):
        """Agenda items should move through discussion history and removal."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        candidates_url = (
            f'/api/one-on-one/{workspace_id}/candidates?type=milestone'
        )

        def candidate_ids():
            """Return currently selectable milestone IDs."""
            return {
                item['id']
                for item in client.get(candidates_url).get_json()['results']
            }

        milestone_id = one_on_one_data['milestone_id']
        assert milestone_id in candidate_ids()
        added = client.post(f'/api/one-on-one/{workspace_id}/agenda', json={
            'item_type': 'milestone',
            'entity_id': milestone_id,
        })
        assert added.status_code == 200
        item_id = added.get_json()['item']['id']
        assert milestone_id not in candidate_ids()

        note = client.patch(f'/api/one-on-one/agenda/{item_id}', json={
            'talking_points': 'Ask whether the target date is still realistic.',
        })
        discussed = client.patch(f'/api/one-on-one/agenda/{item_id}', json={
            'status': 'discussed',
        })
        assert milestone_id in candidate_ids()
        restored = client.patch(f'/api/one-on-one/agenda/{item_id}', json={
            'status': 'active',
        })
        assert milestone_id not in candidate_ids()
        deleted = client.delete(f'/api/one-on-one/agenda/{item_id}')
        assert milestone_id in candidate_ids()

        assert note.status_code == 200
        assert discussed.get_json()['item']['discussed_at'] is not None
        assert restored.get_json()['item']['status'] == 'active'
        assert deleted.status_code == 200
        with app.app_context():
            assert db.session.get(OneOnOneAgendaItem, item_id) is None

    def test_duplicate_active_item_is_rejected(self, client, app, one_on_one_data):
        """Same linked entity should appear only once on an active agenda."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        payload = {
            'item_type': 'engagement',
            'entity_id': one_on_one_data['engagement_id'],
        }
        assert client.post(
            f'/api/one-on-one/{workspace_id}/agenda', json=payload
        ).status_code == 200
        assert client.post(
            f'/api/one-on-one/{workspace_id}/agenda', json=payload
        ).status_code == 409

    def test_cross_seller_item_is_rejected(self, client, app, one_on_one_data):
        """Seller workspaces should reject direct cross-book API additions."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        response = client.post(f'/api/one-on-one/{workspace_id}/agenda', json={
            'item_type': 'milestone',
            'entity_id': one_on_one_data['other_milestone_id'],
        })
        assert response.status_code == 400
        assert 'outside this workspace scope' in response.get_json()['error']

    def test_milestone_becomes_linked_engagement(
        self, client, app, one_on_one_data
    ):
        """Milestone workflow should create a linked engagement in place."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        added = client.post(f'/api/one-on-one/{workspace_id}/agenda', json={
            'item_type': 'milestone',
            'entity_id': one_on_one_data['milestone_id'],
        })
        item_id = added.get_json()['item']['id']

        response = client.post(
            f'/api/one-on-one/agenda/{item_id}/engagement',
            json={'title': 'Migration execution engagement'},
        )

        assert response.status_code == 200
        engagement_id = response.get_json()['engagement_id']
        with app.app_context():
            item = db.session.get(OneOnOneAgendaItem, item_id)
            engagement = db.session.get(Engagement, engagement_id)
            assert item.item_type == 'engagement'
            assert item.milestone_id is None
            assert item.engagement_id == engagement.id
            assert engagement.title == 'Migration execution engagement'
            assert [milestone.id for milestone in engagement.milestones] == [
                one_on_one_data['milestone_id']
            ]

    def test_engagement_discussion_note_links_milestone(
        self, client, app, one_on_one_data, monkeypatch
    ):
        """1:1 notes should attach to engagement and its milestone."""
        tracked_note_ids = []
        backed_up_customer_ids = []
        monkeypatch.setattr(
            'app.routes.one_on_one.track_note_on_milestones',
            lambda note: tracked_note_ids.append(note.id),
        )
        monkeypatch.setattr(
            'app.routes.one_on_one.schedule_customer_backup',
            lambda customer_id: backed_up_customer_ids.append(customer_id),
        )
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        added = client.post(f'/api/one-on-one/{workspace_id}/agenda', json={
            'item_type': 'milestone',
            'entity_id': one_on_one_data['milestone_id'],
        })
        item_id = added.get_json()['item']['id']
        converted = client.post(
            f'/api/one-on-one/agenda/{item_id}/engagement',
            json={'title': 'Migration execution engagement'},
        )
        engagement_id = converted.get_json()['engagement_id']

        response = client.post(
            f'/api/one-on-one/agenda/{item_id}/notes',
            json={'content': 'Seller confirmed architecture review next Tuesday.'},
        )

        assert response.status_code == 200
        note_id = response.get_json()['note']['id']
        with app.app_context():
            note = db.session.get(Note, note_id)
            assert note.content == 'Seller confirmed architecture review next Tuesday.'
            assert [engagement.id for engagement in note.engagements] == [engagement_id]
            assert [milestone.id for milestone in note.milestones] == [
                one_on_one_data['milestone_id']
            ]
        assert tracked_note_ids == [note_id]
        assert backed_up_customer_ids == [note.customer_id]

    def test_engagement_flow_validates_item_type_and_content(
        self, client, app, one_on_one_data
    ):
        """Workflow endpoints should reject invalid transitions and empty notes."""
        workspace_id = self._workspace(client, app, one_on_one_data['seller_id'])
        added = client.post(f'/api/one-on-one/{workspace_id}/agenda', json={
            'item_type': 'engagement',
            'entity_id': one_on_one_data['engagement_id'],
        })
        item_id = added.get_json()['item']['id']

        conversion = client.post(
            f'/api/one-on-one/agenda/{item_id}/engagement',
            json={'title': 'Should not be created'},
        )
        empty_note = client.post(
            f'/api/one-on-one/agenda/{item_id}/notes',
            json={'content': '   '},
        )

        assert conversion.status_code == 400
        assert empty_note.status_code == 400
