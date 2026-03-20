"""
Tests for milestone comment tracking via MSX (Issue #72).

Tests cover:
- Upsert logic: ref-tag matching, create vs update, multi-user support
- Engagement story: template assembly, pinning, update-in-place
- Call summary: AI summarization, fallback, existing comment context
- Background execution: notification queue for failures
- Edge cases: no milestones, no MSX ID, API failures
"""
import pytest
from datetime import datetime, date, timezone
from unittest.mock import patch, MagicMock
import os

from app.models import db, Customer, Note, Milestone, Engagement, Topic
from app.services.milestone_tracking import (
    track_note_on_milestones,
    track_engagement_on_milestones,
    _build_engagement_story,
    _build_engagement_story_template,
    _ai_compose_story,
    _add_footer,
    _strip_html,
    _build_note_fallback,
    drain_notifications,
    _notify_error,
)


# ── Unit tests for helpers ──────────────────────────────────────────────────


class TestHelpers:
    """Test helper functions in milestone_tracking."""

    def test_strip_html_removes_tags(self):
        assert _strip_html('<p>Hello <b>world</b></p>') == 'Hello world'

    def test_strip_html_handles_none(self):
        assert _strip_html(None) == ''

    def test_add_footer_appends_ref_tag(self):
        result = _add_footer("Some content", "note-42")
        assert result.startswith("Some content\n")
        assert "· note-42 ·" in result

    def test_build_note_fallback(self):
        result = _build_note_fallback("SQL MI, Migration")
        assert "Topics: SQL MI, Migration" in result

    def test_build_note_fallback_no_topics(self):
        result = _build_note_fallback("None")
        assert "pending" in result.lower()


# ── Engagement story template tests ─────────────────────────────────────────


