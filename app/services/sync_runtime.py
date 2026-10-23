"""File-backed live sync status and OS-owned background transaction coordination."""
from __future__ import annotations

import errno
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from flask import Response, current_app, has_request_context, request
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models import Job, db

logger = logging.getLogger(__name__)
EVENT_LIMIT = 200


def timestamp() -> str:
    """Return an unambiguous UTC timestamp for live state."""
    return datetime.now(timezone.utc).isoformat()


def state_directory() -> Path:
    """Keep coordination files beside this database, never a different installation."""
    configured = current_app.config.get('SYNC_STATE_DIR')
    database = db.engine.url.database
    if configured:
        directory = Path(configured)
    elif database and database != ':memory:':
        directory = Path(database).resolve().with_suffix('.sync-state')
    else:
        directory = Path(current_app.instance_path) / 'sync-state'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _try_lock(handle: BinaryIO) -> bool:
    """Acquire a nonblocking OS lock; only contention is a normal false result."""
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise


def _unlock(handle: BinaryIO) -> None:
    """Release ownership explicitly; closing the handle also releases it on crash."""
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_handle(path: Path) -> BinaryIO:
    """Open a persistent sidecar with a lockable first byte."""
    handle = path.open('a+b')
    if handle.tell() == 0:
        handle.write(b'\0')
        handle.flush()
    return handle


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    """Serialize progress/heartbeat publishers without acquiring SQLite locks."""
    with _lock_handle(path) as handle:
        deadline = time.monotonic() + 10
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Sync status publisher lock timed out: {path}')
            time.sleep(0.01)
        try:
            yield
        finally:
            _unlock(handle)


def _read(path: Path) -> dict | None:
    """Missing files are normal before execution; corrupt/unreadable files are errors."""
    try:
        for attempt in range(10):
            try:
                contents = path.read_text(encoding='utf-8')
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.01)
        data = json.loads(contents)
        if not isinstance(data, dict):
            raise ValueError('Sync status must be a JSON object.')
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.exception('Could not read sync status %s', path)
        raise


def _replace(path: Path, data: dict) -> None:
    """Publish a complete snapshot, retrying transient Windows reader sharing conflicts."""
    temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps(data), encoding='utf-8')
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.01)
    except OSError:
        logger.exception('Could not publish sync status %s', path)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def identity(job: Job) -> dict:
    """Fence a live publisher to one database row generation and worker attempt."""
    return {'id': job.id, 'created_at': job.created_at.isoformat(),
            'attempt': job.attempts, 'worker': job.claimed_by}


def start(job: Job) -> dict:
    """Initialize the current attempt while preserving bounded earlier-attempt events."""
    owner = identity(job)
    path = state_directory() / f'job-{job.id}.json'
    with _state_lock(path.with_suffix('.lock')):
        previous = _read(path)
        same_run = previous and previous.get('created_at') == owner['created_at']
        if same_run and previous['attempt'] > owner['attempt']:
            raise RuntimeError('An obsolete worker cannot restart this sync attempt.')
        state = {**owner, 'heartbeat_at': timestamp(), 'progress': {},
                 'progress_at': None, 'events': previous.get('events', []) if same_run else [],
                 'waiting_for_write': False, 'writing': False}
        _replace(path, state)
    return owner


def read(job: Job) -> dict | None:
    """Read only live state belonging to this exact persisted run and attempt."""
    state = _read(state_directory() / f'job-{job.id}.json')
    if state and all(state.get(key) == value for key, value in identity(job).items()):
        return state
    return None


def update(owner: dict, *, progress: dict | None = None,
           event_type: str = 'progress', **fields: Any) -> None:
    """Merge one fenced update, so heartbeat and progress never clobber each other."""
    path = state_directory() / f"job-{owner['id']}.json"
    with _state_lock(path.with_suffix('.lock')):
        state = _read(path)
        if not state or any(state.get(key) != value for key, value in owner.items()):
            raise RuntimeError('Sync status belongs to a different worker attempt.')
        state.update(fields)
        if progress is not None:
            now = timestamp()
            state['progress'] = progress
            state['progress_at'] = now
            events = state['events']
            events.append({'id': events[-1]['id'] + 1 if events else 1,
                           'attempt': owner['attempt'], 'type': event_type,
                           'data': json.dumps(progress), 'created_at': now})
            state['events'] = events[-EVENT_LIMIT:]
        _replace(path, state)


def worker_heartbeat(*, ready: bool = False) -> None:
    """Publish process liveness without competing with a domain database writer."""
    _replace(state_directory() / 'worker.json',
             {'pid': os.getpid(), 'heartbeat_at': timestamp(),
              'boot_id': os.environ.get('SALESBUDDY_BOOT_ID'), 'ready': ready})


def worker_state() -> dict | None:
    """Read the independent worker heartbeat."""
    return _read(state_directory() / 'worker.json')


