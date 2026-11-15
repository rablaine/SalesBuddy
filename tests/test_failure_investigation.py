"""Offline behavior tests for the isolated, synthetic-only failure experiment."""

import base64
import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / 'infra' / 'failure_investigation'
SPEC = importlib.util.spec_from_file_location('investigation_service', LAB / 'service.py')
svc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(svc)


class Blob:
    """Model independent state, exclusive leases and read-only report access."""

    def __init__(self, data=None) -> None:
        """Start with optional JSON and no active lease."""
        self.data = svc.canonical(data) if data is not None else None
        self.writes = 0
        self.locked = False

    def upload_blob(self, data, overwrite=False, lease=None) -> None:
        """Persist JSON bytes and enforce create-only writes."""
        if self.data is not None and not overwrite:
            raise ResourceExistsError('Already exists')
        self.data = data
        self.writes += 1

    def download_blob(self, lease=None, max_concurrency=1):
        """Provide the bounded downloader contract used by the SDK."""
        if self.data is None:
            raise ResourceNotFoundError('Missing')
        return SimpleNamespace(size=len(self.data), readall=lambda: self.data)

    def acquire_lease(self, lease_duration):
        """Exclude a second worker."""
        if self.locked:
            error = HttpResponseError('Locked')
            error.status_code = 409
            raise error
        self.locked = True

        def release() -> None:
            """Release the in-memory lock."""
            self.locked = False

        return SimpleNamespace(release=release, renew=MagicMock())


class Container:
    """Keep all documents and mutation counts visible to isolation assertions."""

    def __init__(self) -> None:
        """Start empty."""
        self.blobs = {}

    def get_blob_client(self, name):
        """Return a stable independent blob."""
        return self.blobs.setdefault(name, Blob())

    def list_blobs(self, name_starts_with, results_per_page):
        """Model bounded Azure pages without listing unpersisted blob-client handles."""
        names = sorted(name for name, blob in self.blobs.items()
                       if name.startswith(name_starts_with) and blob.data is not None)

        class Pages:
            """Expose the same post-page continuation token as the storage SDK."""

            def __init__(self, continuation_token) -> None:
                """Resume at the requested page offset."""
                self.offset = int(continuation_token or 0)
                self.continuation_token = continuation_token

            def __next__(self):
                """Yield one page and advertise only an actual remaining page."""
                if self.offset >= len(names):
                    self.continuation_token = None
                    raise StopIteration
                page = names[self.offset:self.offset + results_per_page]
                self.offset += len(page)
                self.continuation_token = str(self.offset) if self.offset < len(names) else None
                return iter(SimpleNamespace(name=name) for name in page)

        return SimpleNamespace(by_page=lambda continuation_token=None: Pages(continuation_token))


@pytest.fixture
def event() -> dict:
    """Build a report matching the unchanged diagnostics producer and email worker."""
    payload = json.loads((LAB / 'synthetic_payload.json').read_text())
    return {
        'kind': 'failure', 'report_id': payload['report_id'], 'content': payload['report'],
        'attachments': payload['attachments'], 'status': 'succeeded',
        'contact': 'synthetic-reporter@example.org',
    }


@pytest.fixture
def worker(event):
    """Inject storage and HTTP fakes so no test can launch a real agent."""
    reports = Container()
    reports.blobs[f'reports/{event["report_id"]}.json'] = Blob(event)
    return svc.InvestigationService(
        reports, Container(), MagicMock(), MagicMock(), report_id=event['report_id'],
        approved_hash=svc.content_hash(event), repository='test-owner/failure-lab')


def record(worker) -> dict:
    """Read only the experiment's independent state."""
    return json.loads(worker.state.get_blob_client(
        f'investigations/{worker.report_id}.json').data)


def task_response(code=201, state='queued', task_id='test-task-1', artifacts=None):
    """Make the actual task API shape without any network request."""
    task = {'id': task_id, 'state': state, 'artifacts': artifacts or []}
    return SimpleNamespace(status_code=code, content=svc.canonical(task), json=lambda: task)


def enable_submit(worker) -> None:
    """Use only a fake credential in the in-memory worker."""
    worker.mode = 'submit'
    worker.token = 'fake-scoped-token'
    worker.transport.post.return_value = task_response()


def test_fixture_matches_existing_diagnostics_contract(event):
    """Verify the exact fake packet against existing intake without changing its contract."""
    spec = importlib.util.spec_from_file_location(
        'existing_diagnostics_contract', ROOT / 'infra' / 'diagnostics' / 'service.py')
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    payload = json.loads((LAB / 'synthetic_payload.json').read_text())
    assert diagnostics.parse_payload(svc.canonical(payload)) == payload
    frozen = diagnostics.create_event(payload, 'synthetic-reporter@example.org')
    assert svc.content_hash(event) == frozen['hash']
    assert 'NO REAL CUSTOMER DATA' in base64.b64decode(
        payload['attachments'][0]['contentInBase64']).decode()


def test_preview_is_network_free_and_does_not_mutate_reports(worker, event):
    """Only independent storage is written; contact never enters the generated prompt."""
    worker.tick()
    worker.tick()
    state = record(worker)
    assert state['status'] == 'preview'
    assert 'retry_delay(4)' in state['prompt']
    assert event['contact'] not in json.dumps(state)
    worker.transport.post.assert_not_called()
    worker.transport.get.assert_not_called()
    worker.credential.get_token.assert_not_called()
    assert all(blob.writes == 0 for blob in worker.reports.blobs.values())
    assert json.loads(worker.state.get_blob_client('control.json').data)['submissions'] == 0


@pytest.mark.parametrize('email_status', ['pending', 'submitted', 'failed'])
def test_waits_for_successful_email_delivery(worker, event, email_status):
    """The watcher never interferes with delivery or investigates a failed email."""
    event['status'] = email_status
    worker.reports.blobs[f'reports/{worker.report_id}.json'] = Blob(event)
    worker.tick()
    assert worker.state.get_blob_client(f'investigations/{worker.report_id}.json').data is None
    worker.transport.post.assert_not_called()