class TestBuildEngagementStory:
    """Test engagement story comment assembly."""

    @patch('app.services.milestone_tracking._ai_compose_story', return_value=None)
    def test_full_story_with_all_fields(self, mock_ai, app):
        """All 6 narrative fields produce a complete story (template fallback)."""
        with app.app_context():
            customer = Customer(name='Story Corp', tpid=8000)
            db.session.add(customer)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id,
                title='Oracle Migration',
                status='Active',
                key_individuals='John Smith (DBA), Sarah Chen (VP)',
                technical_problem='3 Oracle instances need migration to Azure SQL MI.',
                business_impact='$2M annual licensing savings plus improved DR.',
                solution_resources='SQL MI compatibility assessment + migration planning.',
                estimated_acr=45000,
                target_date=date(2026, 6, 1),
            )
            db.session.add(eng)
            db.session.flush()

            story = _build_engagement_story(eng)

            assert 'Engagement Overview: Oracle Migration [Active]' in story
            assert "I've been working with John Smith (DBA), Sarah Chen (VP)" in story
            assert 'They have run into 3 Oracle instances' in story
            assert "It's impacting $2M annual licensing" in story
            assert 'We are addressing the opportunity with SQL MI compatibility' in story
            assert 'This will result in $45,000/mo by Jun 2026.' in story

    @patch('app.services.milestone_tracking._ai_compose_story', return_value=None)
    def test_minimal_story_with_title_only(self, mock_ai, app):
        """Story works with just title and status."""
        with app.app_context():
            customer = Customer(name='Min Corp', tpid=8001)
            db.session.add(customer)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id,
                title='Discovery Phase',
                status='Active',
            )
            db.session.add(eng)
            db.session.flush()

            story = _build_engagement_story(eng)
            assert 'Engagement Overview: Discovery Phase [Active]' in story
            # Should not crash or include "None"
            assert 'None' not in story

    @patch('app.services.milestone_tracking._ai_compose_story', return_value=None)
    def test_story_with_acr_no_date(self, mock_ai, app):
        """Story includes ACR without date."""
        with app.app_context():
            customer = Customer(name='ACR Corp', tpid=8002)
            db.session.add(customer)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id,
                title='ACR Test',
                status='Active',
                estimated_acr=10000,
            )
            db.session.add(eng)
            db.session.flush()

            story = _build_engagement_story(eng)
            assert 'This will result in $10,000/mo.' in story

    def test_ai_compose_success_uses_ai_text(self, app):
        """When AI compose succeeds, _build_engagement_story uses the AI text."""
        with app.app_context():
            customer = Customer(name='AI Corp', tpid=8010)
            db.session.add(customer)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id,
                title='AI Test',
                status='Active',
                key_individuals='Jane Doe (CTO)',
                technical_problem='Legacy migration needed.',
            )
            db.session.add(eng)
            db.session.flush()

            ai_text = (
                "Engagement Overview: AI Test [Active]\n\n"
                "Working with Jane Doe, CTO, on a legacy migration. "
                "The customer needs to modernize their infrastructure."
            )
            with patch(
                'app.services.milestone_tracking._ai_compose_story',
                return_value=ai_text,
            ):
                story = _build_engagement_story(eng)
            assert story == ai_text

    def test_ai_compose_failure_falls_back_to_template(self, app):
        """When AI compose fails, _build_engagement_story uses the template."""
        with app.app_context():
            customer = Customer(name='Fallback Corp', tpid=8011)
            db.session.add(customer)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id,
                title='Fallback Test',
                status='Active',
                key_individuals='Bob (PM)',
            )
            db.session.add(eng)
            db.session.flush()

            with patch(
                'app.services.milestone_tracking._ai_compose_story',
                return_value=None,
            ):
                story = _build_engagement_story(eng)
            assert 'Engagement Overview: Fallback Test [Active]' in story
            assert "I've been working with Bob (PM)" in story

    @patch('app.gateway_client.gateway_call')
    def test_ai_compose_story_calls_gateway(self, mock_gw, app):
        """_ai_compose_story calls the correct gateway endpoint."""
        mock_gw.return_value = {
            "success": True,
            "story_text": "A nicely composed story about the engagement.",
        }
        with app.app_context():
            customer = Customer(name='GW Corp', tpid=8012)
            db.session.add(customer)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id,
                title='Gateway Test',
                status='Active',
                key_individuals='Alice (Director)',
                technical_problem='Need AKS migration.',
            )
            db.session.add(eng)
            db.session.flush()

            result = _ai_compose_story(eng)

        assert result is not None
        assert 'Engagement Overview: Gateway Test [Active]' in result
        assert 'A nicely composed story' in result
        mock_gw.assert_called_once()
        call_args = mock_gw.call_args
        assert call_args[0][0] == '/v1/compose-engagement-story'
        assert call_args[0][1]['title'] == 'Gateway Test'
        assert 'Alice (Director)' in call_args[0][1]['fields']['key_individuals']

    @patch('app.gateway_client.gateway_call', side_effect=Exception('timeout'))
    def test_ai_compose_story_returns_none_on_failure(self, mock_gw, app):
        """_ai_compose_story returns None when gateway fails."""
        with app.app_context():
            customer = Customer(name='Fail Corp', tpid=8013)
            db.session.add(customer)
            db.session.flush()

            eng = Engagement(
                customer_id=customer.id,
                title='Fail Test',
                status='Active',
                technical_problem='Something broke.',
            )
            db.session.add(eng)
            db.session.flush()

            result = _ai_compose_story(eng)
        assert result is None


# ── Upsert logic tests (msx_api.py) ─────────────────────────────────────────


