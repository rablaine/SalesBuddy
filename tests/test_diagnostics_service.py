"""Offline contract, durable receipt and email tests for the standalone Function."""

import copy
import ast
import importlib.util
import json
import sys
import uuid
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import (
    HttpResponseError, ResourceExistsError, ResourceNotFoundError,
)

ROOT = Path(__file__).resolve().parents[1]
RATE_KEY = 'a' * 64
SPEC = importlib.util.spec_from_file_location(
    'diagnostics_service', ROOT / 'infra' / 'diagnostics' / 'service.py')
svc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(svc)


@pytest.fixture
def payload() -> dict:
    """Match the producer's actual failure_report structure."""
    return {
        'schema_version': 1, 'report_id': '0123456789abcdef0123456789abcdef',
        'report': {
            'schema_version': 1, 'operation': 'update', 'recovery': 'restored',
            'previous_commit': 'a' * 40, 'target_commit': 'b' * 40,
            'upgrade': {
                'phase': 'migrating',
                'error': {
                    'type': 'IntegrityError', 'category': 'migration_foreign_keys',
                    'frames': [{'file': 'migrations.py', 'line': 123}],
                    'parent_tables': ['customers', 'other'], 'violation_count': 2,
                    'exit_code': -1,
                },
            },
            'startup': {
                'phase': 'unknown',
                'error': {
                    'type': 'Error', 'category': 'operation_failed',
                    'frames': [], 'parent_tables': [],
                },
            },
        },
    }


class FakeBlob:
    """Model durable create-only writes and exclusive processing leases."""

    def __init__(self) -> None:
        """Start with no stored event."""
        self.data = None
        self.locked = False
        self.uploads = []

    def upload_blob(self, data, overwrite=False, lease=None) -> None:
        """Store bytes, enforcing create-only conflicts."""
        if self.data is not None and not overwrite:
            raise ResourceExistsError('exists')
        self.data = bytes(data)
        self.uploads.append(json.loads(data))

    def download_blob(self, lease=None):
        """Read the current persisted bytes."""
        if self.data is None:
            raise ResourceNotFoundError('missing')
        return SimpleNamespace(readall=lambda: self.data)

    def get_blob_properties(self):
        """Require an existing blob."""
        if self.data is None:
            raise ResourceNotFoundError('missing')
        return SimpleNamespace()

    def acquire_lease(self, lease_duration):
        """Exclude concurrent event processors."""
        if self.locked:
            error = HttpResponseError('leased')
            error.status_code = 409
            raise error
        if self.data is None:
            raise ResourceNotFoundError('missing')
        self.locked = True

        def release() -> None:
            """Release the fake lock."""
            self.locked = False

        return SimpleNamespace(release=release, renew=lambda: None)

    def delete_blob(self, lease=None) -> None:
        """Delete an expired event."""
        self.data = None


class FakeContainer:
    """Keep independent report and contact blobs."""

    def __init__(self) -> None:
        """Start empty."""
        self.blobs = {}

    def get_blob_client(self, name):
        """Return a stable fake blob client."""
        return self.blobs.setdefault(name, FakeBlob())

    def list_blobs(self, name_starts_with):
        """List existing durable events only."""
        return [SimpleNamespace(name=name) for name, blob in self.blobs.items()
                if name.startswith(name_starts_with) and blob.data is not None]


@pytest.fixture
def service():
    """Inject all cloud boundaries, preventing network access."""
    result = svc.DiagnosticsService(
        FakeContainer(), MagicMock(), MagicMock(), 'https://example.communication.azure.com',
        'sender@example.com', 'support@example.com', MagicMock())
    result.accept = partial(result.accept, rate_key=RATE_KEY)
    return result


def stored(service, payload, followup=False) -> dict:
    """Read a persisted fake event."""
    name = svc.event_name(payload['report_id'], followup)
    return json.loads(service.container.get_blob_client(name).data)


def make_due(service, payload, followup=False) -> None:
    """Allow a subsequent timer poll without sleeping."""
    name = svc.event_name(payload['report_id'], followup)
    blob = service.container.get_blob_client(name)
    event = json.loads(blob.data)
    event.pop('next_attempt_at', None)
    blob.data = svc.canonical(event)


def response(code=202, status='Running'):
    """Make a bounded fake ACS response."""
    result = MagicMock(status_code=code)
    result.json.return_value = {'status': status}
    if code >= 400:
        result.raise_for_status.side_effect = RuntimeError('http error')
    return result


