"""Partner detail parity and isolated manual, photo and remote meeting workflows."""

import json
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from sqlalchemy.exc import SQLAlchemyError

from app.models import Partner, PartnerContact, Specialty, Note, db


@pytest.fixture
def detail_records(app, sample_data):
    """Create all original relationships and stub external side effects."""
    with app.app_context():
        partner = Partner(name='Partner "quoted" <tag>', overview='Line one\nLine two',
                          rating=0, website='example.test', favicon_b64='cached')
        partner.specialties = [Specialty(name='Expertise')]
        partner.contacts = [
            PartnerContact(name="O'Brien", title='Architect', email='p@example.test',
                           is_primary=True, photo_b64='crop', photo_full_b64='full'),
            PartnerContact(name='Another', title=None, email=None),
        ]
        partner.notes = [Note.query.first()]
        other = Partner(name='Other', contacts=[PartnerContact(name='Other contact')])
        db.session.add_all([partner, other])
        db.session.commit()
        ids = partner.id, partner.contacts[0].id, partner.contacts[1].id, other.contacts[0].id
    with patch('app.routes.partners.backup_partner'), \
            patch('app.routes.partners.delete_partner_backup'), \
            patch('app.routes.partners.fetch_favicon_for_domain', return_value='new-icon'):
        yield ids


def soup(response):
    """Parse the rendered owned surface."""
    return BeautifulSoup(response.data, 'html.parser')


def test_detail_complete_native_contract(client, detail_records):
    """All original data, links, scripts and shared consumers survive without Bootstrap."""
    partner_id, primary, secondary, _ = detail_records
    response = client.get(f'/partners/{partner_id}')
    assert response.status_code == 200
    parsed = soup(response)
    root = parsed.select_one('[data-partner-detail]')
    # The wider bound accommodates independent 760px and 400px table tracks.
    assert {'sb-directory-frame', 'sb-directory-frame-wide'} <= set(root['class'])
    assert not root.select('.card,.btn,.badge,.modal,[onclick],[data-bs-toggle]')
    assert parsed.select_one('body.sb-page-frame')
    assert root.select_one('.part-rating')['aria-label'] == '0 out of 5 stars'
    assert root.select_one('.part-overview p').text == 'Line one\nLine two'
    assert root.select_one('a[href^="/specialties/"]')
    assert root.select_one('a[href="mailto:p@example.test"]')
    assert root.select_one('a[href^="/note/"]')
    note_link = root.select_one('[data-note-id] a[data-partner-note-preview]')
    assert note_link['href'] == f"/note/{note_link.find_parent('tr')['data-note-id']}"
    assert note_link['data-record-title']
    preview = parsed.select_one('dialog#partnerNotePreview.sb-dialog-record')
    assert preview['aria-labelledby'] == 'partnerNotePreviewTitle'
    for control in ('title', 'full-link', 'loading', 'error', 'retry', 'content', 'status'):
        assert preview.select_one(f'[data-sb-record-{control}]')
    assert preview.select_one('[data-sb-dialog-close]')
    assert root.select_one('.contact-photo-wrap img')['src'].endswith('crop')
    assert root.select_one('.contact-photo-wrap')['data-full-src'].endswith('full')
    assert root.select_one(f'[data-edit-contact="{primary}"]')
    assert root.select_one(f'[data-delete-contact="{secondary}"]')
    assert root.select_one('form[action$="/primary"]')['action'] == (
        f'/partners/{partner_id}/contacts/{secondary}/primary')
    assert len(root.select('form[action$="/primary"]')) == 1
    form = root.select_one('#contactForm')
    assert {f['name'] for f in form.select('[name]')} == {
        'contact_name', 'contact_title', 'contact_email', 'is_primary'}
    assert form['action'] == f'/partners/{partner_id}/contacts/new'
    assert form.select_one('[name=contact_name]').has_attr('required')
    assert form.select_one('[name=contact_email]')['type'] == 'email'
    assert form.select_one('[name=is_primary]').get('value', 'on') == 'on'
    assert root.select_one('form[action$="/delete"]')['action'] == f'/partners/{partner_id}/delete'
    assert [h['data-export-label'] for h in root.select('th[data-sb-sort-type]')] == [
        'Name', 'Title', 'Email', 'Primary', 'Name', 'Note date']
    assert root.select_one('td[data-sb-sort-value="0"]')
    data = json.loads(parsed.select_one('#partnerDetailData').text)
    assert data['contacts'][0]['name'] == "O'Brien"
    assert data['failure'] is None
    for filename in ('ui-partner-detail.js', 'ui-table-sort.js', 'ui-dialog.js', 'share.js',
                     'ui-record-dialog.js', 'ui-note-actions.js'):
        assert sum(
            urlsplit(script['src']).path.endswith(f'/{filename}')
            for script in parsed.select('script[src]')
        ) == 1
    for dialog_id in ('shareModal', 'shareImportReviewModal', 'addContactModal',
                      'deletePartnerDialog', 'deleteContactDialog', 'contactPhotoModal',
                      'scrapeDomainModal', 'scrapeReviewModal', 'partnerDiscardDialog'):
        assert parsed.select_one(f'#{dialog_id}').name == 'dialog'


