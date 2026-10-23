"""Bounded, anonymous diagnostics intake and durable ACS email delivery."""

import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_BODY = 16384
DAILY_EVENT_LIMIT = 20
RETENTION = timedelta(days=30)
PHASES = {
    'preparing', 'prepared', 'code', 'dependencies', 'rehearsing', 'migrating',
    'starting', 'failed', 'restoring', 'ready', 'restored', 'shell', 'startup',
    'preflight', 'activating', 'unknown',
}
CATEGORIES = {
    'operation_failed', 'dependency_missing', 'migration_foreign_keys',
    'database_locked', 'backup_failed', 'disk_space', 'permission', 'timeout',
    'rehearsal_failed',
}
ERROR_TYPES = {
    'Error', 'RuntimeError', 'ValueError', 'TimeoutError', 'PermissionError',
    'OSError', 'IntegrityError', 'OperationalError', 'ModuleNotFoundError',
    'CalledProcessError', 'StartupFailure', 'RehearsalError',
}
TABLES = {
    'customers', 'notes', 'milestones', 'msx_tasks', 'engagements',
    '_old_call_logs', 'call_logs_backup', '_old_milestones',
    'manager_initiative_items', 'initiative_discussed_points', 'other',
}
FILES = {
    'upgrade.py', 'migrations.py', 'supervisor.py', '__init__.py', 'models.py',
    'db_paths.py', 'database_startup.py', 'upgrade_rehearsal.py', 'worker.py',
    'main.js', 'status.js', 'upgrade-safety.js',
}
EMAIL = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)
API_VERSION = '2023-03-31'


class InvalidPayload(ValueError):
    """Indicate a rejected payload without retaining its sensitive contents."""


class Conflict(ValueError):
    """Indicate reuse of an identifier for different content."""


class InvalidContact(InvalidPayload):
    """Indicate a trusted identity with no usable email contact claim."""


class RateLimited(ValueError):
    """Reject new events when a caller's durable daily budget is exhausted."""

    def __init__(self, retry_after: int) -> None:
        """Expose only a safe retry delay, never the caller's hashed identifier."""
        super().__init__('Diagnostics submission limit reached')
        self.retry_after = retry_after


def now() -> datetime:
    """Return an aware server timestamp."""
    return datetime.now(timezone.utc)