class TestUpsertMilestoneComment:
    """Test the upsert_milestone_comment function in msx_api."""

    @pytest.fixture(autouse=True)
    def _enable_writeback(self, monkeypatch):
        monkeypatch.delenv('MSX_WRITEBACK_DISABLED', raising=False)

    def test_creates_new_comment_when_none_exists(self, app):
        """First comment on a milestone creates a new entry."""
        with app.app_context():
            import json
            with patch('app.services.msx_api.get_milestone_comments') as mock_read, \
                 patch('app.services.msx_api.get_msx_user_display_name') as mock_name, \
                 patch('app.services.msx_api._msx_request') as mock_req:
                mock_read.return_value = {"success": True, "comments": []}
                mock_name.return_value = "Alex Blaine"
                mock_resp = MagicMock()
                mock_resp.status_code = 204
                mock_req.return_value = mock_resp

                from app.services.msx_api import upsert_milestone_comment
                result = upsert_milestone_comment(
                    "ms-guid-1", "Test content\n\n· note-1 ·", "note-1",
                )

                assert result["success"] is True
                assert result["action"] == "created"
                assert result["comment_count"] == 1

                # Verify PATCH payload
                patch_call = mock_req.call_args
                payload = patch_call[1]["json_data"]
                comments = json.loads(payload["msp_forecastcommentsjsonfield"])
                assert len(comments) == 1
                assert comments[0]["userId"] == "Alex Blaine via Sales Buddy"
                assert "· note-1 ·" in comments[0]["comment"]

    def test_updates_existing_comment_by_ref_tag(self, app):
        """When a comment with matching ref tag exists, it's updated in-place."""
        with app.app_context():
            import json
            existing = [
                {"userId": "Some Manager", "modifiedOn": "2026-01-01T00:00:00Z",
                 "comment": "Manual comment from MSX UI"},
                {"userId": "Alex Blaine via Sales Buddy", "modifiedOn": "2026-03-01T00:00:00Z",
                 "comment": "Old summary\n\n· note-5 ·"},
            ]
            with patch('app.services.msx_api.get_milestone_comments') as mock_read, \
                 patch('app.services.msx_api.get_msx_user_display_name') as mock_name, \
                 patch('app.services.msx_api._msx_request') as mock_req:
                mock_read.return_value = {"success": True, "comments": existing}
                mock_name.return_value = "Alex Blaine"
                mock_resp = MagicMock()
                mock_resp.status_code = 204
                mock_req.return_value = mock_resp

                from app.services.msx_api import upsert_milestone_comment
                result = upsert_milestone_comment(
                    "ms-guid-2", "Updated summary\n\n· note-5 ·", "note-5",
                )

                assert result["success"] is True
                assert result["action"] == "updated"
                assert result["comment_count"] == 2  # didn't add a third

                payload = mock_req.call_args[1]["json_data"]
                comments = json.loads(payload["msp_forecastcommentsjsonfield"])
                # First comment untouched
                assert comments[0]["userId"] == "Some Manager"
                # Second updated
                assert "Updated summary" in comments[1]["comment"]
                assert comments[1]["userId"] == "Alex Blaine via Sales Buddy"

    def test_returns_existing_comments_for_ai_context(self, app):
        """Upsert returns other comment texts for AI context."""
        with app.app_context():
            existing = [
                {"userId": "Manager", "modifiedOn": "2026-01-01T00:00:00Z",
                 "comment": "FY26 review note"},
                {"userId": "Alex Blaine via Sales Buddy", "modifiedOn": "2026-03-01T00:00:00Z",
                 "comment": "Our comment\n\n· note-10 ·"},
                {"userId": "Teammate", "modifiedOn": "2026-02-01T00:00:00Z",
                 "comment": "PoC update from field team"},
            ]
            with patch('app.services.msx_api.get_milestone_comments') as mock_read, \
                 patch('app.services.msx_api.get_msx_user_display_name') as mock_name, \
                 patch('app.services.msx_api._msx_request') as mock_req:
                mock_read.return_value = {"success": True, "comments": existing}
                mock_name.return_value = "Alex Blaine"
                mock_resp = MagicMock()
                mock_resp.status_code = 204
                mock_req.return_value = mock_resp

                from app.services.msx_api import upsert_milestone_comment
                result = upsert_milestone_comment(
                    "ms-guid-3", "New content\n\n· note-10 ·", "note-10",
                )

                # Should include comments 0 and 2 but not our own (index 1)
                assert "FY26 review note" in result["existing_comments"]
                assert "PoC update from field team" in result["existing_comments"]
                assert len(result["existing_comments"]) == 2

    def test_multi_user_same_milestone(self, app):
        """Two Sales Buddy users each maintain separate comments for same note ID."""
        with app.app_context():
            import json
            existing = [
                {"userId": "Alice Smith via Sales Buddy", "modifiedOn": "2026-03-01T00:00:00Z",
                 "comment": "Alice's summary\n\n· note-7 ·"},
            ]
            with patch('app.services.msx_api.get_milestone_comments') as mock_read, \
                 patch('app.services.msx_api.get_msx_user_display_name') as mock_name, \
                 patch('app.services.msx_api._msx_request') as mock_req:
                mock_read.return_value = {"success": True, "comments": existing}
                mock_name.return_value = "Bob Jones"  # Different user
                mock_resp = MagicMock()
                mock_resp.status_code = 204
                mock_req.return_value = mock_resp

                from app.services.msx_api import upsert_milestone_comment
                result = upsert_milestone_comment(
                    "ms-guid-4", "Bob's summary\n\n· note-7 ·", "note-7",
                )

                # Bob's note-7 ref tag doesn't match Alice's userId,
                # so Bob should CREATE a new comment (not update Alice's)
                assert result["action"] == "created"
                assert result["comment_count"] == 2

                payload = mock_req.call_args[1]["json_data"]
                comments = json.loads(payload["msp_forecastcommentsjsonfield"])
                user_ids = [c["userId"] for c in comments]
                assert "Alice Smith via Sales Buddy" in user_ids
                assert "Bob Jones via Sales Buddy" in user_ids

    def test_pin_to_top_uses_future_date(self, app):
        """Pin mode sets modifiedOn to 2099-01-01."""
        with app.app_context():
            import json
            with patch('app.services.msx_api.get_milestone_comments') as mock_read, \
                 patch('app.services.msx_api.get_msx_user_display_name') as mock_name, \
                 patch('app.services.msx_api._msx_request') as mock_req:
                mock_read.return_value = {"success": True, "comments": []}
                mock_name.return_value = "Alex Blaine"
                mock_resp = MagicMock()
                mock_resp.status_code = 204
                mock_req.return_value = mock_resp

                from app.services.msx_api import upsert_milestone_comment
                result = upsert_milestone_comment(
                    "ms-guid-5", "Pinned story\n\n· eng-3 ·", "eng-3",
                    pin_to_top=True,
                )

                payload = mock_req.call_args[1]["json_data"]
                comments = json.loads(payload["msp_forecastcommentsjsonfield"])
                assert comments[0]["modifiedOn"] == "2099-01-01T00:00:00.000Z"