@pytest.mark.parametrize('change', ['content', 'attachment', 'identity', 'kind'])
def test_rejects_unapproved_report_and_attachments(worker, event, change):
    """The approval hash and report identity exclude altered or unrelated evidence."""
    if change == 'content':
        event['content']['unexpected'] = 'Not approved'
    elif change == 'attachment':
        event['attachments'][0]['contentInBase64'] = base64.b64encode(b'Not approved').decode()
    elif change == 'identity':
        event['report_id'] = 'f' * 32
    else:
        event['kind'] = 'followup'
    worker.reports.blobs[f'reports/{worker.report_id}.json'] = Blob(event)
    with pytest.raises(ValueError):
        worker.tick()
    worker.transport.post.assert_not_called()
    assert all(blob.writes == 0 for blob in worker.reports.blobs.values())


def test_preview_can_submit_once_and_then_poll(worker):
    """Promotion produces a PR task, not a second submission on each timer tick."""
    worker.tick()
    enable_submit(worker)
    worker.tick()
    assert record(worker)['task_id'] == 'test-task-1'
    worker.transport.get.return_value = task_response(200, 'completed', artifacts=[
        {'provider': 'github', 'type': 'pull', 'data': {'id': 5317297503}},
    ])
    worker.tick()
    worker.tick()
    worker.transport.post.assert_called_once()
    call = worker.transport.post.call_args
    assert call.kwargs['json']['create_pull_request'] is True
    assert call.kwargs['json']['base_ref'] == 'main'
    assert call.kwargs['allow_redirects'] is False
    assert call.kwargs['timeout'] == (5, 15)
    assert record(worker)['status'] == 'completed'
    assert record(worker)['task_url'] == 'https://github.com/test-owner/failure-lab/tasks/test-task-1'
    assert 'pull_requests' not in record(worker)
    worker.transport.get.assert_called_once()
    assert 'fake-scoped-token' not in json.dumps(record(worker))
    assert all(blob.writes == 0 for blob in worker.reports.blobs.values())


def test_completed_legacy_task_repairs_database_id_link_without_resubmitting(worker):
    """Completed experiments replace guessed PR links with the official task review URL."""
    enable_submit(worker)
    worker.tick()
    blob = worker.state.get_blob_client(f'investigations/{worker.report_id}.json')
    legacy = record(worker)
    legacy.update(status='completed', pull_requests=[
        f'https://github.com/{worker.repository}/pull/5317297503'])
    legacy.pop('task_url')
    blob.upload_blob(svc.canonical(legacy), overwrite=True)
    worker.transport.get.return_value = task_response(200, 'completed', artifacts=[
        {'provider': 'github', 'type': 'pull', 'data': {'id': 5317297503}},
    ])
    worker.tick()
    worker.tick()
    assert record(worker)['task_url'] == 'https://github.com/test-owner/failure-lab/tasks/test-task-1'
    assert 'pull_requests' not in record(worker)
    worker.transport.get.assert_called_once()
    worker.transport.post.assert_called_once()


@pytest.mark.parametrize('failure', ['network', 'server', 'malformed', 'storage_after_post'])
def test_ambiguous_creation_is_never_retried(worker, failure):
    """A lost response or post-response storage failure cannot create duplicate agent tasks."""
    enable_submit(worker)
    if failure == 'network':
        worker.transport.post.side_effect = TimeoutError('Fake network timeout')
    elif failure == 'server':
        worker.transport.post.return_value = task_response(503)
    elif failure == 'malformed':
        worker.transport.post.return_value = SimpleNamespace(
            status_code=201, content=b'{}', json=lambda: {})
    else:
        blob = worker.state.get_blob_client(f'investigations/{worker.report_id}.json')
        original_upload = blob.upload_blob

        def fail_after_post(data, **kwargs) -> None:
            """Fail only when persisting a returned task ID."""
            if json.loads(data).get('task_id'):
                raise RuntimeError('Fake storage failure')
            original_upload(data, **kwargs)

        blob.upload_blob = fail_after_post
    with pytest.raises((TimeoutError, RuntimeError, ValueError)):
        worker.tick()
    assert record(worker)['status'] == 'submission_uncertain'
    worker.tick()
    worker.transport.post.assert_called_once()


@pytest.mark.parametrize('code', [401, 403, 422])
def test_explicit_api_rejection_stops_submission(worker, code):
    """Permission or validation rejection is visible and does not loop."""
    enable_submit(worker)
    worker.transport.post.return_value = task_response(code)
    worker.tick()
    worker.tick()
    assert record(worker)['status'] == 'rejected'
    assert record(worker)['http_status'] == code
    worker.transport.post.assert_called_once()


def test_global_budget_prevents_second_report_task(worker, event):
    """Measure the one-submission daily ceiling across different approved reports."""
    enable_submit(worker)
    worker.tick()
    another = copy.deepcopy(event)
    another['report_id'] = 'f' * 32
    worker.reports.blobs[f'reports/{another["report_id"]}.json'] = Blob(another)
    second = svc.InvestigationService(
        worker.reports, worker.state, MagicMock(), worker.transport,
        report_id=another['report_id'], approved_hash=svc.content_hash(another),
        repository=worker.repository, mode='submit', token='fake-scoped-token')
    second.tick()
    worker.transport.post.assert_called_once()
    assert json.loads(worker.state.get_blob_client('control.json').data)['submissions'] == 1


def test_locked_worker_and_poll_errors_do_not_submit_again(worker):
    """Concurrent timer invocations and read failures cannot bypass task deduplication."""
    worker.state.blobs['control.json'] = Blob(
        {'day': svc.now().date().isoformat(), 'submissions': 0})
    worker.state.blobs['control.json'].locked = True
    worker.tick()
    worker.transport.post.assert_not_called()
    worker.state.blobs['control.json'].locked = False
    enable_submit(worker)
    worker.tick()
    worker.transport.get.side_effect = TimeoutError('Fake read timeout')
    with pytest.raises(TimeoutError):
        worker.tick()
    worker.transport.post.assert_called_once()
    assert record(worker)['task_id'] == 'test-task-1'


def test_settings_cannot_retarget_existing_state(worker):
    """Changing a configured repository cannot reuse or redirect an existing investigation."""
    worker.tick()
    worker.repository = 'different-owner/failure-lab'
    enable_submit(worker)
    with pytest.raises(ValueError, match='settings cannot be changed'):
        worker.tick()
    worker.transport.post.assert_not_called()