def test_manual_contact_all_writes_and_original_primary_semantics(client, app, detail_records):
    """New, edit, set-primary and delete keep exact field names and redirects."""
    pid, primary, secondary, _ = detail_records
    base = f'/partners/{pid}/contacts'
    assert client.post(base + '/new', data={
        'contact_name': ' New ', 'contact_title': ' Role ', 'contact_email': ' n@example.test ',
        'is_primary': 'on'}).headers['Location'] == f'/partners/{pid}'
    with app.app_context():
        added = PartnerContact.query.filter_by(name='New').one()
        new_id = added.id
        assert added.title == 'Role' and added.email == 'n@example.test' and added.is_primary
        assert not db.session.get(PartnerContact, primary).is_primary
    assert client.post(f'{base}/{new_id}/edit', data={
        'contact_name': ' Revised ', 'contact_title': '', 'contact_email': '',
    }).status_code == 302
    with app.app_context():
        contact = db.session.get(PartnerContact, new_id)
        assert contact.title is contact.email is None
        assert contact.is_primary  # Original edit does not demote an unchecked primary.
    assert client.post(f'{base}/{secondary}/primary').status_code == 302
    with app.app_context():
        assert db.session.get(PartnerContact, secondary).is_primary
        assert not db.session.get(PartnerContact, new_id).is_primary
    assert client.post(f'{base}/{new_id}/delete').status_code == 302
    with app.app_context():
        assert db.session.get(PartnerContact, new_id) is None


@pytest.mark.parametrize('action', ['edit', 'delete', 'primary'])
def test_foreign_contact_rejected(client, app, detail_records, action):
    """Actual POST consumers cannot modify another partner's contacts."""
    pid, _, _, foreign = detail_records
    response = client.post(f'/partners/{pid}/contacts/{foreign}/{action}',
                           data={'contact_name': 'Changed', 'is_primary': 'on'})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(PartnerContact, foreign).name == 'Other contact'


@pytest.mark.parametrize('action', ['new', 'edit'])
def test_validation_entries_return_in_original_dialog(client, detail_records, action):
    """Existing validation redirect retains raw values and the correct retry action."""
    pid, cid, _, _ = detail_records
    path = f'/partners/{pid}/contacts/' + ('new' if action == 'new' else f'{cid}/edit')
    response = client.post(path, data={'contact_name': '  ', 'contact_title': ' Raw ',
                                      'contact_email': 'retry@example.test', 'is_primary': 'on'})
    assert response.status_code == 302
    data = json.loads(soup(client.get(response.headers['Location']))
                      .select_one('#partnerDetailData').text)['failure']
    assert data['path'] == path
    assert data['values']['contact_title'] == ' Raw '
    assert data['values']['is_primary'] == 'on'


@pytest.mark.parametrize('action', [
    'new', 'edit', 'primary', 'delete', 'partner-delete', 'photo', 'domain'])