# ── Note tracking integration tests ─────────────────────────────────────────


class TestTrackNoteOnMilestones:
    """Test track_note_on_milestones with the new upsert + AI flow."""

    def test_posts_with_ai_summary(self, app):
        """When AI is available, uses AI-generated summary."""
        with app.app_context():
            customer = Customer(name='AI Corp', tpid=9500)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/ai1',
                title='AI Test MS',
                customer_id=customer.id,
                msx_milestone_id='ai-guid-1',
            )
            db.session.add(milestone)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='<p>Discussed SQL MI migration. 2 of 3 DBs ready.</p>',
            )
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.flush()

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert, \
                 patch('app.services.milestone_tracking._ai_summarize_note') as mock_ai, \
                 patch('app.services.msx_api.get_milestone_comments') as mock_read:
                mock_read.return_value = {"success": True, "comments": []}
                mock_upsert.return_value = {
                    "success": True, "comment_count": 1, "action": "created",
                }
                mock_ai.return_value = "SQL MI migration assessed. 2 of 3 DBs ready."

                track_note_on_milestones(note, background=False)

                # Only called once — AI summary only (no fallback write)
                assert mock_upsert.call_count == 1

                final_args = mock_upsert.call_args
                final_content = final_args[0][1]
                assert "SQL MI migration assessed" in final_content
                assert f"· note-{note.id} ·" in final_content
                assert "Call:" not in final_content

                # comment_date should be the call_date
                assert final_args[1]["comment_date"] == "2026-03-12T00:00:00.000Z"

    def test_creates_fallback_when_ai_unavailable_and_no_existing_post(self, app):
        """When AI returns None but note has no existing MSX post, write a fallback comment."""
        with app.app_context():
            customer = Customer(name='Fallback Corp', tpid=9501)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/fb1',
                title='Fallback MS',
                customer_id=customer.id,
                msx_milestone_id='fb-guid-1',
            )
            db.session.add(milestone)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='<p>Some call notes.</p>',
            )
            topic = Topic(name='SQL MI')
            db.session.add(topic)
            db.session.flush()
            note.topics.append(topic)
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.flush()

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert, \
                 patch('app.services.milestone_tracking._ai_summarize_note') as mock_ai, \
                 patch('app.services.msx_api.get_milestone_comments') as mock_read:
                mock_read.return_value = {"success": True, "comments": []}
                mock_ai.return_value = None  # AI unavailable

                track_note_on_milestones(note, background=False)

                # Fallback write - note has no existing post on this milestone
                assert mock_upsert.call_count == 1
                content = mock_upsert.call_args[0][1]
                assert 'Fallback Corp' in content
                assert f'· note-{note.id} ·' in content

    def test_skips_write_when_ai_unavailable_but_post_exists(self, app):
        """When AI returns None and note already has an MSX post, skip write."""
        with app.app_context():
            customer = Customer(name='Existing Corp', tpid=9510)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/exist1',
                title='Existing MS',
                customer_id=customer.id,
                msx_milestone_id='exist-guid-1',
            )
            db.session.add(milestone)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='<p>Already synced notes.</p>',
            )
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.flush()
            ref_tag = f'note-{note.id}'

            # Simulate an existing comment with this note's ref tag
            existing_comment = {
                "userId": "Someone via Sales Buddy",
                "comment": f"Previous summary\n\n· {ref_tag} ·",
                "modifiedOn": "2026-03-10T00:00:00.000Z",
            }

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert, \
                 patch('app.services.milestone_tracking._ai_summarize_note') as mock_ai, \
                 patch('app.services.msx_api.get_milestone_comments') as mock_read:
                mock_read.return_value = {"success": True, "comments": [existing_comment]}
                mock_ai.return_value = None  # AI says no new info

                track_note_on_milestones(note, background=False)

                # No write - already has an existing post and AI says nothing new
                mock_upsert.assert_not_called()

    def test_skips_msx_for_no_msx_id(self, app):
        """Milestones without msx_milestone_id are skipped."""
        with app.app_context():
            customer = Customer(name='Local Corp', tpid=9502)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/local',
                title='Local Only',
                customer_id=customer.id,
            )
            db.session.add(milestone)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='Local note',
            )
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.flush()

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert:
                track_note_on_milestones(note, background=False)

                mock_upsert.assert_not_called()

    def test_no_milestones_returns_empty(self, app):
        """Note with no milestones returns empty list."""
        with app.app_context():
            customer = Customer(name='Empty Corp', tpid=9503)
            db.session.add(customer)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='No milestones',
            )
            db.session.add(note)
            db.session.flush()

            results = track_note_on_milestones(note, background=False)
            assert results == []

    def test_passes_existing_comments_to_ai(self, app):
        """AI receives existing comments read from MSX for dedup context."""
        with app.app_context():
            customer = Customer(name='Context Corp', tpid=9504)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/ctx1',
                title='Context MS',
                customer_id=customer.id,
                msx_milestone_id='ctx-guid-1',
            )
            db.session.add(milestone)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='<p>Call about migration blockers.</p>',
            )
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.flush()

            existing_comment_texts = ["Manager posted: PoC blockers identified", "Field update from team"]

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert, \
                 patch('app.services.milestone_tracking._ai_summarize_note') as mock_ai, \
                 patch('app.services.msx_api.get_milestone_comments') as mock_read:
                mock_read.return_value = {
                    "success": True,
                    "comments": [
                        {"comment": existing_comment_texts[0]},
                        {"comment": existing_comment_texts[1]},
                    ],
                }
                mock_upsert.return_value = {"success": True}
                mock_ai.return_value = "New blockers found in Oracle schema."

                track_note_on_milestones(note, background=False)

                # AI should have been called with the existing comment texts
                ai_call = mock_ai.call_args
                assert ai_call[0][3] == existing_comment_texts  # 4th arg is existing_comments


