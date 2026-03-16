"""
Tests for deferred topic creation.

Topics entered by users or suggested by AI are not created in the database
until the note is actually saved. This prevents AI-generated noise topics
from polluting the topic list.

Tests cover:
- Note create with new_topic_names creates topics on save
- Note edit with new_topic_names creates topics on save
- Duplicate handling (new name matches existing topic, case-insensitive)
- AI suggest-topics endpoint no longer creates topics in DB
- Mixed existing topic_ids + new_topic_names on save
"""
from unittest.mock import patch
from app import db
from app.models import Topic, Note, Customer


class TestNoteCreateWithPendingTopics:
    """Test that new topics are created at note save time."""

    def test_create_note_with_new_topic_names(self, client, app):
        """Pending topic names should be created in DB when the note is saved."""
        resp = client.post('/note/new', data={
            'call_date': '2026-03-16',
            'content': '<p>Discussed new tech</p>',
            'new_topic_names': ['Azure Fabric', 'Copilot Studio'],
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.filter_by(
                content='<p>Discussed new tech</p>'
            ).first()
            assert note is not None
            assert len(note.topics) == 2
            names = sorted(t.name for t in note.topics)
            assert names == ['Azure Fabric', 'Copilot Studio']

            # Topics should exist in DB now
            assert Topic.query.filter_by(name='Azure Fabric').first() is not None
            assert Topic.query.filter_by(name='Copilot Studio').first() is not None

    def test_create_note_with_mixed_existing_and_new(self, client, app, sample_data):
        """Both existing topic_ids and new_topic_names work together."""
        existing_id = sample_data['topic1_id']
        resp = client.post('/note/new', data={
            'call_date': '2026-03-16',
            'content': '<p>Mixed topics test</p>',
            'topic_ids': [str(existing_id)],
            'new_topic_names': ['Brand New Topic'],
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.filter_by(
                content='<p>Mixed topics test</p>'
            ).first()
            assert note is not None
            assert len(note.topics) == 2
            topic_ids = {t.id for t in note.topics}
            assert existing_id in topic_ids

    def test_new_topic_name_dedupes_case_insensitive(self, client, app, sample_data):
        """If a new_topic_name matches an existing topic (case-insensitive), reuse it."""
        with app.app_context():
            existing = Topic(name='Azure SQL')
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id

        resp = client.post('/note/new', data={
            'call_date': '2026-03-16',
            'content': '<p>Dedup test</p>',
            'new_topic_names': ['azure sql'],
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.filter_by(content='<p>Dedup test</p>').first()
            assert len(note.topics) == 1
            assert note.topics[0].id == existing_id
            # Should NOT have created a duplicate
            count = Topic.query.filter(
                db.func.lower(Topic.name) == 'azure sql'
            ).count()
            assert count == 1

    def test_empty_topic_names_are_ignored(self, client, app):
        """Blank or whitespace-only topic names should be skipped."""
        resp = client.post('/note/new', data={
            'call_date': '2026-03-16',
            'content': '<p>Empty topic test</p>',
            'new_topic_names': ['', '  ', 'Real Topic'],
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.filter_by(content='<p>Empty topic test</p>').first()
            assert len(note.topics) == 1
            assert note.topics[0].name == 'Real Topic'


class TestNoteEditWithPendingTopics:
    """Test that pending topics are created when editing a note."""

    def test_edit_note_adds_new_topic_names(self, client, app, sample_data):
        """Editing a note with new_topic_names creates them and links them."""
        note_id = sample_data['call1_id']

        resp = client.post(f'/note/{note_id}/edit', data={
            'call_date': '2026-03-16',
            'content': '<p>Updated content</p>',
            'new_topic_names': ['Freshly Added Topic'],
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.get(note_id)
            assert any(t.name == 'Freshly Added Topic' for t in note.topics)

    def test_edit_note_mixed_existing_and_new(self, client, app, sample_data):
        """Edit with both existing IDs and new names."""
        note_id = sample_data['call1_id']
        existing_id = sample_data['topic1_id']

        resp = client.post(f'/note/{note_id}/edit', data={
            'call_date': '2026-03-16',
            'content': '<p>Edit mixed test</p>',
            'topic_ids': [str(existing_id)],
            'new_topic_names': ['Edit New Topic'],
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.get(note_id)
            assert len(note.topics) == 2
            names = {t.name for t in note.topics}
            assert 'Edit New Topic' in names

    def test_edit_replaces_topics_completely(self, client, app, sample_data):
        """Edit should clear old topics and set only the submitted ones."""
        note_id = sample_data['call1_id']

        # First verify the note has existing topics
        with app.app_context():
            note = Note.query.get(note_id)
            old_count = len(note.topics)
            assert old_count > 0

        # Edit with only a new topic (no existing IDs)
        resp = client.post(f'/note/{note_id}/edit', data={
            'call_date': '2026-03-16',
            'content': '<p>Replaced topics</p>',
            'new_topic_names': ['Only This Topic'],
        }, follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            note = Note.query.get(note_id)
            assert len(note.topics) == 1
            assert note.topics[0].name == 'Only This Topic'


class TestAISuggestTopicsDeferred:
    """Test that AI suggest-topics no longer creates topics in DB."""

    @patch('app.routes.ai.gateway_call')
    def test_new_topics_returned_without_id(self, mock_gw, app, client):
        """New topics from AI should come back with id=None."""
        mock_gw.return_value = {
            'topics': ['Never Seen Before', 'Also Brand New'],
            'usage': {'model': 'gpt-4o-mini', 'prompt_tokens': 100,
                      'completion_tokens': 50, 'total_tokens': 150},
        }

        resp = client.post('/api/ai/suggest-topics', json={
            'call_notes': 'Discussed some brand new technologies that are not in our database yet'
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert len(data['topics']) == 2

        # Both should have id=None since they don't exist
        for topic in data['topics']:
            assert topic['id'] is None

        # No topics should have been created in DB
        with app.app_context():
            assert Topic.query.count() == 0

    @patch('app.routes.ai.gateway_call')
    def test_existing_topics_returned_with_id(self, mock_gw, app, client):
        """Existing topics should still come back with their DB id."""
        with app.app_context():
            existing = Topic(name='Azure Functions')
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id

        mock_gw.return_value = {
            'topics': ['Azure Functions', 'Brand New Thing'],
            'usage': {},
        }

        resp = client.post('/api/ai/suggest-topics', json={
            'call_notes': 'Discussed Azure Functions and a brand new thing nobody has heard of'
        })
        assert resp.status_code == 200
        data = resp.get_json()

        matched = [t for t in data['topics'] if t['id'] is not None]
        unmatched = [t for t in data['topics'] if t['id'] is None]

        assert len(matched) == 1
        assert matched[0]['id'] == existing_id
        assert matched[0]['name'] == 'Azure Functions'

        assert len(unmatched) == 1
        assert unmatched[0]['name'] == 'Brand New Thing'

        # Only the pre-existing topic should be in DB
        with app.app_context():
            assert Topic.query.count() == 1