def test_write_failure_rolls_back_and_allows_retry(client, app, detail_records, action):
    """Every detail write has an honest failure, retained manual entries and rollback."""
    pid, cid, _, _ = detail_records
    path = f'/partners/{pid}/contacts/' + ('new' if action == 'new' else f'{cid}/{action}')
    if action == 'partner-delete':
        path = f'/partners/{pid}/delete'
    if action == 'photo':
        path = f'/api/partner/contact/{cid}/photo'
    if action == 'domain':
        path = f'/api/partners/{pid}'
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('isolated failure')):
        response = (client.put(path, json={'name': 'Changed', 'website': 'new.test'})
                    if action == 'domain' else client.post(path, json={'photo_b64': None})
                    if action == 'photo' else client.post(path, data={
                        'contact_name': 'Retained', 'contact_title': 'Draft'}))
    assert response.status_code == (503 if action in ('photo', 'domain') else 500)
    with app.app_context():
        assert db.session.get(Partner, pid).name == 'Partner "quoted" <tag>'
        assert db.session.get(PartnerContact, cid).name == "O'Brien"
        assert db.session.get(PartnerContact, cid).photo_b64 == 'crop'
        assert PartnerContact.query.filter_by(name='Retained').count() == 0
    if action in ('new', 'edit'):
        data = json.loads(soup(response).select_one('#partnerDetailData').text)
        assert data['failure']['values']['contact_name'] == 'Retained'
    if action == 'photo':
        assert client.post(path, json={'photo_b64': None}).json['success']
    elif action == 'domain':
        assert client.put(path, json={'name': 'Changed'}).status_code == 200
    else:
        assert client.post(path, data={'contact_name': 'Retried'}).status_code == 302


def test_partner_delete_preserves_notes(client, app, detail_records):
    """Delete removes the association and contacts, not the original linked note."""
    pid = detail_records[0]
    with app.app_context():
        note_id = db.session.get(Partner, pid).notes[0].id
    assert client.post(f'/partners/{pid}/delete').headers['Location'] == '/partners'
    with app.app_context():
        assert db.session.get(Partner, pid) is None
        assert db.session.get(Note, note_id) is not None


def test_photo_save_remove_and_domain_preserve_contact_identity(client, app, detail_records):
    """Photo uses the existing crop provider; domain PUT keeps IDs, primary and photo data."""
    pid, cid, _, _ = detail_records
    with patch('app.services.contact_photo.process_contact_photo',
               return_value=('new-crop', 'new-full')) as process:
        response = client.post(f'/api/partner/contact/{cid}/photo', json={'photo_b64': 'raw'})
        assert response.json == {'success': True, 'photo_b64': 'new-crop'}
        process.assert_called_once_with('raw')
    original = client.get(f'/api/partners/{pid}').json
    original['website'] = 'https://www.new.test/path'
    assert client.put(f'/api/partners/{pid}', json=original).status_code == 200
    with app.app_context():
        contact = db.session.get(PartnerContact, cid)
        assert contact.photo_b64 == 'new-crop' and contact.photo_full_b64 == 'new-full'
        assert contact.is_primary
        assert db.session.get(Partner, pid).website == 'new.test'
    response = client.post(f'/api/partner/contact/{cid}/photo', json={'photo_b64': None})
    assert response.json['success']
    with app.app_context():
        contact = db.session.get(PartnerContact, cid)
        assert contact.photo_b64 is contact.photo_full_b64 is None


def test_photo_provider_failure_does_not_replace_existing_photo(client, app, detail_records):
    """Invalid clipboard photos report a recoverable JSON error, not a silent success."""
    cid = detail_records[1]
    with patch('app.services.contact_photo.process_contact_photo',
               side_effect=ValueError('invalid')):
        response = client.post(f'/api/partner/contact/{cid}/photo', json={'photo_b64': 'bad'})
    assert response.status_code == 400 and not response.json['success']
    with app.app_context():
        assert db.session.get(PartnerContact, cid).photo_b64 == 'crop'


@pytest.mark.parametrize('failure, status', [(TimeoutError('timeout'), 504),
                                           (RuntimeError('remote failure'), 500)])
def test_remote_scrape_failure_and_retry(client, detail_records, failure, status):
    """No live WorkIQ: remote failure/timeout is explicit and a second request can succeed."""
    path = f'/api/partners/{detail_records[0]}/scrape'
    with patch('app.services.workiq_service.query_workiq', side_effect=failure):
        assert client.post(path).status_code == status
    with patch('app.services.workiq_service.query_workiq', return_value=json.dumps({
        'contacts': [{'name': 'New person', 'email': 'new@example.test', 'title': None}],
        'specialties': ['New expertise'], 'overview': 'Suggestion', 'meetings_found': 2,
    })) as remote:
        result = client.post(path).json
        assert result['success'] and result['contacts'][0]['is_new']
        assert result['specialties'][0]['is_new']
        assert result['overview'] == 'Suggestion'
        assert remote.call_args.kwargs['operation'] == 'partner_scrape'