# ── Notification queue tests ────────────────────────────────────────────────


class TestNotificationQueue:
    """Test the background notification mechanism."""

    def test_drain_returns_queued_notifications(self):
        # Clear any stale notifications
        drain_notifications()

        _notify_error("MSX connection failed")
        _notify_error("AI timeout")

        notifications = drain_notifications()
        assert len(notifications) == 2
        assert notifications[0] == ("warning", "MSX connection failed")
        assert notifications[1] == ("warning", "AI timeout")

        # Queue should be empty now
        assert drain_notifications() == []

    def test_notify_error_with_note_id_includes_retry_link(self):
        """When note_id is provided, notification includes a retry link."""
        drain_notifications()

        _notify_error("Your note was saved but was not synced to MSX.", note_id=42)

        notifications = drain_notifications()
        assert len(notifications) == 1
        category, message = notifications[0]
        assert category == "warning"
        # Message should contain the retry URL
        msg_str = str(message)
        assert "/notes/42/retry-msx" in msg_str
        assert "Retry MSX sync" in msg_str
        # Original message text should be present
        assert "not synced to MSX" in msg_str


# ── Engagement tracking tests ────────────────────────────────────────────────


class TestTrackEngagementOnMilestones:
    """Test track_engagement_on_milestones."""

    def test_posts_pinned_story_to_msx(self, app):
        """Engagement story is posted with pin_to_top=True."""
        with app.app_context():
            customer = Customer(name='Eng Corp', tpid=9600)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/eng1',
                title='Eng MS',
                customer_id=customer.id,
                msx_milestone_id='eng-guid-1',
            )
            db.session.add(milestone)
            db.session.flush()

            engagement = Engagement(
                customer_id=customer.id,
                title='Cloud PoC',
                status='Active',
                technical_problem='<p>Need to assess cloud readiness</p>',
            )
            engagement.milestones.append(milestone)
            db.session.add(engagement)
            db.session.flush()

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert:
                mock_upsert.return_value = {
                    "success": True, "comment_count": 1,
                    "action": "created", "existing_comments": [],
                }
                results = track_engagement_on_milestones(engagement, background=False)

                mock_upsert.assert_called_once()
                call_args = mock_upsert.call_args
                assert call_args[0][0] == 'eng-guid-1'
                content = call_args[0][1]
                assert 'Engagement Overview: Cloud PoC [Active]' in content
                assert f'· eng-{engagement.id} ·' in content
                # pin_to_top should be True
                assert call_args[1]['pin_to_top'] is True

    def test_tracks_across_multiple_milestones(self, app):
        """Posts to MSX for each linked milestone."""
        with app.app_context():
            customer = Customer(name='Multi Corp', tpid=9601)
            db.session.add(customer)
            db.session.flush()

            m1 = Milestone(
                url='https://msx/m/m1', title='Phase 1',
                customer_id=customer.id, msx_milestone_id='multi-1',
            )
            m2 = Milestone(
                url='https://msx/m/m2', title='Phase 2',
                customer_id=customer.id, msx_milestone_id='multi-2',
            )
            db.session.add_all([m1, m2])
            db.session.flush()

            engagement = Engagement(
                customer_id=customer.id,
                title='Multi-phase Deploy',
                status='Active',
                technical_problem='Need to deploy across two phases.',
            )
            engagement.milestones.extend([m1, m2])
            db.session.add(engagement)
            db.session.flush()

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert:
                mock_upsert.return_value = {
                    "success": True, "comment_count": 1,
                    "action": "created", "existing_comments": [],
                }
                track_engagement_on_milestones(engagement, background=False)

                assert mock_upsert.call_count == 2

    def test_skips_msx_for_no_msx_id(self, app):
        """Milestones without msx_milestone_id return None result."""
        with app.app_context():
            customer = Customer(name='Local Eng Corp', tpid=9602)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/local-eng',
                title='Local Eng',
                customer_id=customer.id,
            )
            db.session.add(milestone)
            db.session.flush()

            engagement = Engagement(
                customer_id=customer.id,
                title='Local PoC',
                status='Active',
            )
            engagement.milestones.append(milestone)
            db.session.add(engagement)
            db.session.flush()

            with patch('app.services.milestone_tracking._upsert_to_msx') as mock_upsert:
                track_engagement_on_milestones(engagement, background=False)

                mock_upsert.assert_not_called()

    def test_no_milestones_returns_empty(self, app):
        """Engagement with no milestones returns empty list."""
        with app.app_context():
            customer = Customer(name='Empty Eng Corp', tpid=9603)
            db.session.add(customer)
            db.session.flush()

            engagement = Engagement(
                customer_id=customer.id,
                title='No MS',
                status='Active',
            )
            db.session.add(engagement)
            db.session.flush()

            results = track_engagement_on_milestones(engagement, background=False)
            assert results == []