def cleanup() -> None:
    """Remove old terminal snapshots, never active runs or ownership lock files."""
    directory = state_directory()
    now = time.time()
    for path in directory.glob('save-*.json'):
        if now - path.stat().st_mtime > 86400:
            path.unlink(missing_ok=True)
    for path in directory.glob('job-*.json'):
        if now - path.stat().st_mtime <= 30 * 86400:
            continue
        job = db.session.get(Job, int(path.stem.removeprefix('job-')))
        if job is None or job.status in (Job.STATUS_DONE, Job.STATUS_FAILED):
            path.unlink(missing_ok=True)


def write_owner() -> dict | None:
    """Return a named writer only while an OS lock actually proves ownership."""
    directory = state_directory()
    path = directory / 'writer.lock'
    if not path.exists():
        return None
    with _lock_handle(path) as handle:
        if _try_lock(handle):
            _unlock(handle)
            return None
        return _read(directory / 'writer.json')


def _save_id() -> str | None:
    """Use a validated opaque token to correlate native and fetch save requests."""
    if not has_request_context() or request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    value = request.headers.get('X-SB-Save-Id') or request.form.get('_sb_save_id')
    try:
        return uuid.UUID(value).hex if value else None
    except (ValueError, AttributeError):
        return None


def save_status(save_id: str) -> dict | None:
    """Read a save's proven contention state; do not guess from an unrelated sync."""
    token = uuid.UUID(save_id).hex
    return _read(state_directory() / f'save-{token}.json')


def _record_save_wait() -> None:
    """Record only a real interactive DML attempt made while a sync owns the gate."""
    token = _save_id()
    owner = write_owner() if token else None
    if token and owner:
        request.environ['sb_save_wait_id'] = token
        request.environ['sb_save_statement_waiting'] = True
        _replace(state_directory() / f'save-{token}.json',
                 {'pending': True, 'waiting': True, 'writer': owner, 'started_at': timestamp()})


def finish_save(response: Response) -> Response:
    """A response ends the pending request; never equate HTTP failure with a saved edit."""
    token = request.environ.get('sb_save_wait_id')
    if token:
        _replace(state_directory() / f'save-{token}.json',
                 {'pending': False, 'response_status': response.status_code,
                  'finished_at': timestamp()})
    return response


def _acquire_writer(owner: dict, name: str) -> BinaryIO:
    """Wait outside SQLite for exclusive background transaction ownership."""
    handle = _lock_handle(state_directory() / 'writer.lock')
    acquired = False
    try:
        while not _try_lock(handle):
            update(owner, waiting_for_write=True, writing=False)
            time.sleep(0.1)
        acquired = True
        _replace(state_directory() / 'writer.json',
                 {**owner, 'name': name, 'since': timestamp()})
        update(owner, waiting_for_write=False, writing=True)
        return handle
    except Exception:
        if acquired:
            _unlock(handle)
        handle.close()
        raise


def install_write_gate(engine: Engine) -> None:
    """Gate each sync's actual SQLite write transaction through connection check-in."""
    if engine.dialect.name != 'sqlite':
        return
    from app.services.job_queue import CURRENT_SYNC_OWNER

    @event.listens_for(engine, 'before_cursor_execute')
    def before_write(connection, cursor, statement, parameters, context, executemany):
        """Acquire just before DML, not while fetching or preparing source data."""
        owner = CURRENT_SYNC_OWNER.get()
        if not (context.isinsert or context.isupdate or context.isdelete or
                statement.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ', 'REPLACE '))):
            return
        if owner is None:
            _record_save_wait()
            return
        if 'sync_write_handle' in connection.info:
            return
        from app.services.sync_jobs import OPERATIONS
        name = OPERATIONS[owner['operation']].name
        handle = _acquire_writer(owner['identity'], name)
        try:
            row = connection.execute(select(Job.attempts, Job.claimed_by, Job.status).where(
                Job.id == owner['identity']['id'])).one()
            if (row.attempts != owner['identity']['attempt'] or
                    row.claimed_by != owner['identity']['worker'] or row.status != 'running'):
                raise RuntimeError('An obsolete sync attempt cannot write database results.')
        except Exception:
            _unlock(handle)
            handle.close()
            update(owner['identity'], writing=False)
            raise
        connection.info['sync_write_handle'] = handle
        connection.info['sync_write_owner'] = owner['identity']

    @event.listens_for(engine, 'after_cursor_execute')
    def after_write(connection, cursor, statement, parameters, context, executemany):
        """Stop attributing a wait to a sync once the user's SQL statement succeeds."""
        if has_request_context() and request.environ.pop('sb_save_statement_waiting', False):
            token = request.environ['sb_save_wait_id']
            _replace(state_directory() / f'save-{token}.json',
                     {'pending': True, 'waiting': False})

    @event.listens_for(engine.pool, 'checkin')
    def after_transaction(dbapi_connection, record):
        """Release only after commit/rollback and connection reset have finished."""
        handle = record.info.pop('sync_write_handle', None)
        owner = record.info.pop('sync_write_owner', None)
        if handle is not None:
            try:
                _unlock(handle)
            finally:
                handle.close()
            update(owner, writing=False, waiting_for_write=False)
