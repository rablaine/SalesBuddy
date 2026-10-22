"""Explicit note-save acknowledgements, safe retries and competing editor revisions."""
from uuid import uuid4
from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models import Note, NoteSaveReceipt, db


def _submission(**changes: str) -> dict[str, str]:
    """Build one uniquely identified editor revision."""
    return {
        'call_date': '2026-10-22', 'content': '<p>Recoverable meeting notes</p>',
        'save_token': str(uuid4()), **changes,
    }


@pytest.mark.parametrize('field,value', [
    ('call_date', ''), ('call_date', 'not-a-date'), ('content', ''),
])
def test_invalid_editor_save_is_not_a_successful_redirect(client, field, value):
    """Validation failures cannot make the editor clear its recovery copy."""
    response = client.post(
        '/note/new', data=_submission(**{field: value}),
        headers={'Accept': 'application/json'},
    )
    assert response.status_code == 400
    assert response.json['success'] is False
    assert response.json['error']
    assert 'Location' not in response.headers


def test_create_retry_acknowledges_the_same_note(client, app):
    """A lost response and retry must not create another note."""
    payload = _submission()
    first = client.post('/note/new', data=payload, headers={'Accept': 'application/json'})
    second = client.post('/note/new', data=payload, headers={'Accept': 'application/json'})
    assert first.status_code == second.status_code == 200
    assert first.json['success'] is True
    assert first.json['note_id'] == second.json['note_id']
    with app.app_context():
        receipt = db.session.get(NoteSaveReceipt, payload['save_token'])
        assert receipt.note_id == first.json['note_id']
        assert db.session.get(Note, receipt.note_id).content == payload['content']


def test_save_token_cannot_be_reused_for_different_content(client):
    """A retry token acknowledges exactly one revision, not arbitrary later edits."""
    payload = _submission()
    assert client.post('/note/new', data=payload, headers={'Accept': 'application/json'}).json['success']
    payload['content'] = '<p>Different revision</p>'
    response = client.post('/note/new', data=payload, headers={'Accept': 'application/json'})
    assert response.status_code == 409
    assert response.json['success'] is False
    assert response.json['existing_note_id']


def test_retry_navigation_context_does_not_create_a_second_note(client):
    """Resuming from Home may change the return page, but not the save identity."""
    payload = _submission(referrer='/notes')
    first = client.post('/note/new', data=payload, headers={'Accept': 'application/json'})
    payload['referrer'] = '/'
    retry = client.post('/note/new', data=payload, headers={'Accept': 'application/json'})
    assert retry.status_code == 200
    assert first.json['note_id'] == retry.json['note_id']


def test_stale_edit_preserves_the_saved_note(client, app, sample_data):
    """Two editors cannot silently overwrite each other's saved revisions."""
    note_id = sample_data['call1_id']
    with app.app_context():
        original_version = db.session.get(Note, note_id).updated_at.isoformat()
    first = _submission(note_version=original_version)
    response = client.post(
        f'/note/{note_id}/edit', data=first, headers={'Accept': 'application/json'},
    )
    assert response.status_code == 200
    assert response.json['version'] != original_version
    retry = client.post(
        f'/note/{note_id}/edit', data=first, headers={'Accept': 'application/json'},
    )
    assert retry.status_code == 200
    stale = client.post(
        f'/note/{note_id}/edit',
        data=_submission(note_version=original_version, content='<p>Stale edit</p>'),
        headers={'Accept': 'application/json'},
    )
    assert stale.status_code == 409
    with app.app_context():
        assert db.session.get(Note, note_id).content == first['content']


def test_unconfirmed_task_is_not_created_again_by_note_save(client):
    """An editor save cannot fall back to an ambiguous duplicate MSX task write."""
    response = client.post(
        '/note/new', data=_submission(task_subject='Unconfirmed task'),
        headers={'Accept': 'application/json'},
    )
    assert response.status_code == 400
    assert 'Create the milestone task first' in response.json['error']


def test_old_acknowledgement_cannot_claim_a_later_saved_revision(client, sample_data):
    """A recovered response keeps its original version even after another editor saves."""
    payload = _submission()
    first = client.post('/note/new', data=payload, headers={'Accept': 'application/json'})
    note_id = first.json['note_id']
    newer = client.post(
        f'/note/{note_id}/edit',
        data=_submission(note_version=first.json['version'], content='<p>Newer saved revision</p>'),
        headers={'Accept': 'application/json'},
    )
    assert newer.status_code == 200
    retry = client.post('/note/new', data=payload, headers={'Accept': 'application/json'})
    assert retry.json['version'] == first.json['version']
    assert retry.json['version'] != newer.json['version']


def test_plain_form_submission_retains_existing_redirect_contract(client):
    """Non-editor clients keep the established HTML form behavior."""
    response = client.post('/note/new', data=_submission())
    assert response.status_code == 302
    assert '/note/' in response.headers['Location']


def test_failed_commit_rolls_back_note_and_retry_receipt(client, app):
    """A failed transaction cannot acknowledge or leave a half-created note."""
    payload = _submission(content='<p>Commit failure regression</p>')
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('Write failed')):
        response = client.post(
            '/note/new', data=payload, headers={'Accept': 'application/json'},
        )
    assert response.status_code == 503
    assert response.json['success'] is False
    with app.app_context():
        assert db.session.get(NoteSaveReceipt, payload['save_token']) is None
        assert Note.query.filter_by(content=payload['content']).count() == 0