# ── Retry MSX sync route tests ──────────────────────────────────────────────


class TestRetryMsxRoute:
    """Test the POST /notes/<id>/retry-msx endpoint."""

    def test_retry_triggers_tracking(self, client, app):
        """POST to retry route re-triggers milestone tracking."""
        with app.app_context():
            customer = Customer(name='Retry Corp', tpid=9700)
            db.session.add(customer)
            db.session.flush()

            milestone = Milestone(
                url='https://msx/m/retry1',
                title='Retry MS',
                customer_id=customer.id,
                msx_milestone_id='retry-guid-1',
            )
            db.session.add(milestone)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='<p>Retry test note.</p>',
            )
            note.milestones.append(milestone)
            db.session.add(note)
            db.session.commit()

            with patch('app.routes.notes.track_note_on_milestones') as mock_track:
                resp = client.post(f'/notes/{note.id}/retry-msx')
                assert resp.status_code == 202
                mock_track.assert_called_once()

    def test_retry_returns_404_for_missing_note(self, client):
        """POST to retry route returns 404 when note doesn't exist."""
        resp = client.post('/notes/99999/retry-msx')
        assert resp.status_code == 404

    def test_retry_returns_400_when_no_milestones(self, client, app):
        """POST to retry route returns 400 when note has no milestones."""
        with app.app_context():
            customer = Customer(name='No MS Corp', tpid=9701)
            db.session.add(customer)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                call_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
                content='<p>No milestones note.</p>',
            )
            db.session.add(note)
            db.session.commit()

            resp = client.post(f'/notes/{note.id}/retry-msx')
            assert resp.status_code == 400
