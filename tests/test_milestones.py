"""
Tests for milestone functionality.
"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
import pytest
from bs4 import BeautifulSoup
from datetime import datetime
from unittest.mock import patch
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from app.models import db, Customer, Engagement, Milestone, MsxTask, Note


@pytest.fixture
def cache_payload(sample_customer):
    """Supply metadata from a partner tool's completed MSX lookup."""
    return {
        "customer_id": sample_customer.id,
        "tpid": sample_customer.tpid,
        "msx_milestone_id": "11111111-1111-4111-8111-111111111111",
        "name": "Fabric adoption",
        "number": "7-123456789",
        "status": "On Track",
        "opportunity_name": "Fabric modernization",
        "workload": "Fabric",
        "due_date": "2027-01-15T00:00:00Z",
        "monthly_usage": 0,
    }


class TestLocalMilestoneCache:
    """Exercise local provisioning, validation, persistence and retry behavior."""

    def test_create_commit_inventory_and_reuse(self, client, app, cache_payload):
        """Commit all supplied fields without remote calls or engagement side effects."""
        from app.services.salesiq_tools import get_milestone_status

        with patch('requests.sessions.Session.request', side_effect=AssertionError("Remote call")):
            response = client.post('/api/milestones/cache', json=cache_payload)
        assert response.status_code == 201
        result = response.get_json()
        assert result == {
            "success": True,
            "local_milestone_id": result["local_milestone_id"],
            "msx_milestone_id": cache_payload["msx_milestone_id"],
            "customer_id": cache_payload["customer_id"],
            "tpid": cache_payload["tpid"],
            "created": True,
        }
        with app.app_context():
            milestone = db.session.get(Milestone, result["local_milestone_id"])
            assert milestone.title == cache_payload["name"]
            assert milestone.milestone_number == cache_payload["number"]
            assert milestone.msx_status == cache_payload["status"]
            assert milestone.opportunity_name == cache_payload["opportunity_name"]
            assert milestone.workload == cache_payload["workload"]
            assert milestone.due_date == datetime(2027, 1, 15)
            assert milestone.monthly_usage == 0
            assert milestone.url.endswith(f'&id={cache_payload["msx_milestone_id"]}')
            assert not milestone.on_my_team
            assert milestone.last_synced_at is None
            assert milestone.opportunity_id is None
            assert Note.query.count() == Engagement.query.count() == MsxTask.query.count() == 0
            inventory = get_milestone_status(customer_id=cache_payload["customer_id"])
            assert [row["id"] for row in inventory] == [milestone.id]

        cache_payload["name"] = "Do not overwrite"
        reused = client.post('/api/milestones/cache', json=cache_payload)
        assert reused.status_code == 200
        assert reused.get_json() == {**result, "created": False}
        with app.app_context():
            assert Milestone.query.count() == 1
            assert Milestone.query.first().title == "Fabric adoption"

    @pytest.mark.parametrize("guid", [
        "ABCDEFAB-1111-4111-8111-111111111111",
        "{ABCDEFAB-1111-4111-8111-111111111111}",
        "abcdefab111141118111111111111111",
        " urn:uuid:ABCDEFAB-1111-4111-8111-111111111111 ",
    ])
    def test_reuse_legacy_guid_spelling(self, client, app, cache_payload, guid):
        """Legacy GUID spellings resolve to the same local identity without rewriting."""
        with app.app_context():
            milestone = Milestone(
                msx_milestone_id=guid, customer_id=cache_payload["customer_id"], url="existing",
            )
            db.session.add(milestone)
            db.session.commit()
            local_id = milestone.id
        cache_payload["msx_milestone_id"] = "{ABCDEFAB-1111-4111-8111-111111111111}"
        response = client.post('/api/milestones/cache', json=cache_payload)
        assert response.status_code == 200
        assert response.json["local_milestone_id"] == local_id
        assert response.json["msx_milestone_id"] == "abcdefab-1111-4111-8111-111111111111"
        with app.app_context():
            assert Milestone.query.count() == 1
            assert Milestone.query.first().msx_milestone_id == guid

    @pytest.mark.parametrize("status", ["Completed", "Cancelled", "Unknown future status", None])
    def test_old_and_unknown_metadata(self, client, app, cache_payload, status):
        """Completion, age and unknown labels must not prevent local provisioning."""
        cache_payload.update(
            status=status, workload=None, due_date="2001-01-01T02:00:00+02:00",
            partner_extension={"future": True},
        )
        response = client.post('/api/milestones/cache', json=cache_payload)
        assert response.status_code == 201
        with app.app_context():
            milestone = Milestone.query.first()
            assert milestone.msx_status == status
            assert milestone.workload is None
            assert milestone.due_date == datetime(2001, 1, 1)

    def test_identifiers_only(self, client, app, cache_payload):
        """Unknown optional metadata can be omitted entirely."""
        payload = {key: cache_payload[key] for key in ("customer_id", "tpid", "msx_milestone_id")}
        assert client.post('/api/milestones/cache', json=payload).status_code == 201
        with app.app_context():
            assert Milestone.query.first().monthly_usage is None

    @pytest.mark.parametrize("usage", [-1, -25.5])
    def test_negative_usage(self, client, app, cache_payload, usage):
        """Preserve finite negative usage supplied by MSX."""
        cache_payload["monthly_usage"] = usage
        response = client.post('/api/milestones/cache', json=cache_payload)
        assert response.status_code == 201
        with app.app_context():
            assert Milestone.query.first().monthly_usage == usage

    @pytest.mark.parametrize(("owner", "msx_id"), [
        ("same", None), ("none", None), ("other", None),
        ("same", "22222222-2222-4222-8222-222222222222"),
    ])
    def test_canonical_url_collision(self, client, app, cache_payload, owner, msx_id):
        """Reject URL collisions without duplicating or adopting the existing record."""
        from app.services.msx_api import build_milestone_url

        with app.app_context():
            other = Customer(name="Other", tpid=12345)
            db.session.add(other)
            db.session.flush()
            owner_id = {
                "same": cache_payload["customer_id"], "none": None, "other": other.id,
            }[owner]
            milestone = Milestone(
                url=build_milestone_url(cache_payload["msx_milestone_id"]),
                msx_milestone_id=msx_id, customer_id=owner_id, title="Preserve",
            )
            db.session.add(milestone)
            db.session.commit()
            local_id = milestone.id
        response = client.post('/api/milestones/cache', json=cache_payload)
        assert response.status_code == 409
        assert response.json["success"] is False
        assert response.json["retryable"] is False
        with app.app_context():
            assert Milestone.query.count() == 1
            milestone = db.session.get(Milestone, local_id)
            assert milestone.customer_id == owner_id
            assert milestone.msx_milestone_id == msx_id
            assert milestone.title == "Preserve"

    @pytest.mark.parametrize(("field", "value"), [
        ("customer_id", True), ("customer_id", "42"), ("customer_id", 1.5),
        ("customer_id", 0), ("customer_id", 2**63), ("tpid", None), ("tpid", -1),
        ("tpid", "9999"), ("tpid", False), ("msx_milestone_id", None),
        ("msx_milestone_id", "not-a-guid"),
        ("msx_milestone_id", "00000000-0000-0000-0000-000000000000"),
        ("name", []), ("name", "x" * 501), ("number", 7), ("status", "x" * 51),
        ("opportunity_name", {}), ("workload", "x" * 201),
        ("due_date", 20270115), ("due_date", ""), ("due_date", "2027-02-30"),
        ("monthly_usage", True), ("monthly_usage", "0"),
        ("monthly_usage", float("nan")), ("monthly_usage", float("inf")),
        ("monthly_usage", float("-inf")),
    ])
    def test_invalid_payload(self, client, app, cache_payload, field, value):
        """Reject invalid types and bounds without persisting anything."""
        cache_payload[field] = value
        response = client.post('/api/milestones/cache', json=cache_payload)
        assert response.status_code == 400
        assert response.json["success"] is False
        assert response.json["retryable"] is False
        with app.app_context():
            assert Milestone.query.count() == 0

    @pytest.mark.parametrize(("body", "content_type", "status"), [
        ("{}", "text/plain", 415), ("{", "application/json", 400),
        ("null", "application/json", 400), ("[]", "application/json", 400),
        ("{}", "application/json", 400),
    ])
    def test_invalid_json(self, client, body, content_type, status):
        """All invalid request bodies return explicit JSON errors."""
        response = client.post('/api/milestones/cache', data=body, content_type=content_type)
        assert response.status_code == status
        assert response.json["success"] is False

    @pytest.mark.parametrize(("remote", "host", "origin", "status"), [
        ("192.168.1.10", "localhost", None, 403),
        ("192.168.1.10", "localhost", "http://localhost", 403),
        ("127.0.0.1", "attacker.example", None, 403),
        ("127.0.0.1", "localhost", "https://attacker.example", 403),
        ("127.0.0.1", "localhost", "null", 403),
        ("127.0.0.1", "localhost", "http://localhost:5000", 403),
        ("127.0.0.1", "localhost", "http://localhost", 201),
        ("::1", "[::1]:5000", None, 201),
        ("::ffff:127.0.0.1", "127.0.0.1:5000", None, 201),
    ])
    def test_local_only(self, client, cache_payload, remote, host, origin, status):
        """Connection, Host and Origin must be local; forwarded headers cannot bypass it."""
        headers = {"Host": host, "X-Forwarded-For": "127.0.0.1"}
        if origin:
            headers["Origin"] = origin
        response = client.post(
            '/api/milestones/cache', json=cache_payload, headers=headers,
            environ_overrides={"REMOTE_ADDR": remote},
        )
        assert response.status_code == status

    def test_customer_identity(self, client, app, cache_payload):
        """Reject missing customers and numeric TPID mismatches."""
        response = client.post(
            '/api/milestones/cache', json={**cache_payload, "customer_id": 999999},
        )
        assert response.status_code == 404
        response = client.post('/api/milestones/cache', json={**cache_payload, "tpid": 1})
        assert response.status_code == 409
        with app.app_context():
            assert Milestone.query.count() == 0

    @pytest.mark.parametrize("customerless", [False, True])
    def test_no_customer_reassignment(self, client, app, cache_payload, customerless):
        """Never adopt another customer's milestone or an unowned milestone."""
        with app.app_context():
            other = Customer(name="Other", tpid=12345)
            db.session.add(other)
            db.session.flush()
            owner_id = None if customerless else other.id
            milestone = Milestone(
                msx_milestone_id=cache_payload["msx_milestone_id"].upper(),
                customer_id=owner_id, url="existing", title="Preserve",
            )
            db.session.add(milestone)
            db.session.commit()
        assert client.post('/api/milestones/cache', json=cache_payload).status_code == 409
        with app.app_context():
            assert Milestone.query.count() == 1
            assert Milestone.query.first().customer_id == owner_id
            assert Milestone.query.first().title == "Preserve"

    def test_ambiguous_legacy_duplicates(self, client, app, cache_payload):
        """Existing noncanonical duplicates must not produce an arbitrary identity."""
        with app.app_context():
            guid = cache_payload["msx_milestone_id"]
            for spelling in (guid, "{" + guid + "}"):
                db.session.add(Milestone(
                    msx_milestone_id=spelling, customer_id=cache_payload["customer_id"],
                    url="existing",
                ))
            db.session.commit()
        assert client.post('/api/milestones/cache', json=cache_payload).status_code == 409

    @pytest.mark.parametrize("busy", [False, True])
    def test_failed_commit_and_retry(self, client, app, cache_payload, busy):
        """A failed commit is rolled back, never acknowledged as successful."""
        error = SQLAlchemyError("save failed")
        if busy:
            cause = sqlite3.OperationalError("database is locked")
            cause.sqlite_errorcode = sqlite3.SQLITE_BUSY
            error = OperationalError("insert", {}, cause)
        with patch.object(db.session, 'commit', side_effect=error):
            response = client.post('/api/milestones/cache', json=cache_payload)
        assert response.status_code == (503 if busy else 500)
        assert response.json["success"] is False
        assert response.json["retryable"] is busy
        if busy:
            assert response.headers["Retry-After"] == "1"
        with app.app_context():
            assert Milestone.query.count() == 0
        assert client.post('/api/milestones/cache', json=cache_payload).status_code == 201

    def test_concurrent_create_and_retry(self, app, cache_payload, monkeypatch):
        """Race two requests after both observe absence, then retry without duplicates."""
        from app.services import milestone_cache

        original_find = milestone_cache._find_cached_milestones
        barrier = Barrier(2)
        thread_state = local()

        def racing_find(guid):
            """Synchronize only the first lookup in each competing request."""
            matches = original_find(guid)
            if not getattr(thread_state, "looked_up", False):
                thread_state.looked_up = True
                barrier.wait(timeout=10)
            return matches

        def post():
            """Use independent request and database sessions for competing writers."""
            with app.test_client() as client:
                response = client.post('/api/milestones/cache', json=cache_payload)
                return response.status_code, response.json

        monkeypatch.setattr(milestone_cache, '_find_cached_milestones', racing_find)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: post(), range(2)))
        assert sorted(status for status, _ in results) in ([200, 201], [201, 503])
        monkeypatch.setattr(milestone_cache, '_find_cached_milestones', original_find)
        status, result = post()
        assert status == 200
        assert result["created"] is False
        with app.app_context():
            assert Milestone.query.count() == 1
            assert Milestone.query.first().id == result["local_milestone_id"]