def test_exact_producer_shape(payload):
    """Accept exact producer output, not a simplified imitation."""
    spec = importlib.util.spec_from_file_location('diagnostics_upgrade', ROOT / 'scripts' /
                                                'upgrade.py')
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    state = {
        'phase': 'restored', 'previous_commit': 'a' * 40, 'target_commit': 'b' * 40,
        'original_failure': {'phase': 'migrating', 'error': payload['report']['upgrade']['error']},
        'startup_failure': {},
    }
    payload['report'] = upgrade.failure_report(ROOT, state)
    assert svc.parse_payload(svc.canonical(payload)) == payload


@pytest.mark.parametrize(('path', 'value'), [
    (('email',), 'someone@microsoft.com'),
    (('report', 'message'), 'private customer data'),
    (('report', 'upgrade', 'error', 'message'), 'password'),
    (('report', 'upgrade', 'phase'), 'private phase'),
    (('report', 'upgrade', 'error', 'type'), 'CustomerSecret'),
    (('report', 'upgrade', 'error', 'category'), 'private category'),
    (('report', 'upgrade', 'error', 'parent_tables'), ['private_table']),
    (('report', 'upgrade', 'error', 'frames'), [{'file': 'C:\\secret.py', 'line': 1}]),
    (('report', 'upgrade', 'error', 'frames'), [{'file': 'migrations.py', 'line': True}]),
    (('report', 'upgrade', 'error', 'violation_count'), -1),
    (('report', 'upgrade', 'error', 'exit_code'), 1000000001),
    (('schema_version',), True),
    (('report_id',), '../secret'),
    (('report', 'previous_commit'), 'private branch'),
    (('report', 'recovery'), []),
])
def test_reject_sensitive_unknown_or_wrong_type(payload, path, value):
    """Reject unsafe metadata at each trust boundary."""
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(svc.InvalidPayload):
        svc.parse_payload(svc.canonical(payload))


@pytest.mark.parametrize('body', [
    b'{' * 4000, b'x' * 16385, b'\xff', b'{"schema_version":1,"schema_version":1}',
    b'null', b'[]', b'{"report_id":NaN}',
], ids=['nested', 'oversized', 'encoding', 'duplicate', 'null', 'array', 'nan'])
def test_malformed_or_unbounded_json(body):
    """Reject duplicate keys, invalid encodings and excessive nesting or length."""
    with pytest.raises(svc.InvalidPayload):
        svc.parse_payload(body)


def test_bounded_children(payload):
    """Accept only readiness counters and web/worker identities."""
    failure = payload['report']['startup']
    failure['children'] = [{'name': 'web', 'attempts': 2, 'ready': False},
                           {'name': 'worker', 'attempts': 1, 'ready': True}]
    assert svc.parse_payload(svc.canonical(payload)) == payload
    failure['children'][1]['name'] = 'web'
    with pytest.raises(svc.InvalidPayload):
        svc.parse_payload(svc.canonical(payload))


def test_durable_dedupe_and_conflict(service, payload):
    """Retrying identical content cannot create another event."""
    service.accept(payload)
    original = stored(service, payload)
    service.accept(copy.deepcopy(payload))
    assert stored(service, payload) == original
    payload['report']['recovery'] = 'incomplete'
    with pytest.raises(svc.Conflict):
        service.accept(payload)
    assert stored(service, payload) == original


def test_queue_failure_repaired_and_only_succeeded_is_success(service, payload):
    """A lost queue wakeup does not lose the durable event."""
    service.queue.send_message.side_effect = RuntimeError('offline')
    service.transport.post.return_value = response()
    service.accept(payload)
    assert stored(service, payload)['status'] == 'pending'
    service.repair()
    assert stored(service, payload)['status'] == 'submitted'
    make_due(service, payload)
    service.transport.get.return_value = response(200, 'Running')
    service.repair()
    assert stored(service, payload)['status'] == 'submitted'
    make_due(service, payload)
    service.transport.get.return_value = response(200, 'Succeeded')
    service.repair()
    assert stored(service, payload)['status'] == 'succeeded'
    service.repair()
    assert service.transport.post.call_count == 1


def test_storage_failure_never_receipted(service, payload):
    """No notification occurs before durable creation succeeds."""
    blob = service.container.get_blob_client(svc.event_name(payload['report_id']))
    blob.upload_blob = MagicMock(side_effect=RuntimeError('storage down'))
    with pytest.raises(RuntimeError):
        service.accept(payload)
    service.queue.send_message.assert_not_called()


