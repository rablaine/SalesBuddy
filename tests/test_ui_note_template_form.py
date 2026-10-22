"""Native template form parity and failure recovery using the isolated database."""
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from sqlalchemy.exc import SQLAlchemyError

from app.models import NoteTemplate, UserPreference, db


HTML = '<h2>Summary</h2><p><strong>Bold</strong> <em>Italic</em></p><ul><li>Action</li></ul>'


@pytest.fixture
def template_id(app):
    """Create one saved rich template with both default associations."""
    with app.app_context():
        template = NoteTemplate(name='Original', content=HTML)
        db.session.add(template)
        db.session.flush()
        pref = UserPreference.query.first()
        pref.default_template_customer_id = template.id
        pref.default_template_noncustomer_id = template.id
        db.session.commit()
        return template.id


def parse(response):
    """Parse a server-rendered response."""
    return BeautifulSoup(response.data, 'html.parser')


def paths(mode, template_id):
    """Return the original distinct GET and POST paths."""
    return ('/templates/new', '/templates') if mode == 'new' else (
        f'/templates/{template_id}/edit', f'/templates/{template_id}')


@pytest.mark.parametrize('mode', ['new', 'edit'])
def test_exact_native_fields_and_editor(client, template_id, mode):
    """Keep original types, requiredness, defaults, formatting and commands."""
    get, post = paths(mode, template_id)
    soup = parse(client.get(get))
    form = soup.select_one('#templateForm')
    assert form['action'] == post and form['method'] == 'POST'
    assert [item['name'] for item in form.select('[name]')] == ['name', 'content']
    name = form.select_one('[name=name]')
    assert name['type'] == 'text' and name['maxlength'] == '200'
    assert name.has_attr('required') and not name.has_attr('autofocus')
    assert name['value'] == ('' if mode == 'new' else 'Original')
    content = form.select_one('[name=content]')
    assert content.name == 'textarea' and not content.has_attr('required')
    assert content.get_text() == ('' if mode == 'new' else HTML)
    assert not form.select('.btn,.card,.form-control,.row,.col,[onsubmit]')
    assert soup.select_one('body.sb-page-frame')
    assert form.select_one('[data-template-cancel]')['href'] == '/preferences'
    assert form.select_one('[type=submit]').get_text(strip=True) == (
        'Create Template' if mode == 'new' else 'Update Template')
    assert sum(
        urlsplit(script['src']).path == '/static/js/ui-milestone-form.js'
        for script in soup.select('script[src]')
    ) == 1
    assert len(form.select('#templateToolbar button')) == 10
    assert [option['value'] for option in form.select('.ql-header option')] == ['', '1', '2', '3']


@pytest.mark.parametrize('mode', ['new', 'edit'])
def test_success_exact_rich_roundtrip_defaults_and_consumers(client, app, template_id, mode):
    """HTML remains exact after original strip normalization; consumers see saved content."""
    _, post = paths(mode, template_id)
    with patch('app.routes.main.backup_template') as backup:
        response = client.post(post, data={'name': ' Revised ', 'content': f' {HTML}\n'})
    assert response.status_code == 302 and response.headers['Location'] == '/preferences'
    with app.app_context():
        saved = NoteTemplate.query.filter_by(name='Revised').one()
        assert saved.content == HTML
        sid = saved.id
        pref = UserPreference.query.first()
        assert pref.default_template_customer_id == template_id
        assert pref.default_template_noncustomer_id == template_id
    backup.assert_called_once_with(sid)
    assert client.get(f'/api/templates/{sid}').json == {
        'id': sid, 'name': 'Revised', 'content': HTML}
    assert {'id': sid, 'name': 'Revised'} in client.get('/api/templates').json
    settings = parse(client.get('/preferences'))
    assert settings.select_one(f'#templateSelect option[value="{sid}"]').get_text() == 'Revised'
    assert settings.select_one(f'#defaultCustomer option[value="{template_id}"][selected]')
    assert settings.select_one(f'#defaultNoncustomer option[value="{template_id}"][selected]')
    note = client.get('/note/new').get_data(as_text=True)
    assert 'SBTemplateHTML.sanitize' in note and 'Apply template...' in note
    assert 'templateReplaceBtn' in note and 'templatePrependBtn' in note


@pytest.mark.parametrize('mode', ['new', 'edit'])
@pytest.mark.parametrize('field', ['name', 'content'])
def test_required_redirect_raw_retention_once(client, template_id, mode, field):
    """Keep error strings and redirects, retaining raw entries on just the failed path."""
    get, post = paths(mode, template_id)
    values = {'name': ' raw <name> & ', 'content': ' <p> raw & content </p> '}
    values[field] = '   '
    response = client.post(post, data=values)
    assert response.status_code == 302 and response.headers['Location'] == get
    rendered = parse(client.get(get))
    assert f'Template {field} is required.' in rendered.get_text()
    assert rendered.select_one('[name=name]')['value'] == values['name']
    assert rendered.select_one('[name=content]').get_text() == values['content']
    assert rendered.select_one('#templateForm')['data-posted'] == 'true'
    assert parse(client.get(get)).select_one('#templateForm')['data-posted'] == 'false'


@pytest.mark.parametrize('mode', ['new', 'edit'])
def test_failure_rollback_escaped_content_and_retry(client, app, template_id, mode):
    """Database failure retains inert raw HTML, logs and preserves associations."""
    _, post = paths(mode, template_id)
    values = {'name': 'Failed', 'content': '<script>alert(1)</script><p onclick="bad()">raw</p>'}
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('isolated')), \
            patch('app.routes.main.backup_template') as backup:
        response = client.post(post, data=values)
    assert response.status_code == 500
    soup = parse(response)
    assert 'Could not save the template. Your entries are still here.' in soup.get_text()
    assert soup.select_one('[name=content]').get_text() == values['content']
    assert not soup.select_one('#editor script')
    assert soup.select_one('#templateForm')['data-posted'] == 'true'
    trail = soup.select_one('nav.sb-context-bar[aria-label="Breadcrumb"]')
    assert [link['href'] for link in trail.select('a')] == ['/', '/preferences']
    assert trail.select_one('[aria-current="page"] > span[title]').get_text(strip=True) == (
        'New Template' if mode == 'new' else 'Edit Template')
    backup.assert_not_called()
    with app.app_context():
        assert db.session.get(NoteTemplate, template_id).content == HTML
        assert NoteTemplate.query.count() == 1
        assert UserPreference.query.first().default_template_customer_id == template_id
    with patch('app.routes.main.backup_template'):
        assert client.post(post, data={'name': 'Retry', 'content': HTML}).status_code == 302


def test_missing_and_methods_and_inert_saved_html(client, app, template_id):
    """Missing IDs stay 404; saves stay POST-only; saved content never parses as markup."""
    assert client.get('/templates/999999/edit').status_code == 404
    assert client.post('/templates/999999', data={'name': 'x', 'content': HTML}).status_code == 404
    assert client.get('/templates').status_code == 405
    with app.app_context():
        db.session.get(NoteTemplate, template_id).content = '</textarea><script>bad()</script>'
        db.session.commit()
    soup = parse(client.get(f'/templates/{template_id}/edit'))
    assert soup.select_one('#content').get_text() == '</textarea><script>bad()</script>'
    assert not soup.select_one('#templateForm script')