@pytest.mark.parametrize('settings', [
    {'repository': 'rablaine/SalesBuddy'},
    {'repository': 'owner/salesbuddy'},
    {'repository': 'owner/repo/../../SalesBuddy'},
    {'mode': 'submit'},
    {'mode': 'submit', 'token': '@Microsoft.KeyVault(SecretUri=unresolved)'},
    {'report_id': '../report'},
    {'approved_hash': 'unapproved'},
])
def test_unsafe_operator_settings_are_rejected(worker, settings):
    """Invalid target or unresolved credential must fail before any outbound access."""
    options = {
        'report_id': worker.report_id, 'approved_hash': worker.approved_hash,
        'repository': worker.repository,
    }
    options.update(settings)
    with pytest.raises(ValueError):
        svc.InvestigationService(worker.reports, worker.state, MagicMock(), MagicMock(), **options)


def test_disabled_function_never_initializes_clients_and_status_is_private(monkeypatch):
    """Default-off timer and operator status cannot access diagnostics or GitHub."""
    import azure.functions as func

    monkeypatch.setitem(sys.modules, 'service', svc)
    spec = importlib.util.spec_from_file_location('investigation_functions', LAB / 'function_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clients = MagicMock(side_effect=AssertionError('Disabled app initialized clients'))
    monkeypatch.setattr(module, 'service', clients)
    monkeypatch.delenv('INVESTIGATION_ENABLED', raising=False)
    functions = {item.get_function_name(): item for item in module.app.get_functions()}
    functions['investigate'].get_user_function()(MagicMock())
    request = func.HttpRequest('GET', 'https://lab/api/status', body=b'')
    result = functions['status'].get_user_function()(request)
    assert json.loads(result.get_body()) == {'enabled': False}
    clients.assert_not_called()
    binding = next(item for item in functions['status'].get_bindings()
                   if item.type == 'httpTrigger')
    assert binding.get_dict_repr()['authLevel'] == func.AuthLevel.FUNCTION
    monkeypatch.setenv('INVESTIGATION_ENABLED', 'invalid')
    with pytest.raises(ValueError):
        functions['investigate'].get_user_function()(MagicMock())
    clients.assert_not_called()


def test_operator_status_does_not_expose_prompt_or_contact(monkeypatch, worker):
    """Only task state and review links are returned by the protected endpoint."""
    import azure.functions as func

    worker.tick()
    monkeypatch.setitem(sys.modules, 'service', svc)
    spec = importlib.util.spec_from_file_location('investigation_status', LAB / 'function_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'service', lambda: worker)
    monkeypatch.setenv('INVESTIGATION_ENABLED', 'true')
    result = module.status.build().get_user_function()(
        func.HttpRequest('GET', 'https://lab/api/status', body=b''))
    assert json.loads(result.get_body()) == {'report_id': worker.report_id, 'status': 'preview'}


def test_oversized_stored_packet_is_rejected_before_read(worker):
    """Enforce the real byte budget, not just the fixture's normal size."""
    blob = worker.reports.get_blob_client(f'reports/{worker.report_id}.json')
    blob.data = b'x' * (svc.MAX_REPORT_BYTES + 1)
    with pytest.raises(ValueError, match='exceeds experiment limit'):
        worker.tick()
    worker.transport.post.assert_not_called()


def enable_completion_email(worker) -> None:
    """Finish a fake task with real API artifact shapes and isolated email settings."""
    enable_submit(worker)
    worker.tick()
    worker.transport.get.return_value = task_response(200, 'completed', artifacts=[
        {'provider': 'github', 'type': 'pull', 'data': {'id': 5317297503}},
        {'provider': 'github', 'type': 'branch',
         'data': {'head_ref': 'copilot/fix-retry', 'base_ref': 'main'}},
    ])
    worker.tick()
    worker.completion_email = {
        'endpoint': 'https://lab-mail.unitedstates.communication.azure.com',
        'sender': 'sender@example.org', 'recipient': 'reviewer@example.org',
    }
    worker.credential.get_token.return_value = SimpleNamespace(token='fake-email-token')


def email_response(status_code=200, body=None):
    """Return a bounded fake ACS or PR lookup response."""
    payload = body if body is not None else {}
    return SimpleNamespace(status_code=status_code, content=svc.canonical(payload),
                           json=lambda: payload)


def test_completion_email_resolves_real_pr_and_confirms_delivery_once(worker, monkeypatch):
    """The email has the PR number, no logs, one recipient and one stable send operation."""
    enable_completion_email(worker)
    timestamp = svc.now()
    monkeypatch.setattr(svc, 'now', lambda: timestamp)
    worker.transport.get.side_effect = [
        email_response(body=[{'id': 5317297503, 'number': 1}]),
        email_response(body={'status': 'Succeeded'}),
    ]
    worker.transport.post.return_value = email_response(202)
    worker.tick()
    notice = record(worker)['notification']
    assert notice['status'] == 'submitted'
    assert notice['pr_url'] == 'https://github.com/test-owner/failure-lab/pull/1'
    message = worker.transport.post.call_args.kwargs['json']
    assert message['recipients'] == {'to': [{'address': 'reviewer@example.org'}]}
    assert '/pull/1' in message['content']['plainText']
    parser = EmailContentParser()
    parser.feed(message['content']['html'])
    assert parser.links == [
        ('https://github.com/test-owner/failure-lab/pull/1', 'Review pull request'),
        ('https://github.com/test-owner/failure-lab/tasks/test-task-1', 'View Copilot run'),
    ]
    assert 'https://' not in ''.join(parser.visible_text)
    assert 'attachments' not in message
    assert 'prompt' not in message
    assert 'reviewer@example.org' not in json.dumps(record(worker))
    worker.tick()
    assert worker.transport.post.call_count == 2  # One task, one email.
    monkeypatch.setattr(svc, 'now', lambda: timestamp + timedelta(minutes=2))
    worker.tick()
    worker.tick()
    assert record(worker)['notification']['status'] == 'succeeded'
    assert worker.transport.post.call_count == 2
    assert all(blob.writes == 0 for blob in worker.reports.blobs.values())


class EmailContentParser(HTMLParser):
    """Read visible link labels independently from their potentially rewritten targets."""

    def __init__(self) -> None:
        """Collect link destinations and visible message text."""
        super().__init__()
        self.links = []
        self.visible_text = []
        self.elements = []
        self.target = None
        self.label = ''

    def handle_starttag(self, tag, attrs) -> None:
        """Capture the destination without treating it as visible content."""
        self.elements.append((tag, dict(attrs)))
        if tag == 'a':
            self.target = dict(attrs)['href']
            self.label = ''

    def handle_data(self, data) -> None:
        """Collect the rendered text rather than HTML source attributes."""
        self.visible_text.append(data)
        if self.target is not None:
            self.label += data

    def handle_endtag(self, tag) -> None:
        """Finish each link's accessible label."""
        if tag == 'a':
            self.links.append((self.target, self.label))
            self.target = None


def test_completion_html_leaves_theme_colors_to_outlook(worker):
    """Explicit dark fills invert to light in Outlook, so use its native theme instead."""
    enable_completion_email(worker)
    state = record(worker)
    state['notification'] = {'pr_url': 'https://github.com/test-owner/failure-lab/pull/1'}
    parser = EmailContentParser()
    parser.feed(svc.completion_email_content(state)['html'])

    assert any(tag == 'body' for tag, _ in parser.elements)
    for _, attrs in parser.elements:
        assert 'bgcolor' not in attrs
        assert 'color' not in attrs
        styles = dict((key.strip(), value.strip()) for key, value in
                      (part.split(':', 1) for part in attrs.get('style', '').split(';')
                       if ':' in part))
        assert not ({'color', 'background', 'background-color', 'background-image'} & styles.keys())
    hints = {attrs.get('name'): attrs.get('content') for tag, attrs in parser.elements
             if tag == 'meta'}
    assert 'color-scheme' not in hints
    assert 'supported-color-schemes' not in hints


def test_opt_in_theme_preview_preserves_previous_preview_and_original(worker, monkeypatch):
    """A new template preview sends once without resetting either successful delivery."""
    enable_completion_email(worker)
    timestamp = svc.now()
    monkeypatch.setattr(svc, 'now', lambda: timestamp)
    state = record(worker)
    original = {
        'status': 'succeeded', 'pr_url': 'https://github.com/test-owner/failure-lab/pull/1',
        'operation_id': 'original-email-operation',
    }
    state['notification'] = original
    worker.state.get_blob_client(f'investigations/{worker.report_id}.json').upload_blob(
        svc.canonical(state), overwrite=True)
    previous_preview = worker.state.get_blob_client(
        f'notification-previews/{state["task_id"]}/dark-html-preview-v1.json')
    previous_preview.upload_blob(svc.canonical({'notification': {'status': 'succeeded'}}))
    previous_content = previous_preview.data
    worker.preview_email = True
    worker.transport.post.return_value = email_response(202)
    worker.transport.get.return_value = email_response(body={'status': 'Succeeded'})
    worker.tick()
    message = worker.transport.post.call_args.kwargs['json']
    assert message['content']['subject'].startswith('[Preview] ')
    assert worker.transport.post.call_args.kwargs['headers']['Operation-Id'] != (
        original['operation_id'])
    assert record(worker)['notification'] == original
    assert record(worker)['email_preview']['status'] == 'submitted'
    assert record(worker)['email_preview']['delivery_kind'] == svc.EMAIL_PREVIEW_KIND
    monkeypatch.setattr(svc, 'now', lambda: timestamp + timedelta(minutes=2))
    worker.tick()
    worker.tick()
    assert record(worker)['notification'] == original
    assert record(worker)['email_preview']['status'] == 'succeeded'
    assert worker.transport.post.call_count == 2  # One task and one explicitly requested preview.
    receipts = [name for name in worker.state.blobs if name.startswith('notification-previews/')]
    assert len(receipts) == 2
    assert previous_preview.data == previous_content
    assert previous_preview.writes == 1


def test_uncertain_email_send_polls_saved_operation_without_resending(worker, monkeypatch):
    """A lost ACS response cannot create another task or email when the operation exists."""
    enable_completion_email(worker)
    timestamp = svc.now()
    monkeypatch.setattr(svc, 'now', lambda: timestamp)
    worker.transport.get.side_effect = [
        email_response(body=[{'id': 5317297503, 'number': 1}]),
        email_response(body={'status': 'Succeeded'}),
    ]
    worker.transport.post.side_effect = TimeoutError('Synthetic secret DO_NOT_LOG')
    worker.tick()
    notice = record(worker)['notification']
    assert notice['status'] == 'submitting'
    sent_id = worker.transport.post.call_args.kwargs['headers']['Operation-Id']
    assert notice['operation_id'] == sent_id
    monkeypatch.setattr(svc, 'now', lambda: timestamp + timedelta(minutes=3))
    worker.tick()
    assert record(worker)['notification']['status'] == 'succeeded'
    assert sent_id in worker.transport.get.call_args.args[0]
    assert worker.transport.post.call_count == 2


def test_completion_pr_permission_error_is_visible_and_sends_nothing(worker):
    """Missing PR-read permission is not disguised as a completed email notification."""
    enable_completion_email(worker)
    worker.transport.get.return_value = email_response(403)
    worker.tick()
    notice = record(worker)['notification']
    assert notice['stage'] == 'pr_lookup'
    assert notice['http_status'] == 403
    assert notice['errors'] == 1
    assert record(worker)['status'] == 'completed'
    worker.transport.post.assert_called_once()
    worker.credential.get_token.assert_not_called()


def test_uncertain_email_with_missing_operation_is_not_sent_again(worker, monkeypatch):
    """Even an ACS 404 after a lost send response cannot justify sending another email."""
    enable_completion_email(worker)
    timestamp = svc.now()
    monkeypatch.setattr(svc, 'now', lambda: timestamp)
    worker.transport.get.side_effect = [
        email_response(body=[{'id': 5317297503, 'number': 1}]),
        email_response(404),
    ]
    worker.transport.post.side_effect = TimeoutError('Synthetic ambiguous send')
    worker.tick()
    monkeypatch.setattr(svc, 'now', lambda: timestamp + timedelta(minutes=3))
    worker.tick()
    assert record(worker)['notification']['status'] == 'submitting'
    assert record(worker)['notification']['http_status'] == 404
    assert worker.transport.post.call_count == 2


def test_completion_email_does_not_send_in_preview_or_failed_task(worker):
    """Only enabled submit mode and successfully completed work can notify the reviewer."""
    enable_completion_email(worker)
    worker.mode = 'preview'
    worker.tick()
    worker.transport.post.assert_called_once()
    worker.mode = 'submit'
    blob = worker.state.get_blob_client(f'investigations/{worker.report_id}.json')
    state = record(worker)
    state['status'] = 'failed'
    blob.upload_blob(svc.canonical(state), overwrite=True)
    worker.tick()
    worker.transport.post.assert_called_once()


def test_completion_email_rejects_changed_recipient_during_delivery(worker):
    """Changing routing cannot silently reuse an already submitted operation."""
    enable_completion_email(worker)
    worker.transport.get.return_value = email_response(body=[{'id': 5317297503, 'number': 1}])
    worker.transport.post.return_value = email_response(202)
    worker.tick()
    worker.completion_email['recipient'] = 'different@example.org'
    with pytest.raises(ValueError, match='configuration changed'):
        worker.tick()
    assert worker.transport.post.call_count == 2


@pytest.fixture
def upload_module(monkeypatch):
    """Load the actual upload handler with every cloud boundary replaced."""
    monkeypatch.setitem(sys.modules, 'service', svc)
    spec = importlib.util.spec_from_file_location('lab_token_upload', LAB / 'function_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def upload_clients(upload_module, monkeypatch):
    """Use independent fake state and a vault that has no secret."""
    state = Container()
    vault = MagicMock()
    vault.get_secret.side_effect = ResourceNotFoundError('Missing')
    factory = MagicMock(return_value=(state, vault))
    monkeypatch.setattr(upload_module, 'configured_token_upload', factory)
    return state, vault, factory


def upload_request(body=None, method='POST', content_type='application/json',
                   request_id='a' * 32, profile=None):
    """Create a request with fake credential input, not a real GitHub token."""
    import azure.functions as func

    if body is None:
        body = svc.canonical({'token': 'github_pat_' + 'A' * 32})
    headers = {'Content-Type': content_type, 'X-Lab-Upload-Id': request_id}
    if profile is not None:
        headers['X-Upload-Profile'] = profile
    return func.HttpRequest(method, 'https://lab/api/token-upload', headers=headers, body=body)


@pytest.mark.parametrize('minutes', [None, -1, 0, 16])
def test_upload_closed_or_excessive_window_never_touches_vault(
        upload_module, upload_clients, monkeypatch, minutes):
    """Default-off, expired and oversized windows deny even an otherwise valid request."""
    timestamp = datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(svc, 'now', lambda: timestamp)
    if minutes is None:
        monkeypatch.delenv('TOKEN_UPLOAD_EXPIRES_AT', raising=False)
    else:
        monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT',
                           (timestamp + timedelta(minutes=minutes)).isoformat())
    response = upload_module.upload_token.build().get_user_function()(upload_request())
    assert response.status_code == 410
    upload_clients[2].assert_not_called()


@pytest.mark.parametrize('body', [
    b'{"token":"ghp_classic_token_not_allowed"}',
    b'{"token":true}', b'{"token":"github_pat_short"}',
    b'{"token":"github_pat_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","token":"duplicate"}',
    b'{"token":"github_pat_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","vault":"another-vault"}',
    b'x' * 513,
])
def test_upload_rejects_broad_tokens_and_invalid_bodies(
        upload_module, upload_clients, monkeypatch, body):
    """Only the bounded fine-grained credential field can reach the fixed vault."""
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    response = upload_module.upload_token.build().get_user_function()(upload_request(body))
    assert response.status_code == 400
    upload_clients[2].assert_not_called()
    assert b'github_pat' not in response.get_body()


def test_upload_readiness_and_single_write_do_not_start_copilot(
        upload_module, upload_clients, monkeypatch):
    """Readiness reads no token value to the caller; upload writes one fixed secret once."""
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    handler = upload_module.upload_token.build().get_user_function()
    state, vault, _ = upload_clients
    agent = MagicMock(side_effect=AssertionError('Token upload started an agent'))
    monkeypatch.setattr(upload_module, 'service', agent)
    ready = handler(upload_request(method='GET'))
    assert json.loads(ready.get_body()) == {
        'status': 'ready', 'secret_present': False, 'upload_status': 'pending',
        'profile': 'synthetic', 'repository': 'rablaine/salesbuddy-failure-lab'}
    assert json.loads(state.blobs['token-upload.json'].data) == {'status': 'pending'}
    vault.set_secret.assert_not_called()
    response = handler(upload_request())
    assert response.status_code == 201
    assert json.loads(response.get_body()) == {'status': 'uploaded'}
    assert response.headers['Cache-Control'] == 'no-store'
    vault.set_secret.assert_called_once_with(
        'github-test-token', 'github_pat_' + 'A' * 32, logging_enable=False)
    assert 'github_pat' not in json.dumps(json.loads(state.blobs['token-upload.json'].data))
    assert handler(upload_request()).status_code == 201
    assert handler(upload_request(request_id='b' * 32)).status_code == 409
    vault.set_secret.assert_called_once()
    agent.assert_not_called()


def test_existing_secret_and_ambiguous_upload_cannot_be_overwritten(
        upload_module, upload_clients, monkeypatch, caplog):
    """Existing and uncertain writes are surfaced, never overwritten or leaked in logs."""
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    handler = upload_module.upload_token.build().get_user_function()
    state, vault, _ = upload_clients
    vault.get_secret.side_effect = None
    vault.get_secret.return_value = SimpleNamespace(value='not-to-be-returned')
    ready = handler(upload_request(method='GET'))
    assert json.loads(ready.get_body()) == {
        'status': 'ready', 'secret_present': True, 'upload_status': 'pending',
        'profile': 'synthetic', 'repository': 'rablaine/salesbuddy-failure-lab'}
    assert b'not-to-be-returned' not in ready.get_body()
    assert handler(upload_request()).status_code == 409
    vault.set_secret.assert_not_called()
    state.blobs.clear()
    vault.get_secret.side_effect = ResourceNotFoundError('Missing')
    vault.set_secret.side_effect = RuntimeError('credential github_pat_DO_NOT_LOG')
    response = handler(upload_request())
    assert response.status_code == 503
    assert 'DO_NOT_LOG' not in caplog.text
    assert handler(upload_request()).status_code == 409
    vault.set_secret.assert_called_once()


def test_upload_binding_requires_function_key_and_json(upload_module, upload_clients, monkeypatch):
    """The published endpoint must not become anonymous or accept other content types."""
    import azure.functions as func

    function = upload_module.upload_token.build()
    trigger = next(binding for binding in function.get_bindings() if binding.type == 'httpTrigger')
    assert trigger.get_dict_repr()['authLevel'] == func.AuthLevel.FUNCTION
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    response = function.get_user_function()(upload_request(content_type='text/plain'))
    assert response.status_code == 415
    upload_clients[2].assert_not_called()


def test_upload_known_permission_denial_can_retry_without_overwriting(
        upload_module, upload_clients, monkeypatch):
    """A definitive Azure 403 permits retry; only one successful secret write occurs."""
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    handler = upload_module.upload_token.build().get_user_function()
    state, vault, _ = upload_clients
    denied = HttpResponseError('Synthetic permission propagation delay')
    denied.status_code = 403
    vault.set_secret.side_effect = [denied, SimpleNamespace()]
    rejected = handler(upload_request())
    assert rejected.status_code == 503
    assert json.loads(rejected.get_body()) == {'error': 'permission_pending'}
    assert json.loads(state.blobs['token-upload.json'].data)['status'] == 'permission_pending'
    assert handler(upload_request()).status_code == 201
    assert handler(upload_request()).status_code == 201
    assert vault.set_secret.call_count == 2


def test_upload_busy_or_unpersisted_failure_does_not_start_secret_write(
        upload_module, upload_clients, monkeypatch):
    """Retrying startup/storage failures is safe because the uncertain-write barrier precedes set."""
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    handler = upload_module.upload_token.build().get_user_function()
    state, vault, _ = upload_clients
    gate = state.get_blob_client('token-upload.json')
    original_upload = gate.upload_blob
    gate.upload_blob = MagicMock(side_effect=TimeoutError('Synthetic initial blob outage'))
    assert handler(upload_request()).status_code == 503
    vault.set_secret.assert_not_called()
    gate.upload_blob = original_upload
    gate.locked = True
    assert json.loads(handler(upload_request()).get_body()) == {'error': 'busy'}
    vault.set_secret.assert_not_called()
    gate.locked = False
    assert handler(upload_request()).status_code == 201
    vault.set_secret.assert_called_once()


def test_upload_readiness_detects_storage_write_denial_without_a_token(
        upload_module, upload_clients, monkeypatch):
    """Readiness exposes safe Azure failure metadata before requesting a credential POST."""
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    handler = upload_module.upload_token.build().get_user_function()
    state, vault, _ = upload_clients
    denied = HttpResponseError('Synthetic private storage denial')
    denied.status_code = 403
    state.get_blob_client('token-upload.json').upload_blob = MagicMock(side_effect=denied)
    response = handler(upload_request(method='GET'))
    assert response.status_code == 503
    assert json.loads(response.get_body()) == {
        'error': 'preparation_failed', 'azure_http_status': 403}
    vault.set_secret.assert_not_called()


@pytest.mark.parametrize('production', [False, True])
@pytest.mark.parametrize(('readiness_codes', 'post_codes', 'expect_upload'), [
    ([410, 503, 200], [201], True),
    ([401], [], False),
    ([200], [410, 503, 201], True),
    ([200], [410] * 24, False),
    ([200], [503, 201], True),
    ([200], [-1, 201], True),
    ([200], [409], False),
])
def test_local_uploader_waits_for_restarted_config_and_always_cleans_up(
        readiness_codes, post_codes, expect_upload, production):
    """Run the real local script with fake Azure/HTTP boundaries and no real credential."""
    executable = shutil.which('pwsh')
    if not executable:
        pytest.skip('PowerShell 7 is required for the local uploader regression')
    uploader = str(LAB / 'set-lab-token.ps1').replace("'", "''")
    codes = ','.join(str(code) for code in readiness_codes)
    post_sequence = ','.join(str(code) for code in post_codes)
    script = """
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0
$global:codes = [Collections.Generic.Queue[int]]::new()
foreach ($code in @(__CODES__)) { $global:codes.Enqueue($code) }
$global:postCodes = [Collections.Generic.Queue[int]]::new()
foreach ($code in @(__POST_CODES__)) { $global:postCodes.Enqueue($code) }
$global:posts = 0
$global:requestIds = [Collections.Generic.HashSet[string]]::new()
$global:closed = $false
$global:revoked = $false
function az {
    $command = $args -join ' '
    if ($command.StartsWith('account show')) {
        return '{"tenantId":"96d12531-723e-46c1-842b-0480739c7419","id":"97aba503-25b6-46a2-8ed2-a8afc3bfbd23"}'
    }
    if ($command.StartsWith('functionapp identity show')) { return 'fake-principal' }
    if ($command.StartsWith('role assignment create')) { return '{"id":"fake-upload-role"}' }
    if ($command.StartsWith('functionapp function keys list')) { return '{"default":"fake-key"}' }
    if ($command.StartsWith('functionapp config appsettings set')) {
        if ($args -contains 'TOKEN_UPLOAD_EXPIRES_AT=') { $global:closed = $true }
        return
    }
    if ($command.StartsWith('role assignment delete')) {
        if (-not ($args -contains 'fake-upload-role')) { throw 'Wrong role revoked' }
        $global:revoked = $true
        return
    }
    throw 'Unexpected Azure operation'
}
function Read-Host {
    return ConvertTo-SecureString ('github_pat_' + ('A' * 32)) -AsPlainText -Force
}
function Start-Sleep {}
function Invoke-WebRequest {
    if ($args -contains 'Post') {
        $global:posts++
        if ($global:codes.Count) { throw 'Token was sent before readiness' }
        $headers = $args[[Array]::IndexOf($args, '-Headers') + 1]
        if ($headers['X-Lab-Upload-Id'] -notmatch '^[0-9a-f]{32}$') {
            throw 'Missing stable upload request ID'
        }
        if ($headers['X-Upload-Profile'] -ne '__PROFILE__') { throw 'Wrong upload profile' }
        $null = $global:requestIds.Add($headers['X-Lab-Upload-Id'])
        if ($global:requestIds.Count -ne 1) { throw 'Request ID changed during retries' }
        $code = $global:postCodes.Dequeue()
        if ($code -eq -1) { throw 'Synthetic lost POST response' }
        return [pscustomobject]@{ StatusCode = $code; Content = '{"status":"uploaded"}' }
    }
    $code = $global:codes.Dequeue()
    return [pscustomobject]@{
        StatusCode = $code
        Content = '{"status":"ready","secret_present":false,"profile":"__PROFILE__","repository":"__REPOSITORY__"}'
    }
}
$failed = $false
try { & '__UPLOADER__' __ARGUMENTS__ } catch { $failed = $true }
if (-not $global:closed -or -not $global:revoked) { throw 'Cleanup did not run' }
if ($global:posts -ne __POSTS__ -or $failed -ne $__FAILED__) {
    throw "Unexpected uploader outcome: posts=$global:posts failed=$failed"
}
""".replace('__CODES__', codes).replace('__POST_CODES__', post_sequence).replace(
        '__UPLOADER__', uploader).replace('__POSTS__', str(len(post_codes))).replace(
        '__FAILED__', 'false' if expect_upload else 'true').replace(
        '__PROFILE__', 'production' if production else 'synthetic').replace(
        '__REPOSITORY__', 'rablaine/SalesBuddy' if production else
        'rablaine/salesbuddy-failure-lab').replace(
        '__ARGUMENTS__', '-Production' if production else '')
    result = subprocess.run(
        [executable, '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def production_worker(worker, monkeypatch):
    """Use the unchanged intake validator and only in-memory production boundaries."""
    monkeypatch.setitem(sys.modules, 'service', svc)
    contract_spec = importlib.util.spec_from_file_location(
        'diagnostics_contract', ROOT / 'infra' / 'diagnostics' / 'service.py')
    contract = importlib.util.module_from_spec(contract_spec)
    contract_spec.loader.exec_module(contract)
    monkeypatch.setitem(sys.modules, 'diagnostics_contract', contract)
    spec = importlib.util.spec_from_file_location('production', LAB / 'production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, 'production', module)
    worker.transport.post.return_value = task_response()
    worker.transport.get.return_value = task_response(200)
    return module.ProductionInvestigationService(
        Container(), Container(), worker.credential, worker.transport,
        started_at=(svc.now() - timedelta(minutes=1)).isoformat(),
        mode='submit', token='fake-production-token')


def test_production_code_only_preview_does_not_require_directory_access():
    """The real deployer must not request Graph data or permission changes for code-only work."""
    executable = shutil.which('pwsh')
    if not executable:
        pytest.skip('PowerShell 7 is required for the deployer regression')
    deployer = str(ROOT / 'infra' / 'deploy-failure-investigation.ps1').replace("'", "''")
    script = """
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0
function az {
    if (($args -join ' ').StartsWith('account show')) {
        return '{"tenantId":"96d12531-723e-46c1-842b-0480739c7419","id":"97aba503-25b6-46a2-8ed2-a8afc3bfbd23"}'
    }
    throw 'Code-only preview requested directory access or a modifying Azure operation'
}
& '__DEPLOYER__' -Production -WhatIf
""".replace('__DEPLOYER__', deployer)
    result = subprocess.run(
        [executable, '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def production_report(worker, report_id=1, contact='reporter@example.org', target='a' * 40):
    """Create a validated technical report plus deliberately excluded raw attachment data."""
    payload = json.loads((LAB / 'synthetic_payload.json').read_text())
    report = payload['report']
    if target is not None:
        report['target_commit'] = target
    event = {
        'report_id': f'{report_id:032x}', 'kind': 'failure', 'content': report,
        'contact': contact, 'status': 'succeeded', 'created_at': svc.now().isoformat(),
        'attachments': [{'text': 'CONFIDENTIAL_CUSTOMER_SENTINEL github_pat_NEVER_FORWARD'}],
    }
    worker.reports.get_blob_client(f'reports/{event["report_id"]}.json').upload_blob(
        svc.canonical(event))
    return event


def production_jobs(worker) -> list:
    """Read admitted jobs without relying on the status display's pagination."""
    return [json.loads(blob.data) for name, blob in worker.state.blobs.items()
            if name.startswith('production/investigations/') and blob.data is not None]


def test_production_deduplicates_four_retries_across_restart(production_worker):
    """One signed-in user and target commit consume only one durable task admission."""
    worker = production_worker
    for number in range(1, 5):
        production_report(worker, number, contact=(
            'REPORTER@example.org' if number == 2 else 'reporter@example.org'))
    worker.tick()
    restarted = type(worker)(
        worker.reports, worker.state, worker.credential, worker.transport,
        started_at=worker.started_at, mode='submit', token='fake-production-token')
    restarted.tick()
    assert worker.transport.post.call_count == 1
    assert len(production_jobs(worker)) == 1
    receipts = [json.loads(blob.data)['status'] for name, blob in worker.state.blobs.items()
                if name.startswith('production/reports/')]
    assert receipts.count('admitted') == 1
    assert receipts.count('duplicate') == 3
    snapshot = worker.status_snapshot()
    assert datetime.fromisoformat(snapshot['last_tick_at']) >= datetime.fromisoformat(
        worker.started_at)
    persisted = b''.join(blob.data or b'' for blob in worker.state.blobs.values()).decode()
    assert 'reporter@example.org' not in persisted
    assert 'CONFIDENTIAL_CUSTOMER_SENTINEL' not in persisted
    assert 'github_pat_NEVER_FORWARD' not in persisted
    assert all(blob.writes == 1 for blob in worker.reports.blobs.values())


def test_production_allows_different_users_and_versions_without_daily_ceiling(production_worker):
    """Another reporter or another exact version can produce a separate review PR."""
    worker = production_worker
    production_report(worker, 1)
    production_report(worker, 2, contact='second@example.org')
    production_report(worker, 3, target='b' * 40)
    worker.tick()
    worker.tick()
    assert worker.transport.post.call_count == 3
    assert len(production_jobs(worker)) == 3
    for call in worker.transport.post.call_args_list:
        assert call.args[0].endswith('/agents/repos/rablaine/SalesBuddy/tasks')
        assert call.kwargs['json']['base_ref'] == 'main'
        assert call.kwargs['json']['create_pull_request'] is True
        assert 'CONFIDENTIAL_CUSTOMER_SENTINEL' not in call.kwargs['json']['prompt']


def test_production_uncertain_submission_survives_another_report(production_worker):
    """A lost POST result never causes another task for the same user/version."""
    worker = production_worker
    production_report(worker)
    worker.transport.post.side_effect = TimeoutError('Fake response loss')
    worker.tick()
    production_report(worker, 2)
    worker.tick()
    assert worker.transport.post.call_count == 1
    job = production_jobs(worker)[0]
    assert job['status'] == 'submission_uncertain'
    assert job['last_error'] == 'TimeoutError'


def test_production_completed_task_sends_verified_pr_link_once(production_worker, monkeypatch):
    """Production reuses confirmed email delivery without lab wording or reporter data."""
    worker = production_worker
    timestamp = svc.now()
    monkeypatch.setattr(svc, 'now', lambda: timestamp)
    monkeypatch.setattr(sys.modules['production'], 'now', lambda: timestamp)
    production_report(worker)
    worker.tick()
    worker.transport.get.return_value = task_response(200, 'completed', artifacts=[
        {'provider': 'github', 'type': 'pull', 'data': {'id': 5317297503}},
        {'provider': 'github', 'type': 'branch',
         'data': {'head_ref': 'copilot/fix-update', 'base_ref': 'main'}},
    ])
    worker.tick()
    worker.completion_email = {
        'endpoint': 'https://isolated-mail.unitedstates.communication.azure.com',
        'sender': 'sender@example.org', 'recipient': 'reviewer@example.org',
    }
    worker.credential.get_token.return_value = SimpleNamespace(token='fake-email-token')
    worker.transport.get.return_value = email_response(body=[{'id': 5317297503, 'number': 42}])
    worker.transport.post.return_value = email_response(202)
    worker.tick()
    message = worker.transport.post.call_args.kwargs['json']['content']
    assert message['subject'] == 'Sales Buddy: Copilot PR ready for review'
    assert 'https://github.com/rablaine/SalesBuddy/pull/42' in message['plainText']
    assert 'reporter@example.org' not in json.dumps(message)
    worker.transport.get.return_value = email_response(body={'status': 'Succeeded'})
    timestamp += timedelta(minutes=2)
    worker.tick()
    worker.tick()
    assert worker.transport.post.call_count == 2  # One task, one completion notification.
    assert production_jobs(worker)[0]['notification']['status'] == 'succeeded'


@pytest.mark.parametrize(('contact', 'target', 'reason'), [
    (None, 'a' * 40, 'missing_identity'),
    ('reporter@example.org', None, 'missing_target_version'),
])
def test_production_missing_identity_or_version_is_manual(
        production_worker, contact, target, reason):
    """Unknown user/version does not guess a deduplication identity or send a PR."""
    worker = production_worker
    event = production_report(worker, contact=contact, target=target)
    worker.tick()
    worker.tick()
    worker.transport.post.assert_not_called()
    receipt = json.loads(worker.state.blobs[f'production/reports/{event["report_id"]}.json'].data)
    assert receipt['status'] == 'manual_review'
    assert receipt['reason'] == reason


def test_production_waits_for_email_and_excludes_historical_backlog(production_worker):
    """Only post-cutoff reports with confirmed original email delivery are admitted."""
    worker = production_worker
    historical = production_report(worker, 1)
    historical['created_at'] = (datetime.fromisoformat(worker.started_at) -
                                timedelta(seconds=1)).isoformat()
    worker.reports.blobs[f'reports/{historical["report_id"]}.json'].data = svc.canonical(historical)
    pending = production_report(worker, 2)
    pending['status'] = 'pending'
    worker.reports.blobs[f'reports/{pending["report_id"]}.json'].data = svc.canonical(pending)
    worker.tick()
    worker.transport.post.assert_not_called()
    pending['status'] = 'succeeded'
    worker.reports.blobs[f'reports/{pending["report_id"]}.json'].data = svc.canonical(pending)
    worker.tick()
    worker.transport.post.assert_called_once()
    assert production_jobs(worker)[0]['report_id'] == pending['report_id']


def test_production_preview_and_poison_metadata_never_reach_github(production_worker):
    """Preview is network-free; private fields in technical evidence fail closed."""
    worker = production_worker
    worker.mode = 'preview'
    production_report(worker, 1)
    poison = production_report(worker, 2, contact='second@example.org')
    poison['content']['customer_name'] = 'CONFIDENTIAL_CUSTOMER_SENTINEL'
    worker.reports.blobs[f'reports/{poison["report_id"]}.json'].data = svc.canonical(poison)
    worker.tick()
    assert production_jobs(worker)[0]['status'] == 'preview'
    worker.transport.post.assert_not_called()
    worker.transport.get.assert_not_called()
    snapshot = worker.status_snapshot()
    assert any(item.get('reason') == 'invalid_metadata' for item in snapshot['reports'])
    assert 'CONFIDENTIAL_CUSTOMER_SENTINEL' not in json.dumps(snapshot)
    assert 'dedupe_salt' not in json.dumps(snapshot)
    assert 'prompt' not in json.dumps(snapshot)


def test_production_pages_do_not_starve_later_reports_or_jobs(production_worker):
    """Persist both cursors so busy/terminal early entries cannot hide later jobs."""
    worker = production_worker
    count = 23
    for number in range(1, count + 1):
        production_report(worker, number, contact=f'reporter{number}@example.org')
    for _ in range(12):
        worker.tick()
    assert worker.transport.post.call_count == count
    assert len(production_jobs(worker)) == count


def test_production_cutoff_cannot_be_repointed_after_admission(production_worker):
    """Changing the activation boundary cannot silently replay or exclude reports."""
    worker = production_worker
    worker.tick()
    worker.started_at = (datetime.fromisoformat(worker.started_at) -
                         timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match='settings cannot be changed'):
        worker.tick()
    worker.transport.post.assert_not_called()


def test_production_token_uses_separate_secret_and_receipt(
        upload_module, upload_clients, monkeypatch):
    """Promoting provisioning cannot overwrite or reuse the synthetic upload receipt."""
    monkeypatch.setenv('TOKEN_UPLOAD_EXPIRES_AT', (svc.now() + timedelta(minutes=10)).isoformat())
    monkeypatch.setenv('TOKEN_UPLOAD_PROFILE', 'production')
    state, vault, _ = upload_clients
    state.blobs['token-upload.json'] = Blob({'status': 'uploaded', 'request_id': 'old-lab'})
    handler = upload_module.upload_token.build().get_user_function()
    readiness = json.loads(handler(upload_request(method='GET')).get_body())
    assert readiness['profile'] == 'production'
    assert readiness['repository'] == 'rablaine/SalesBuddy'
    assert handler(upload_request()).status_code == 400
    vault.set_secret.assert_not_called()
    assert handler(upload_request(profile='production')).status_code == 201
    assert handler(upload_request(profile='production')).status_code == 201
    vault.set_secret.assert_called_once_with(
        'github-salesbuddy-token', 'github_pat_' + 'A' * 32, logging_enable=False)
    assert json.loads(state.blobs['token-upload.json'].data)['request_id'] == 'old-lab'
    assert json.loads(state.blobs['token-uploads/production.json'].data)['status'] == 'uploaded'