def test_optin_is_separate_and_has_fixed_recipient(service, payload):
    """Initial failures have no identity; followup is a separately linked event."""
    service.accept(payload)
    assert [name for name in service.container.blobs if not name.startswith('rate/')] == [
        svc.event_name(payload['report_id'])]
    anonymous = stored(service, payload)
    assert '@' not in json.dumps(anonymous)
    followup = {'schema_version': 1, 'report_id': payload['report_id']}
    service.accept(followup, 'Person@Microsoft.com')
    contact = stored(service, payload, True)
    assert contact['content'] == {'contact': 'person@microsoft.com'}
    assert stored(service, payload) == anonymous
    assert contact['operation_id'] != anonymous['operation_id']
    service.accept(followup, 'person@microsoft.com')
    with pytest.raises(svc.Conflict):
        service.accept(followup, 'other@microsoft.com')
    email = svc.build_email(contact, service.sender, service.support)
    assert email['recipients'] == {'to': [{'address': 'support@example.com'}]}
    assert 'person@microsoft.com' in email['content']['plainText']
    assert payload['report_id'] in email['content']['subject']
    assert 'contact' not in svc.build_email(anonymous, service.sender,
                                          service.support)['content']['plainText'].lower()


@pytest.mark.parametrize('contact', [
    '', 'user@', 'user@-invalid.example', 'a@microsoft.com\nBcc:x@y.com',
    'Name <a@microsoft.com>', 'a..b@microsoft.com', 'a.@microsoft.com',
    'x' * 65 + '@example.com',
])
def test_contact_rejected(contact):
    """Reject unusable claims and mail header injection without requiring a domain."""
    with pytest.raises(svc.InvalidContact):
        svc.trusted_contact(contact)


@pytest.mark.parametrize('contact', ['person@microsoft.com', 'guest@example.org',
                                     'first.last+tag@partner.example'])
def test_validated_guest_domain_contact(contact):
    """Trust APIM tenant validation, allowing legitimate guest email domains."""
    assert svc.trusted_contact(contact) == contact


def test_followup_before_report_and_payload_identity(service, payload):
    """Opt-in may precede the report but cannot provide an email in JSON."""
    followup = {'schema_version': 1, 'report_id': payload['report_id']}
    service.accept(followup, 'person@microsoft.com')
    assert stored(service, payload, True)['content']['contact'] == 'person@microsoft.com'
    assert svc.event_name(payload['report_id']) not in service.container.blobs
    service.accept(payload)
    assert '@' not in json.dumps(stored(service, payload))
    followup['email'] = 'person@microsoft.com'
    with pytest.raises(svc.InvalidPayload):
        svc.parse_payload(svc.canonical(followup), True)


def test_uncertain_send_polls_stable_id_before_retry(service, payload):
    """A crash/timeout after ACS submission never mints another operation ID."""
    service.accept(payload)
    service.transport.post.side_effect = RuntimeError('timeout')
    service.repair()
    event = stored(service, payload)
    assert event['status'] == 'submitting'
    assert event['operation_id'] == str(uuid.UUID(hex=payload['report_id']))
    assert event['errors'] == 1
    make_due(service, payload)
    service.transport.get.return_value = response(200, 'Succeeded')
    service.repair()
    assert stored(service, payload)['status'] == 'succeeded'
    assert service.transport.post.call_count == 1
    kwargs = service.transport.post.call_args.kwargs
    assert kwargs['allow_redirects'] is False
    assert kwargs['headers']['Operation-Id'] == event['operation_id']
    service.credential.get_token.assert_called_with('https://communication.azure.com/.default')


def test_missing_operation_resubmits_same_id(service, payload):
    """An unobserved operation can retry, retaining its stable ACS identifier."""
    service.accept(payload)
    service.transport.post.side_effect = RuntimeError('timeout')
    service.repair()
    make_due(service, payload)
    service.transport.post.side_effect = None
    service.transport.post.return_value = response(409)
    service.transport.get.return_value = response(404)
    service.repair()
    assert stored(service, payload)['status'] == 'submitted'
    assert service.transport.post.call_count == 2
    ids = [call.kwargs['headers']['Operation-Id']
           for call in service.transport.post.call_args_list]
    assert ids[0] == ids[1]