def canonical(value: Any) -> bytes:
    """Encode deterministic JSON for hashing and durable events."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def require(condition: bool) -> None:
    """Reject a value without echoing untrusted input."""
    if not condition:
        raise InvalidPayload('Invalid diagnostics payload')


def fields(value: Any, required: set, optional: set | None = None) -> None:
    """Enforce exact object keys at every nesting level."""
    require(type(value) is dict)
    require(required <= value.keys() <= required | (optional or set()))


def integer(value: Any, minimum: int, maximum: int) -> None:
    """Reject booleans and oversized integers."""
    require(type(value) is int and minimum <= value <= maximum)


def allowed(value: Any, choices: set) -> None:
    """Require a string from a fixed non-sensitive vocabulary."""
    require(type(value) is str and value in choices)


def validate_failure(value: Any) -> None:
    """Re-allowlist the exact upgrade failure metadata and bounded children."""
    fields(value, {'phase', 'error'}, {'children'})
    allowed(value['phase'], PHASES)
    error = value['error']
    fields(error, {'type', 'category', 'frames', 'parent_tables'},
           {'exit_code', 'violation_count'})
    allowed(error['type'], ERROR_TYPES)
    allowed(error['category'], CATEGORIES)
    for key in ('exit_code', 'violation_count'):
        if key in error:
            integer(error[key], -1000000000 if key == 'exit_code' else 0, 1000000000)
    tables = error['parent_tables']
    require(type(tables) is list and len(tables) <= 32)
    for table in tables:
        allowed(table, TABLES)
    frames = error['frames']
    require(type(frames) is list and len(frames) <= 16)
    for frame in frames:
        fields(frame, {'file', 'line'})
        allowed(frame['file'], FILES)
        integer(frame['line'], 1, 100000)
    children = value.get('children', [])
    require(type(children) is list and len(children) <= 2)
    names = set()
    for child in children:
        fields(child, {'name', 'attempts', 'ready'})
        allowed(child['name'], {'web', 'worker'})
        require(child['name'] not in names)
        names.add(child['name'])
        integer(child['attempts'], 0, 1000000)
        require(type(child['ready']) is bool)


def parse_payload(body: bytes, followup: bool = False) -> dict:
    """Parse bounded UTF-8 JSON, rejecting duplicate keys and unknown fields."""
    require(len(body) <= MAX_BODY)

    def unique(pairs: list) -> dict:
        """Reject JSON duplicate keys instead of accepting ambiguous content."""
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    try:
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidPayload('Invalid diagnostics payload') from None
    fields(payload, {'schema_version', 'report_id'} if followup else
           {'schema_version', 'report_id', 'report'})
    require(type(payload['schema_version']) is int and payload['schema_version'] == 1)
    report_id = payload['report_id']
    require(type(report_id) is str and re.fullmatch(r'[0-9a-f]{32}', report_id) is not None)
    if followup:
        return payload
    report = payload['report']
    sections = {'upgrade', 'startup', 'recovery_failure'}
    fields(report, {'schema_version', 'operation', 'recovery'},
           sections | {'previous_commit', 'target_commit'})
    require(type(report['schema_version']) is int and report['schema_version'] == 1)
    allowed(report['operation'], {'update'})
    allowed(report['recovery'], {'restored', 'incomplete'})
    require(bool(sections & report.keys()))
    for key in ('previous_commit', 'target_commit'):
        if key in report:
            require(type(report[key]) is str and
                    re.fullmatch(r'[0-9a-f]{40,64}', report[key]) is not None)
    for key in sections & report.keys():
        validate_failure(report[key])
    return payload


def trusted_contact(value: str) -> str:
    """Accept a usable APIM claim; tenant authentication, not domain, establishes trust."""
    if (type(value) is not str or len(value) > 254 or
            EMAIL.fullmatch(value) is None or len(value.split('@')[0]) > 64):
        raise InvalidContact('No usable email claim is available')
    return value.lower()


def validate_rate_key(value: str) -> str:
    """Require the APIM-overwritten SHA256 of its authenticated caller oid."""
    require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None)
    return value


def authorized(headers: Any, principal_id: str) -> bool:
    """Bind EasyAuth's platform-generated caller to the configured APIM identity."""
    actual = headers.get('X-MS-CLIENT-PRINCIPAL-ID', '')
    return bool(principal_id and actual and
                hmac.compare_digest(actual.lower(), principal_id.lower()))


def event_name(report_id: str, followup: bool = False) -> str:
    """Separate anonymous reports from explicitly opted-in contacts."""
    return f'{"contacts" if followup else "reports"}/{report_id}.json'


def create_event(payload: dict, contact: str | None = None) -> dict:
    """Build a durable event containing only validated report or contact fields."""
    report_id = payload['report_id']
    followup = contact is not None
    content = {'contact': trusted_contact(contact)} if followup else payload['report']
    operation = (uuid.uuid5(uuid.NAMESPACE_URL, f'salesbuddy:followup:{report_id}')
                 if followup else uuid.UUID(hex=report_id))
    return {
        'report_id': report_id, 'kind': 'followup' if followup else 'failure',
        'content': content, 'hash': hashlib.sha256(canonical(content)).hexdigest(),
        'created_at': now().isoformat(), 'operation_id': str(operation),
        'status': 'pending', 'errors': 0, 'polls': 0, 'submissions': 0,
    }


def build_email(event: dict, sender: str, support: str) -> dict:
    """Address only the fixed operator-configured mailbox, with no attachments."""
    report_id = event['report_id']
    text = (f"Contact opt-in for report {report_id}\n"
            f"Contact: {event['content']['contact']}" if event['kind'] == 'followup'
            else f"Anonymous update failure report {report_id}\n" +
            json.dumps(event['content'], indent=2, sort_keys=True))
    return {
        'senderAddress': sender,
        'recipients': {'to': [{'address': support}]},
        'content': {'subject': f"Sales Buddy {event['kind']} report {report_id}",
                    'plainText': text},
    }