class TestMilestoneModel:
    """Tests for Milestone model."""
    
    def test_milestone_creation(self, app, db_session, sample_user):
        """Test creating a milestone."""
        milestone = Milestone(
            url='https://msxsalesplatform.dynamics.com/milestone/123',
            title='Q2 Deployment',
        )
        db_session.add(milestone)
        db_session.commit()
        
        assert milestone.id is not None
        assert milestone.url == 'https://msxsalesplatform.dynamics.com/milestone/123'
        assert milestone.title == 'Q2 Deployment'
        assert milestone.display_text == 'Q2 Deployment'
    
    def test_milestone_display_text_without_title(self, app, db_session, sample_user):
        """Test display_text property when title is None."""
        milestone = Milestone(
            url='https://msxsalesplatform.dynamics.com/milestone/456',
            title=None,
        )
        db_session.add(milestone)
        db_session.commit()
        
        assert milestone.display_text == 'View in MSX'
    
    def test_milestone_unique_msx_id(self, app, db_session, sample_user):
        """Test that MSX milestone IDs must be unique."""
        milestone1 = Milestone(
            msx_milestone_id='12345678-1234-1234-1234-123456789abc',
            url='https://msxsalesplatform.dynamics.com/milestone/unique',
        )
        db_session.add(milestone1)
        db_session.commit()
        
        milestone2 = Milestone(
            msx_milestone_id='12345678-1234-1234-1234-123456789abc',
            url='https://msxsalesplatform.dynamics.com/milestone/unique2',
        )
        db_session.add(milestone2)
        
        with pytest.raises(Exception):  # SQLAlchemy IntegrityError
            db_session.commit()