def test_retry_budget_and_lease_exclusion(service, payload):
    """Retries are bounded and concurrent processing skips leased events."""
    service.accept(payload)
    blob = service.container.get_blob_client(svc.event_name(payload['report_id']))
    blob.locked = True
    service.repair()
    service.transport.post.assert_not_called()
    blob.locked = False
    service.transport.post.side_effect = RuntimeError('offline')
    service.transport.get.side_effect = RuntimeError('offline')
    for _ in range(12):
        make_due(service, payload)
        service.repair()
    assert stored(service, payload)['status'] == 'failed'
    assert blob.locked is False


def test_missing_operation_submission_budget(service, payload):
    """Repeated missing ACS operations cannot cause unlimited submissions."""
    service.accept(payload)
    service.transport.post.return_value = response()
    service.transport.get.return_value = response(404)
    for _ in range(13):
        make_due(service, payload)
        service.repair()
    assert service.transport.post.call_count == 12
    assert stored(service, payload)['status'] == 'failed'


@pytest.mark.parametrize('status', ['Failed', 'Canceled', 'Succeeded'])
def test_terminal_acs_status(service, payload, status):
    """Only ACS Succeeded is treated as success."""
    service.accept(payload)
    service.transport.post.return_value = response()
    service.repair()
    make_due(service, payload)
    service.transport.get.return_value = response(200, status)
    service.repair()
    expected = 'succeeded' if status == 'Succeeded' else 'failed'
    assert stored(service, payload)['status'] == expected


def test_retention_deletes_both_types_without_email(service, payload):
    """Contact opt-ins and anonymous reports each expire after 30 days."""
    service.accept(payload)
    service.accept({'report_id': payload['report_id']}, 'person@microsoft.com')
    for followup in (False, True):
        blob = service.container.get_blob_client(svc.event_name(payload['report_id'], followup))
        event = json.loads(blob.data)
        event['created_at'] = (svc.now() - timedelta(days=30, seconds=1)).isoformat()
        blob.data = svc.canonical(event)
    service.repair()
    assert all(blob.data is None for name, blob in service.container.blobs.items()
               if not name.startswith('rate/'))
    service.transport.post.assert_not_called()