def test_scrape_malformed_response_is_failure_and_genuine_empty_response_is_retryable(
        client, app, detail_records):
    """A parse error is not a successful empty review; a genuine empty result stays valid."""
    path = f'/api/partners/{detail_records[0]}/scrape'
    with patch('app.services.workiq_service.query_workiq', return_value='not JSON'), \
            patch('app.services.telemetry_shipper.queue_workiq_call'):
        result = client.post(path)
        assert result.status_code == 500
        assert result.json['error'] == 'WorkIQ returned unreadable results. Try again.'
    with patch('app.services.workiq_service.query_workiq', return_value='{}'):
        result = client.post(path).json
        assert result['success'] and result['contacts'] == result['specialties'] == []
    with app.app_context():
        assert db.session.get(Partner, detail_records[0]).overview == 'Line one\nLine two'


def test_scrape_matches_existing_contacts_and_deduplicates_specialties_without_writing(
        client, app, detail_records):
    """Provider matching preserves existing values and explicitly identifies fill-only updates."""
    pid, primary, secondary, _ = detail_records
    with patch('app.services.workiq_service.query_workiq', return_value=json.dumps({
        'contacts': [{'name': "O'Brien", 'email': 'P@EXAMPLE.TEST', 'title': 'Different'},
                     {'name': 'Another', 'email': 'a@test', 'title': 'Filled'},
                     {'name': ' ', 'email': 'ignored@test'}],
        'specialties': ['Expertise', 'New', 'new', ''], 'overview': 'Suggestion',
    })):
        result = client.post(f'/api/partners/{pid}/scrape').json
    assert result['contacts'][0]['existing_id'] == primary
    assert not result['contacts'][0]['has_updates']
    assert result['contacts'][1]['existing_id'] == secondary
    assert result['contacts'][1]['has_updates']
    assert len(result['contacts']) == 2
    assert result['specialties'] == [
        {'name': 'Expertise', 'is_new': False}, {'name': 'New', 'is_new': True}]
    with app.app_context():
        assert db.session.get(PartnerContact, secondary).email is None
        assert db.session.get(Partner, pid).overview == 'Line one\nLine two'


def test_scrape_apply_selected_additions_and_fill_only_updates(client, app, detail_records):
    """Import preserves existing fields and adds only the explicit selected payload."""
    pid, primary, secondary, _ = detail_records
    response = client.post(f'/api/partners/{pid}/scrape/apply', json={
        'contacts': [{'existing_id': secondary, 'title': 'Filled', 'email': 'filled@test'},
                     {'existing_id': primary, 'title': 'Do not replace', 'email': 'other@test'},
                     {'name': 'New import', 'title': 'Role', 'email': 'new@test'}],
        'specialties': ['Expertise', 'New', 'new'],
        'overview': 'Line one\nLine two\n\n---\n\nNew context'})
    assert response.json == {'success': True, 'contacts_created': 1, 'contacts_updated': 1,
                             'specialties_created': 1, 'overview_updated': True}
    with app.app_context():
        partner = db.session.get(Partner, pid)
        assert partner.overview.endswith('New context')
        assert len(partner.specialties) == 2
        assert db.session.get(PartnerContact, primary).title == 'Architect'
        assert db.session.get(PartnerContact, secondary).title == 'Filled'


def test_apply_failure_rolls_back_retained_selection_retry(client, app, detail_records):
    """Apply errors roll back and permit retry of the unchanged selection."""
    pid = detail_records[0]
    path = f'/api/partners/{pid}/scrape/apply'
    payload = {'contacts': [{'name': 'Retry import'}], 'overview': 'Revised'}
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('failure')):
        assert client.post(path, json=payload).status_code == 500
    with app.app_context():
        assert PartnerContact.query.filter_by(name='Retry import').count() == 0
        assert db.session.get(Partner, pid).overview == 'Line one\nLine two'
    assert client.post(path, json=payload).json['success']
    assert client.post(path, json={}).status_code == 400


def test_apply_rejects_foreign_contact(client, app, detail_records):
    """A stale or modified review cannot update contacts outside its partner."""
    pid, _, _, foreign = detail_records
    result = client.post(f'/api/partners/{pid}/scrape/apply', json={
        'contacts': [{'existing_id': foreign, 'email': 'wrong@test'}]})
    assert result.status_code == 500 and not result.json['success']
    with app.app_context():
        assert db.session.get(PartnerContact, foreign).email is None