class TestMilestoneCRUD:
    """Tests for milestone CRUD operations."""
    
    def test_milestones_list_empty(self, client, app):
        """Test milestones list page with no milestones."""
        response = client.get('/milestones')
        assert response.status_code == 200
        assert b'No milestones yet' in response.data
    
    def test_milestones_list_with_data(
        self, client, app, db_session, sample_user, sample_customer,
    ):
        """Test milestones list page with milestones."""
        milestone = Milestone(
            url='https://example.com/milestone/1',
            title='Test Milestone',
            customer_id=sample_customer.id,
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.get('/milestones')
        assert response.status_code == 200
        assert b'Test Milestone' in response.data
    
    def test_milestone_create_form(self, client, app):
        """Test milestone create form loads."""
        response = client.get('/milestone/new')
        assert response.status_code == 200
        assert b'New Milestone' in response.data
        assert b'MSX URL' in response.data
    
    def test_milestone_create_post(self, client, app, db_session):
        """Test creating a milestone via POST."""
        response = client.post('/milestone/new', data={
            'url': 'https://msxsalesplatform.dynamics.com/test/123',
            'title': 'New Test Milestone'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Milestone created successfully' in response.data
        
        # Verify in database
        milestone = Milestone.query.filter_by(url='https://msxsalesplatform.dynamics.com/test/123').first()
        assert milestone is not None
        assert milestone.title == 'New Test Milestone'
    
    def test_milestone_create_requires_url(self, client, app):
        """Test that URL is required."""
        response = client.post('/milestone/new', data={
            'url': '',
            'title': 'No URL Milestone'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'URL is required' in response.data
    
    def test_milestone_view(self, client, app, db_session, sample_user):
        """Test viewing a milestone."""
        milestone = Milestone(
            url='https://example.com/view/test',
            title='View Test',
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.get(f'/milestone/{milestone.id}')
        assert response.status_code == 200
        assert b'View Test' in response.data
        assert b'https://example.com/view/test' in response.data
    
    def test_milestone_edit_form(self, client, app, db_session, sample_user):
        """Test milestone edit form loads."""
        milestone = Milestone(
            url='https://example.com/edit/test',
            title='Edit Test',
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.get(f'/milestone/{milestone.id}/edit')
        assert response.status_code == 200
        assert b'Edit Milestone' in response.data
        assert b'Edit Test' in response.data
    
    def test_milestone_edit_post(self, client, app, db_session, sample_user):
        """Test editing a milestone via POST."""
        milestone = Milestone(
            url='https://example.com/original',
            title='Original Title',
        )
        db_session.add(milestone)
        db_session.commit()
        
        response = client.post(f'/milestone/{milestone.id}/edit', data={
            'url': 'https://example.com/updated',
            'title': 'Updated Title'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Milestone updated successfully' in response.data
        
        # Verify in database
        db_session.refresh(milestone)
        assert milestone.url == 'https://example.com/updated'
        assert milestone.title == 'Updated Title'
    
    def test_milestone_delete(self, client, app, db_session, sample_user):
        """Test deleting a milestone."""
        milestone = Milestone(
            url='https://example.com/delete/test',
            title='Delete Me',
        )
        db_session.add(milestone)
        db_session.commit()
        milestone_id = milestone.id
        
        response = client.post(f'/milestone/{milestone_id}/delete', follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Milestone deleted successfully' in response.data
        
        # Verify deletion
        deleted = db.session.get(Milestone, milestone_id)
        assert deleted is None

    def test_milestone_delete_blocked_when_linked_to_note(self, client, app, db_session, sample_user, sample_customer):
        """Test that deleting a milestone linked to a note is blocked."""
        milestone = Milestone(
            url='https://example.com/linked/test',
            title='Linked Milestone',
        )
        db_session.add(milestone)
        db_session.flush()

        note = Note(
            customer_id=sample_customer.id,
            call_date=datetime(2026, 2, 25),
            content='<p>Test call</p>',
        )
        note.milestones = [milestone]
        db_session.add(note)
        db_session.commit()
        milestone_id = milestone.id

        response = client.post(f'/milestone/{milestone_id}/delete', follow_redirects=True)

        assert response.status_code == 200
        assert b'Cannot delete this milestone' in response.data

        # Verify milestone still exists
        still_exists = db.session.get(Milestone, milestone_id)
        assert still_exists is not None


class TestNoteMilestoneIntegration:
    """Tests for milestone integration with notes."""
    
    def test_note_with_msx_milestone_creates_milestone(self, client, app, db_session, sample_customer):
        """Test that adding MSX milestone to note creates a new milestone."""
        msx_milestone_id = 'test-msx-id-12345678'
        milestone_url = 'https://msxsalesplatform.dynamics.com/new/milestone'
        
        response = client.post(f'/note/new?customer_id={sample_customer.id}', data={
            'customer_id': sample_customer.id,
            'call_date': '2026-01-30',
            'content': '<p>Test note with milestone</p>',
            'milestone_msx_id': msx_milestone_id,
            'milestone_url': milestone_url,
            'milestone_name': 'Test Milestone',
            'milestone_number': 'MS-001',
            'milestone_status': 'On Track',
            'milestone_status_code': '1',
            'milestone_opportunity_name': 'Test Opportunity'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify milestone was created
        with app.app_context():
            from app.models import Milestone, Note
            milestone = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).first()
            assert milestone is not None
            assert milestone.url == milestone_url
            assert milestone.msx_status == 'On Track'
            
            # Verify note is linked to milestone
            note = Note.query.filter_by(customer_id=sample_customer.id).first()
            assert note is not None
            assert milestone in note.milestones
    
    def test_note_with_existing_msx_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that adding existing MSX milestone links to existing milestone."""
        msx_milestone_id = 'existing-msx-id-12345'
        
        # Create existing milestone
        with app.app_context():
            from app.models import db, Milestone, User, Note
            test_user = User.query.first()
            
            existing_milestone = Milestone(
                msx_milestone_id=msx_milestone_id,
                url='https://msxsalesplatform.dynamics.com/existing/milestone',
                msx_status='On Track',
            )
            db.session.add(existing_milestone)
            db.session.commit()
            existing_id = existing_milestone.id
        
        response = client.post(f'/note/new?customer_id={sample_customer.id}', data={
            'customer_id': sample_customer.id,
            'call_date': '2026-01-30',
            'content': '<p>Test note linking to existing milestone</p>',
            'milestone_msx_id': msx_milestone_id,
            'milestone_url': 'https://msxsalesplatform.dynamics.com/existing/milestone',
            'milestone_name': 'Existing Milestone',
            'milestone_status': 'Blocked',
            'milestone_status_code': '3'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Should not create duplicate milestone
        with app.app_context():
            from app.models import Milestone, Note
            milestones = Milestone.query.filter_by(msx_milestone_id=msx_milestone_id).all()
            assert len(milestones) == 1
            
            # Milestone should be updated with new status
            milestone = milestones[0]
            assert milestone.msx_status == 'Blocked'
            
            # Note should be linked to existing milestone
            note = Note.query.filter_by(customer_id=sample_customer.id).first()
            assert len([m for m in note.milestones if m.id == existing_id]) == 1
    
    def test_note_view_shows_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that note view shows associated milestone."""
        from datetime import date
        
        with app.app_context():
            from app.models import db, Milestone, Note, User
            test_user = User.query.first()
            
            milestone = Milestone(
                url='https://example.com/show/milestone',
                title='Visible Milestone',
            )
            db.session.add(milestone)
            
            note = Note(
                customer_id=sample_customer.id,
                call_date=date(2026, 1, 30),
                content='<p>Test content</p>',
            )
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.commit()
            note_id = note.id
        
        response = client.get(f'/note/{note_id}')
        assert response.status_code == 200
        assert b'Visible Milestone' in response.data
    
    def test_note_edit_updates_milestone(self, client, app, db_session, sample_customer, sample_user):
        """Test that editing note can change milestone."""
        from datetime import date
        
        with app.app_context():
            from app.models import db, Milestone, Note, User
            test_user = User.query.first()
            
            # Create note with initial milestone
            old_milestone = Milestone(
                msx_milestone_id='old-msx-id-12345',
                url='https://example.com/old/milestone',
            )
            db.session.add(old_milestone)
            
            note = Note(
                customer_id=sample_customer.id,
                call_date=date(2026, 1, 30),
                content='<p>Original content</p>',
            )
            note.milestones.append(old_milestone)
            db.session.add(note)
            db.session.commit()
            note_id = note.id
        
        # Edit with new MSX milestone
        response = client.post(f'/note/{note_id}/edit', data={
            'customer_id': sample_customer.id,
            'call_date': '2026-01-30',
            'content': '<p>Updated content</p>',
            'milestone_msx_id': 'new-msx-id-67890',
            'milestone_url': 'https://example.com/new/milestone',
            'milestone_name': 'New Milestone',
            'milestone_status': 'On Track'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Verify new milestone was created and linked
        with app.app_context():
            from app.models import db, Note
            note = db.session.get(Note, note_id)
            assert len(note.milestones) == 1
            assert note.milestones[0].msx_milestone_id == 'new-msx-id-67890'
            assert note.milestones[0].url == 'https://example.com/new/milestone'


class TestMilestoneAPI:
    """Tests for milestone API endpoints."""
    
    def test_find_or_create_milestone_creates_new(self, client, app, db_session):
        """Test API creates new milestone when URL doesn't exist."""
        response = client.post('/api/milestones/find-or-create',
            json={'url': 'https://api.test/new/milestone'},
            content_type='application/json'
        )
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['url'] == 'https://api.test/new/milestone'
        assert data['id'] is not None
    
    def test_find_or_create_milestone_finds_existing(self, client, app, db_session, sample_user):
        """Test API finds existing milestone."""
        with app.app_context():
            from app.models import db, Milestone, User
            test_user = User.query.first()
            
            existing = Milestone(
                url='https://api.test/existing',
                title='Existing',
            )
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id
        
        response = client.post('/api/milestones/find-or-create',
            json={'url': 'https://api.test/existing'},
            content_type='application/json'
        )
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['id'] == existing_id
        assert data['title'] == 'Existing'
    
    def test_find_or_create_milestone_requires_url(self, client, app):
        """Test API returns error when URL is missing."""
        response = client.post('/api/milestones/find-or-create',
            json={'url': ''},
            content_type='application/json'
        )
        
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data


class TestMilestoneViewOverhaul:
    """Tests for the overhauled milestone view page."""

    def _create_milestone(self, app, **kwargs):
        """Helper to create a milestone with defaults."""
        with app.app_context():
            defaults = {
                'url': 'https://msxsalesplatform.dynamics.com/milestone/test',
                'title': 'Test Milestone',
                'msx_milestone_id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                'msx_status': 'On Track',
                'dollar_value': 50000,
                'monthly_usage': 5000,
                'workload': 'Azure Synapse',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    def test_view_shows_engagements_in_nav(self, client, app, db_session, sample_user, sample_customer):
        """Linked engagements retain their destination in the details navigation."""
        from app.models import Engagement
        with app.app_context():
            ms = Milestone(
                url='https://test/ms', title='Nav Test',
                msx_milestone_id='nav-test-1234',
                customer_id=sample_customer.id,
            )
            db.session.add(ms)
            db.session.flush()
            eng = Engagement(
                customer_id=sample_customer.id,
                title='Fabric Migration',
                status='Active',
            )
            db.session.add(eng)
            db.session.flush()
            eng.milestones.append(ms)
            db.session.commit()
            ms_id = ms.id
            engagement_id = eng.id

        resp = client.get(f'/milestone/{ms_id}')
        assert resp.status_code == 200
        assert b'Fabric Migration' in resp.data
        soup = BeautifulSoup(resp.data, "html.parser")
        link = soup.select_one(
            f'#milestoneNavigationLinks a[href="/engagement/{engagement_id}"]'
        )
        assert link.get_text(strip=True) == "Fabric Migration"

    def test_view_offers_engagement_creation_when_unlinked(
        self, client, app, db_session, sample_user, sample_customer
    ):
        """Customer milestones without engagements should expose creation flow."""
        ms_id = self._create_milestone(
            app,
            customer_id=sample_customer.id,
            title='Create Engagement Milestone',
        )

        resp = client.get(f'/milestone/{ms_id}')
        html = resp.data.decode()

        assert resp.status_code == 200
        assert 'No engagement linked to this milestone.' in html
        assert 'data-sb-ms-action="engagement"' in html
        assert 'id="milestoneEngagementModal"' in html
        assert 'Technical/Business Problem' in html
        assert f'milestone_ids: [{ms_id}]' in html

    def test_view_hides_engagement_creation_when_linked(
        self, client, app, db_session, sample_user, sample_customer
    ):
        """Linked milestones should show navigation without duplicate create CTA."""
        from app.models import Engagement
        with app.app_context():
            milestone = Milestone(
                url='https://test/linked-ms',
                title='Already Linked Milestone',
                msx_milestone_id='already-linked-milestone',
                customer_id=sample_customer.id,
            )
            engagement = Engagement(
                customer_id=sample_customer.id,
                title='Existing Linked Engagement',
                status='Active',
            )
            engagement.milestones.append(milestone)
            db.session.add(engagement)
            db.session.commit()
            milestone_id = milestone.id

        resp = client.get(f'/milestone/{milestone_id}')
        html = resp.data.decode()

        assert 'Existing Linked Engagement' in html
        assert 'No engagement linked to this milestone.' not in html
        assert 'id="milestoneEngagementModal"' not in html

    def test_view_shows_cached_comments(self, client, app, db_session, sample_user):
        """Cached comments render immediately in the template."""
        comments = [
            {"userId": "abc", "displayName": "Jane Doe",
             "modifiedOn": "2026-03-17T10:00:00Z",
             "comment": "Looking good on track"},
        ]
        ms_id = self._create_milestone(
            app, cached_comments_json=json.dumps(comments),
        )
        resp = client.get(f'/milestone/{ms_id}')
        assert resp.status_code == 200
        assert b'Jane Doe' in resp.data
        assert b'Looking good on track' in resp.data

    def test_view_no_msx_id_hides_spinners(self, client, app, db_session, sample_user):
        """Milestone without MSX ID should not show loading spinners."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.get(f'/milestone/{ms_id}')
        assert resp.status_code == 200
        # Check for actual spinner div elements, not just JS references
        assert b'id="detailsSpinner"' not in resp.data
        assert b'id="commentsSpinner"' not in resp.data

    def test_tasks_in_left_column(self, client, app, db_session, sample_user):
        """Tasks remain in the details rail rather than the forecast reading pane."""
        ms_id = self._create_milestone(app)
        resp = client.get(f'/milestone/{ms_id}')
        soup = BeautifulSoup(resp.data, "html.parser")
        assert soup.select_one('.sb-ms-context > section[aria-label="Milestone tasks"]')
        assert not soup.select('.sb-ms-main [aria-label="Milestone tasks"]')

    def test_notes_between_details_and_tasks(self, client, app, db_session, sample_user):
        """Associated notes keep their original position between details and tasks."""
        ms_id = self._create_milestone(app)
        resp = client.get(f'/milestone/{ms_id}')
        soup = BeautifulSoup(resp.data, "html.parser")
        assert [
            heading.get_text(strip=True)
            for heading in soup.select(".sb-ms-context .sb-ms-heading")
        ] == ["Details", "Associated Notes", "Tasks"]


class TestMilestoneMsxDetailsAPI:
    """Tests for the lazy-load MSX details endpoint."""

    def _create_milestone(self, app, **kwargs):
        with app.app_context():
            defaults = {
                'url': 'https://test/ms',
                'title': 'API Test',
                'msx_milestone_id': 'aaaaaaaa-1111-2222-3333-444444444444',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    @patch('app.services.msx_api.get_milestone_details')
    def test_msx_details_success_caches_data(self, mock_get, client, app, db_session, sample_user):
        """Successful MSX fetch caches comments and details_fetched_at."""
        ms_id = self._create_milestone(app)
        mock_get.return_value = {
            'success': True,
            'milestone': {
                'title': 'Updated Title',
                'milestone_number': '7-999',
                'msx_status': 'At Risk',
                'msx_status_code': 861980001,
                'due_date': '2026-06-30T00:00:00Z',
                'dollar_value': 75000,
                'monthly_usage': 8000,
                'workload': 'Azure Data Factory',
                'comments': [
                    {'userId': 'u1', 'displayName': 'Bob',
                     'modifiedOn': '2026-03-17', 'comment': 'Test comment'},
                ],
            },
        }
        resp = client.get(f'/api/milestone/{ms_id}/msx-details')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['milestone']['msx_status'] == 'At Risk'

        with app.app_context():
            ms = db.session.get(Milestone, ms_id)
            assert ms.title == 'Updated Title'
            assert ms.msx_status == 'At Risk'
            assert ms.cached_comments_json is not None
            assert ms.details_fetched_at is not None

    @patch('app.services.msx_api.get_milestone_details')
    def test_msx_details_failure_returns_error(self, mock_get, client, app, db_session, sample_user):
        """Failed MSX fetch returns error JSON."""
        ms_id = self._create_milestone(app)
        mock_get.return_value = {
            'success': False,
            'error': 'Connection timeout',
            'vpn_blocked': False,
        }
        resp = client.get(f'/api/milestone/{ms_id}/msx-details')
        data = resp.get_json()
        assert data['success'] is False
        assert 'Connection timeout' in data['error']

    def test_msx_details_no_msx_id(self, client, app, db_session, sample_user):
        """Milestone without MSX ID returns error."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.get(f'/api/milestone/{ms_id}/msx-details')
        data = resp.get_json()
        assert data['success'] is False
        assert 'No MSX ID' in data['error']


class TestMilestoneCommentAPI:
    """Tests for the milestone comment endpoints."""

    def _create_milestone(self, app, **kwargs):
        with app.app_context():
            defaults = {
                'url': 'https://test/ms',
                'title': 'Comment Test',
                'msx_milestone_id': 'cccccccc-1111-2222-3333-444444444444',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    @patch('app.services.msx_api.add_milestone_comment')
    def test_post_comment_form(self, mock_add, client, app, db_session, sample_user):
        """Form-based comment post redirects on success."""
        ms_id = self._create_milestone(app)
        mock_add.return_value = {'success': True, 'comment_count': 3}
        resp = client.post(
            f'/milestone/{ms_id}/comment',
            data={'comment': 'Great progress'},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        mock_add.assert_called_once()

    @patch('app.services.msx_api.add_milestone_comment')
    def test_post_comment_api(self, mock_add, client, app, db_session, sample_user):
        """JSON API comment post returns success."""
        ms_id = self._create_milestone(app)
        mock_add.return_value = {'success': True, 'comment_count': 5}
        resp = client.post(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': 'Looking great'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_post_comment_api_empty(self, client, app, db_session, sample_user):
        """Empty comment returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.post(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': '  '},
        )
        assert resp.status_code == 400

    def test_post_comment_no_msx_id(self, client, app, db_session, sample_user):
        """Milestone without MSX ID returns error for API comment."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.post(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': 'test'},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'No MSX ID' in data['error']


class TestMilestoneCommentEditDelete:
    """Tests for editing and deleting milestone comments."""

    def _create_milestone(self, app, **kwargs):
        with app.app_context():
            defaults = {
                'url': 'https://test/ms-ed',
                'title': 'Edit Delete Test',
                'msx_milestone_id': 'dddddddd-1111-2222-3333-444444444444',
            }
            defaults.update(kwargs)
            ms = Milestone(**defaults)
            db.session.add(ms)
            db.session.commit()
            return ms.id

    @patch('app.services.msx_api.edit_milestone_comment')
    def test_edit_comment_success(self, mock_edit, client, app, db_session, sample_user):
        """PUT comment returns success when MSX edit succeeds."""
        ms_id = self._create_milestone(app)
        mock_edit.return_value = {'success': True}
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={
                'modifiedOn': '2026-03-01T10:00:00.000Z',
                'userId': 'Alex via Sales Buddy',
                'comment': 'Updated text',
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        mock_edit.assert_called_once()

    def test_edit_comment_empty(self, client, app, db_session, sample_user):
        """PUT with empty comment returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x', 'userId': 'y', 'comment': '  '},
        )
        assert resp.status_code == 400

    def test_edit_comment_missing_id(self, client, app, db_session, sample_user):
        """PUT without modifiedOn or userId returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={'comment': 'hello'},
        )
        assert resp.status_code == 400
        assert 'Missing comment identifier' in resp.get_json()['error']

    def test_edit_comment_no_msx_id(self, client, app, db_session, sample_user):
        """PUT on milestone without MSX ID returns 400."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.put(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x', 'userId': 'y', 'comment': 'hello'},
        )
        assert resp.status_code == 400
        assert 'No MSX ID' in resp.get_json()['error']

    @patch('app.services.msx_api.delete_milestone_comment')
    def test_delete_comment_success(self, mock_delete, client, app, db_session, sample_user):
        """DELETE comment returns success when MSX delete succeeds."""
        ms_id = self._create_milestone(app)
        mock_delete.return_value = {'success': True}
        resp = client.delete(
            f'/api/milestone/{ms_id}/comment',
            json={
                'modifiedOn': '2026-03-01T10:00:00.000Z',
                'userId': 'Alex via Sales Buddy',
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        mock_delete.assert_called_once()

    def test_delete_comment_missing_id(self, client, app, db_session, sample_user):
        """DELETE without modifiedOn or userId returns 400."""
        ms_id = self._create_milestone(app)
        resp = client.delete(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x'},
        )
        assert resp.status_code == 400
        assert 'Missing comment identifier' in resp.get_json()['error']

    def test_delete_comment_no_msx_id(self, client, app, db_session, sample_user):
        """DELETE on milestone without MSX ID returns 400."""
        ms_id = self._create_milestone(app, msx_milestone_id=None)
        resp = client.delete(
            f'/api/milestone/{ms_id}/comment',
            json={'modifiedOn': 'x', 'userId': 'y'},
        )
        assert resp.status_code == 400
        assert 'No MSX ID' in resp.get_json()['error']