def test_http_identity_receipt_and_no_header_identity_storage(monkeypatch, service, payload):
    """Exercise actual Functions handlers with no cloud calls."""
    import azure.functions as func

    monkeypatch.setitem(sys.modules, 'service', svc)
    spec = importlib.util.spec_from_file_location(
        'diagnostics_function_app', ROOT / 'infra' / 'diagnostics' / 'function_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'service', lambda: service)
    monkeypatch.setenv('APIM_PRINCIPAL_ID', 'apim-object-id')

    def request(body, headers=None):
        """Create a Functions HTTP request."""
        return func.HttpRequest('POST', 'https://backend/api/reports',
                                headers=headers or {}, body=svc.canonical(body))

    assert module.receive(request(payload), False).status_code == 403
    headers = {'X-MS-CLIENT-PRINCIPAL-ID': 'apim-object-id',
               'X-SalesBuddy-Rate-Key': RATE_KEY,
               'X-SalesBuddy-Contact': 'person@microsoft.com'}
    result = module.receive(request(payload, headers), False)
    assert result.status_code == 202
    assert json.loads(result.get_body()) == {
        'report_id': payload['report_id'], 'status': 'accepted'}
    assert result.mimetype == 'application/json'
    assert 'person' not in json.dumps(stored(service, payload))
    followup = {'schema_version': 1, 'report_id': payload['report_id']}
    assert module.receive(request(followup, headers), True).status_code == 202
    assert stored(service, payload, True)['content']['contact'] == 'person@microsoft.com'
    missing_contact = module.receive(request(followup, {
        'X-MS-CLIENT-PRINCIPAL-ID': 'apim-object-id',
        'X-SalesBuddy-Rate-Key': RATE_KEY}), True)
    assert missing_contact.status_code == 422
    assert json.loads(missing_contact.get_body()) == {
        'error': 'contact_email_unavailable',
        'message': 'No usable email contact claim is available.'}
    payload['report']['recovery'] = 'incomplete'
    assert module.receive(request(payload, headers), False).status_code == 409


def test_http_storage_failure_and_followup_before_report(monkeypatch, service, payload):
    """HTTP acknowledges independently durable opt-ins and reports storage failure honestly."""
    import azure.functions as func

    monkeypatch.setitem(sys.modules, 'service', svc)
    spec = importlib.util.spec_from_file_location(
        'diagnostics_function_http', ROOT / 'infra' / 'diagnostics' / 'function_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'service', lambda: service)
    monkeypatch.setenv('APIM_PRINCIPAL_ID', 'apim-object-id')
    headers = {'X-MS-CLIENT-PRINCIPAL-ID': 'apim-object-id',
               'X-SalesBuddy-Rate-Key': RATE_KEY,
               'X-SalesBuddy-Contact': 'person@microsoft.com'}
    followup = {'schema_version': 1, 'report_id': payload['report_id']}
    request = func.HttpRequest('POST', 'https://backend/api/followups', headers=headers,
                               body=svc.canonical(followup))
    assert module.receive(request, True).status_code == 202
    service.accept = MagicMock(side_effect=RuntimeError('storage offline'))
    assert module.receive(request, True).status_code == 503
    health = module.health.build().get_user_function()
    assert health(request).get_body() == b'ok'
    monkeypatch.delenv('APIM_PRINCIPAL_ID')
    assert health(request).status_code == 403


def test_queue_connection_and_independent_imports(monkeypatch):
    """Verify identity-based binding metadata and matching SDK client settings offline."""
    import azure.functions as func
    import azure.identity
    import azure.storage.blob
    import azure.storage.queue

    settings = {
        'REPORT_STORAGE_URL': 'https://stsalesbuddydiag.blob.core.windows.net/',
        'REPORT_CONTAINER': 'diagnostics',
        'REPORT_QUEUE__queueServiceUri': 'https://stsalesbuddydiag.queue.core.windows.net',
        'REPORT_QUEUE__credential': 'managedidentity',
        'DIAGNOSTICS_QUEUE_NAME': 'diagnostics',
        'AzureWebJobsStorage__accountName': 'stsalesbuddydiag',
        'ACS_ENDPOINT': 'https://example.communication.azure.com',
        'ACS_SENDER': 'DoNotReply@example.azurecomm.net',
        'SUPPORT_EMAIL': 'alex.blaine@microsoft.com',
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    credential = MagicMock()
    monkeypatch.setattr(azure.identity, 'DefaultAzureCredential', lambda: credential)
    blobs = MagicMock()
    queues = MagicMock()
    monkeypatch.setattr(azure.storage.blob, 'BlobServiceClient', blobs)
    monkeypatch.setattr(azure.storage.queue, 'QueueClient', queues)
    configured = svc.configured_service()
    queues.assert_called_once_with(
        settings['REPORT_QUEUE__queueServiceUri'], 'diagnostics', credential=credential,
        retry_total=0, connection_timeout=5, read_timeout=10)
    blobs.return_value.get_container_client.assert_called_once_with('diagnostics')
    assert configured.queue is queues.return_value
    monkeypatch.setitem(sys.modules, 'service', svc)
    spec = importlib.util.spec_from_file_location(
        'diagnostics_function_metadata', ROOT / 'infra' / 'diagnostics' / 'function_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    functions = {function.get_function_name(): function for function in module.app.get_functions()}
    assert set(functions) == {'reports', 'followups', 'health', 'dispatch', 'repair'}
    binding = next(binding for binding in functions['dispatch'].get_bindings()
                   if binding.type == 'queueTrigger')
    metadata = binding.get_dict_repr()
    assert metadata['connection'] == 'REPORT_QUEUE'
    assert metadata['queueName'] == '%DIAGNOSTICS_QUEUE_NAME%'
    for name in ('reports', 'followups', 'health'):
        trigger = next(binding for binding in functions[name].get_bindings()
                       if binding.type == 'httpTrigger')
        assert trigger.get_dict_repr()['authLevel'] == func.AuthLevel.ANONYMOUS
    for filename in ('service.py', 'function_app.py'):
        tree = ast.parse((ROOT / 'infra' / 'diagnostics' / filename).read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or '')
        assert not any(name.split('.')[0] in {'app', 'scripts', 'tests'} for name in imports)


def counter_blob(service, rate_key=RATE_KEY):
    """Locate today's durable hashed-caller counter."""
    return service.container.get_blob_client(
        f'rate/{svc.now().date().isoformat()}/{rate_key}.json')


def test_daily_budget_deduplication_and_separate_callers(service, payload):
    """Charge only new events and keep identical retry receipts available."""
    first = copy.deepcopy(payload)
    for number in range(20):
        payload['report_id'] = f'{number:032x}'
        service.accept(payload)
    assert json.loads(counter_blob(service).data)['count'] == 20
    service.accept(payload)
    assert json.loads(counter_blob(service).data)['count'] == 20
    first['report_id'] = 'f' * 32
    with pytest.raises(svc.RateLimited) as error:
        service.accept(first)
    assert 1 <= error.value.retry_after <= 86400
    assert service.container.get_blob_client(svc.event_name(first['report_id'])).data is None
    service.accept(first, rate_key='b' * 64)
    assert json.loads(counter_blob(service, 'b' * 64).data)['count'] == 1
    service.accept(first)
    assert json.loads(counter_blob(service).data)['count'] == 20


def test_followup_and_report_share_daily_budget(service, payload):
    """Anonymous failures and separate contact events share the same admission quota."""
    service.accept(payload)
    service.accept({'report_id': payload['report_id']}, 'person@microsoft.com')
    assert json.loads(counter_blob(service).data)['count'] == 2
    service.accept({'report_id': payload['report_id']}, 'person@microsoft.com')
    assert json.loads(counter_blob(service).data)['count'] == 2
    assert set(json.loads(counter_blob(service).data)) == {'count', 'created_at'}
    assert RATE_KEY not in json.dumps(stored(service, payload))
    assert RATE_KEY not in json.dumps(stored(service, payload, True))


def test_concurrent_counter_lease_and_duplicate_bypass(service, payload):
    """A held caller lease blocks new events, but never blocks stored duplicates."""
    service.accept(payload)
    counter = counter_blob(service)
    counter.locked = True
    service.accept(payload)
    other = copy.deepcopy(payload)
    other['report_id'] = 'f' * 32
    with pytest.raises(svc.RateLimited) as error:
        service.accept(other)
    assert error.value.retry_after == 1
    assert json.loads(counter.data)['count'] == 1
    counter.locked = False
    service.accept(other)
    assert json.loads(counter.data)['count'] == 2
    assert counter.locked is False


def test_counter_daily_rollover_and_30_day_retention(service, payload, monkeypatch):
    """UTC days get separate budgets; hashed caller counters expire after 30 days."""
    timestamp = svc.now()
    monkeypatch.setattr(svc, 'now', lambda: timestamp)
    first, lease = service.rate_counter(RATE_KEY)
    lease.release()
    monkeypatch.setattr(svc, 'now', lambda: timestamp + timedelta(days=1))
    second, lease = service.rate_counter(RATE_KEY)
    lease.release()
    assert first is not second
    monkeypatch.setattr(svc, 'now', lambda: timestamp + timedelta(days=30, seconds=1))
    service.expire_counters()
    assert first.data is None
    assert second.data is not None
    service.transport.post.assert_not_called()


@pytest.mark.parametrize('rate_key', ['', 'oid-plain-text', 'A' * 64, 'a' * 63, None])
def test_invalid_rate_key_cannot_create_event_or_counter(service, payload, rate_key):
    """Only the APIM-overwritten lowercase SHA256 value is admissible."""
    with pytest.raises(svc.InvalidPayload):
        service.accept(payload, rate_key=rate_key)
    assert not service.container.blobs


def test_http_rate_limit_and_health_exemption(monkeypatch, service, payload):
    """Expose 429 and Retry-After while exempting health from caller throttling."""
    import azure.functions as func

    monkeypatch.setitem(sys.modules, 'service', svc)
    spec = importlib.util.spec_from_file_location(
        'diagnostics_rate_http', ROOT / 'infra' / 'diagnostics' / 'function_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'service', lambda: service)
    monkeypatch.setenv('APIM_PRINCIPAL_ID', 'apim-object-id')
    headers = {'X-MS-CLIENT-PRINCIPAL-ID': 'apim-object-id'}

    def request():
        """Create an intake request from the current test headers."""
        return func.HttpRequest('POST', 'https://backend/api/reports', headers=headers,
                                body=svc.canonical(payload))

    assert module.receive(request(), False).status_code == 400
    assert not service.container.blobs
    health = module.health.build().get_user_function()
    assert health(request()).status_code == 200
    headers['X-SalesBuddy-Rate-Key'] = RATE_KEY
    service.accept(payload)
    blob = counter_blob(service)
    event = json.loads(blob.data)
    event['count'] = 20
    blob.data = svc.canonical(event)
    assert module.receive(request(), False).status_code == 202
    payload['report_id'] = 'f' * 32
    result = module.receive(request(), False)
    assert result.status_code == 429
    assert int(result.headers['Retry-After']) > 0
    assert json.loads(result.get_body()) == {'error': 'submission_limit_reached'}
    assert health(request()).status_code == 200