class DiagnosticsService:
    """Use create-only blobs for receipts and leased events for email delivery."""

    def __init__(self, container: Any, queue: Any, credential: Any, endpoint: str,
                 sender: str, support: str, transport: Any) -> None:
        """Inject cloud dependencies to keep all tests offline."""
        self.container = container
        self.queue = queue
        self.credential = credential
        self.endpoint = endpoint.rstrip('/')
        self.sender = sender
        self.support = support
        self.transport = transport

    def accept(self, payload: dict, contact: str | None = None, *,
               rate_key: str) -> None:
        """Acknowledge only after durable creation, tolerating lost queue wakeups."""
        from azure.core.exceptions import ResourceExistsError

        validate_rate_key(rate_key)
        report_id = payload['report_id']
        event = create_event(payload, contact)
        name = event_name(report_id, contact is not None)
        blob = self.container.get_blob_client(name)
        if not self.existing_event(blob, event):
            counter, lease = self.rate_counter(rate_key)
            try:
                # Recheck after acquiring the caller's lock so duplicate retries
                # neither consume budget nor fail when the budget is exhausted.
                if not self.existing_event(blob, event):
                    budget = json.loads(counter.download_blob(lease=lease).readall())
                    if budget['count'] >= DAILY_EVENT_LIMIT:
                        tomorrow = (now() + timedelta(days=1)).replace(
                            hour=0, minute=0, second=0, microsecond=0)
                        raise RateLimited(max(1, int((tomorrow - now()).total_seconds())))
                    budget['count'] += 1
                    # Reserve first: failure between these writes can consume a
                    # slot, but cannot allow more than the daily limit.
                    counter.upload_blob(canonical(budget), overwrite=True, lease=lease)
                    try:
                        blob.upload_blob(canonical(event), overwrite=False)
                    except ResourceExistsError:
                        self.existing_event(blob, event)
            finally:
                lease.release()
        try:
            self.queue.send_message(name)
        except Exception:
            # The minute repair scan is the durable queue's backstop.
            pass

    def existing_event(self, blob: Any, event: dict) -> bool:
        """Check immutable content before charging any daily budget."""
        from azure.core.exceptions import ResourceNotFoundError

        try:
            stored = json.loads(blob.download_blob().readall())
        except ResourceNotFoundError:
            return False
        if stored['hash'] != event['hash']:
            raise Conflict('Report identifier already used') from None
        return True

    def rate_counter(self, rate_key: str) -> tuple[Any, Any]:
        """Create the UTC-day counter once and lease it across admission."""
        from azure.core.exceptions import HttpResponseError, ResourceExistsError

        timestamp = now()
        name = f'rate/{timestamp.date().isoformat()}/{rate_key}.json'
        blob = self.container.get_blob_client(name)
        try:
            blob.upload_blob(canonical({'count': 0, 'created_at': timestamp.isoformat()}),
                             overwrite=False)
        except ResourceExistsError:
            pass
        try:
            lease = blob.acquire_lease(lease_duration=60)
        except HttpResponseError as error:
            if error.status_code == 409:
                raise RateLimited(1) from None
            raise
        return blob, lease

    def process(self, name: str) -> None:
        """Advance one email under a finite lease without persisting ACS responses."""
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        if re.fullmatch(r'(reports|contacts)/[0-9a-f]{32}\.json', name) is None:
            return
        blob = self.container.get_blob_client(name)
        try:
            lease = blob.acquire_lease(lease_duration=60)
        except (HttpResponseError, ResourceNotFoundError):
            return
        try:
            event = json.loads(blob.download_blob(lease=lease).readall())
            if now() - datetime.fromisoformat(event['created_at']) >= RETENTION:
                blob.delete_blob(lease=lease)
                return
            if event['status'] in {'succeeded', 'failed'}:
                return
            due = event.get('next_attempt_at')
            if due and now() < datetime.fromisoformat(due):
                return
            try:
                self.advance(event, blob, lease)
            except Exception:
                # Never log HTTP bodies, tokens, contact addresses or exception messages.
                event['errors'] += 1
                if event['errors'] >= 12:
                    event['status'] = 'failed'
                delay = min(3600, 60 * 2 ** min(event['errors'], 6))
                event['next_attempt_at'] = (now() + timedelta(seconds=delay)).isoformat()
            blob.upload_blob(canonical(event), overwrite=True, lease=lease)
        finally:
            try:
                lease.release()
            except HttpResponseError:
                pass

    def advance(self, event: dict, blob: Any, lease: Any) -> None:
        """Submit with a stable operation ID, then confirm via later status polls."""
        token = self.credential.get_token('https://communication.azure.com/.default').token
        # Credential acquisition can outlive a lease. Revalidate ownership before ACS.
        lease.renew()
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        operation_id = event['operation_id']
        suffix = f'?api-version={API_VERSION}'
        if event['status'] != 'pending':
            response = self.transport.get(
                f'{self.endpoint}/emails/operations/{operation_id}{suffix}',
                headers=headers, timeout=(5, 15), allow_redirects=False)
            if response.status_code != 404:
                response.raise_for_status()
                if response.status_code != 200:
                    raise RuntimeError('Unexpected operation response')
                status = response.json().get('status')
                if status == 'Succeeded':
                    event['status'] = 'succeeded'
                elif status in {'Failed', 'Canceled'}:
                    event['status'] = 'failed'
                elif status not in {'NotStarted', 'Running'}:
                    raise RuntimeError('Unexpected operation status')
                event['polls'] += 1
                if event['polls'] >= 1440 and event['status'] != 'succeeded':
                    event['status'] = 'failed'
                event['next_attempt_at'] = (now() + timedelta(minutes=1)).isoformat()
                return
        if event['submissions'] >= 12:
            event['status'] = 'failed'
            return
        # Persist the uncertain-submission state BEFORE the network write.
        event['status'] = 'submitting'
        event['submissions'] += 1
        blob.upload_blob(canonical(event), overwrite=True, lease=lease)
        headers['Operation-Id'] = operation_id
        response = self.transport.post(
            f'{self.endpoint}/emails:send{suffix}', headers=headers,
            json=build_email(event, self.sender, self.support), timeout=(5, 15),
            allow_redirects=False)
        if response.status_code != 409:
            response.raise_for_status()
        if response.status_code not in {202, 409}:
            raise RuntimeError('Unexpected send response')
        event['status'] = 'submitted'
        event['next_attempt_at'] = (now() + timedelta(minutes=1)).isoformat()

    def repair(self) -> None:
        """Scan durable events every minute, repairing lost queue notifications."""
        for prefix in ('reports/', 'contacts/'):
            for item in self.container.list_blobs(name_starts_with=prefix):
                self.process(item.name)
        self.expire_counters()

    def expire_counters(self) -> None:
        """Remove hashed caller counters after 30 days without sending emails."""
        from azure.core.exceptions import HttpResponseError

        for item in self.container.list_blobs(name_starts_with='rate/'):
            blob = self.container.get_blob_client(item.name)
            try:
                lease = blob.acquire_lease(lease_duration=60)
            except HttpResponseError:
                continue
            try:
                counter = json.loads(blob.download_blob(lease=lease).readall())
                if now() - datetime.fromisoformat(counter['created_at']) >= RETENTION:
                    blob.delete_blob(lease=lease)
            finally:
                try:
                    lease.release()
                except HttpResponseError:
                    pass


def configured_service() -> DiagnosticsService:
    """Create managed-identity clients without account keys or connection strings."""
    import requests
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    from azure.storage.queue import QueueClient

    credential = DefaultAzureCredential()
    storage_url = os.environ['REPORT_STORAGE_URL']
    queue_url = os.environ['REPORT_QUEUE__queueServiceUri']
    endpoint = os.environ['ACS_ENDPOINT']
    for url in (storage_url, queue_url, endpoint):
        require(re.fullmatch(r'https://[A-Za-z0-9.-]+(?::443)?/?', url) is not None)
    container = BlobServiceClient(
        storage_url, credential=credential, retry_total=0,
        connection_timeout=5, read_timeout=10,
    ).get_container_client(os.environ.get('REPORT_CONTAINER', 'diagnostics'))
    queue = QueueClient(queue_url, os.environ.get('DIAGNOSTICS_QUEUE_NAME', 'diagnostics'),
                        credential=credential, retry_total=0,
                        connection_timeout=5, read_timeout=10)
    return DiagnosticsService(container, queue, credential, endpoint,
                              os.environ['ACS_SENDER'], os.environ['SUPPORT_EMAIL'], requests)
