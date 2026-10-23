"""Real SQLite/file-lock acceptance tests for concurrent detached syncs."""
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from app.models import Customer, Job, db
from app.services import job_queue, sync_jobs, sync_runtime


@pytest.fixture(autouse=True)
def isolated_live_files(app, tmp_path, monkeypatch):
    """Keep process heartbeats and crash fixtures independent between tests."""
    monkeypatch.setitem(app.config, 'SYNC_STATE_DIR', str(tmp_path))


def wait_until(predicate, timeout: float = 10) -> None:
    """Wait for observable state, not arbitrary sleeps or live external services."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Expected concurrent state did not become observable.')


def test_status_read_retries_transient_windows_access_conflicts(client, monkeypatch):
    """A snapshot replacement race must not turn a healthy status poll into a 503."""
    sync_runtime.worker_heartbeat()
    original = Path.read_text
    attempts = []

    def read_text(path, *args, **kwargs):
        if path.name == 'worker.json':
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError('Snapshot is being replaced')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    response = client.get('/api/jobs')
    assert response.status_code == 200
    assert response.json['worker']['alive'] is True
    assert len(attempts) == 3


def test_status_read_reports_persistent_access_failure(client, monkeypatch, caplog):
    """Retries are bounded and never disguise a persistently unreadable heartbeat."""
    original = Path.read_text
    attempts = []

    def read_text(path, *args, **kwargs):
        if path.name == 'worker.json':
            attempts.append(path)
            raise PermissionError('Access remains denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    response = client.get('/api/jobs')
    assert response.status_code == 503
    assert response.json['success'] is False
    assert len(attempts) == 10
    assert 'Could not read sync status' in caplog.text


def test_conflicting_requests_wait_with_named_blocker(client):
    """The entire group is reserved while fetching, before any write starts."""
    customer = Customer(name='Meeting Group Test Customer', tpid=999003)
    db.session.add(customer)
    db.session.commit()
    try:
        first = sync_jobs.admit_sync('meetings', {'date': '2026-10-14'})
        second = sync_jobs.admit_sync('meeting-aura', {'anchor': '2026-10-14'})
        assert job_queue.claim_next('one').id == first.id
        assert job_queue.claim_next('two') is None
        snapshot = client.get(f'/api/jobs/{second.id}').json['job']
        assert snapshot['state'] == 'waiting_for_group'
        assert snapshot['blocker'] == {'id': first.id, 'name': 'Meetings for a date'}
    finally:
        db.session.delete(customer)
        db.session.commit()


def test_independent_fetches_really_overlap_in_worker_loop(app, monkeypatch):
    """Two blocked source queries start without waiting for either to finish."""
    entered = {key: threading.Event() for key in ('favicons', 'copilot-actions')}
    release = threading.Event()
    stop = threading.Event()

    def run(operation, options):
        entered[operation].set()
        assert release.wait(10)
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    for key in entered:
        sync_jobs.admit_sync(key)
    loop = threading.Thread(target=job_queue.run_worker_loop, kwargs={
        'app': app, 'stop_event': stop, 'poll_interval': 0.02}, daemon=True)
    loop.start()
    try:
        assert all(event.wait(5) for event in entered.values())
    finally:
        release.set()
        stop.set()
        loop.join(10)

    def completed():
        db.session.expire_all()
        return Job.query.filter_by(status='done').count() == 2

    wait_until(completed)


def test_background_writers_take_turns_while_status_and_heartbeats_stay_live(
    app, client, monkeypatch,
):
    """A real SQLite writer cannot block file-backed progress or cause stale recovery."""
    first = sync_jobs.admit_sync('favicons')
    second = sync_jobs.admit_sync('copilot-actions')
    jobs = [job_queue.claim_next('one'), job_queue.claim_next('two')]
    assert [job.id for job in jobs] == [first.id, second.id]
    writing = threading.Event()
    release = threading.Event()

    def run(operation, options):
        if operation == 'copilot-actions':
            assert writing.wait(5)
        sync_jobs.publish_progress({'progress': 50, 'message': 'External data ready.'})
        db.session.add(Customer(name=operation, tpid=123 if operation == 'favicons' else 456))
        db.session.flush()
        if operation == 'favicons':
            writing.set()
            assert release.wait(10)
        db.session.commit()
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)

    def execute(job_id):
        with app.app_context():
            job_queue._execute(db.session.get(Job, job_id), app=app, with_heartbeat=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, job.id) for job in jobs]
        try:
            assert writing.wait(5)
            wait_until(lambda: (sync_runtime.read(second) or {}).get('waiting_for_write'))
            assert sync_runtime.write_owner()['id'] == first.id
            job_queue.heartbeat(second.id)
            sync_runtime.worker_heartbeat()
            observed = client.get(f'/api/jobs/{second.id}').json['job']
            assert observed['state'] == 'waiting_for_write'
            assert observed['blocker']['id'] == first.id
            assert observed['progress']['message'] == 'External data ready.'
            assert sync_jobs.worker_health()['alive']
        finally:
            release.set()
        for future in futures:
            future.result(timeout=10)
    db.session.expire_all()
    assert Job.query.filter_by(status='done').count() == 2
    assert sync_runtime.write_owner() is None
    assert Customer.query.filter(Customer.tpid.in_([123, 456])).count() == 2


def test_milestones_and_revenue_fetch_together_and_serialize_sqlite_writes(
    app, client, monkeypatch,
):
    """The exact onboarding pair overlaps source work but never SQLite transactions."""
    customer = Customer(name='Concurrent Onboarding Test Customer', tpid=999004)
    db.session.add(customer)
    db.session.commit()
    customer_id = customer.id
    entered = {operation: threading.Event() for operation in ('milestones', 'revenue')}
    writing = threading.Event()
    release = threading.Event()
    stop = threading.Event()
    write_order = []

    def run(operation, options):
        """Stand in for remote reads, leaving claims, writes and outcomes real."""
        entered[operation].set()
        assert all(event.wait(5) for event in entered.values())
        if operation == 'revenue':
            assert writing.wait(5)
        sync_jobs.publish_progress({'message': 'Source data fetched.', 'progress': 50})
        row = db.session.get(Customer, customer_id)
        row.nickname = operation
        db.session.flush()
        write_order.append(operation)
        if operation == 'milestones':
            writing.set()
            assert release.wait(10)
        db.session.commit()
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    milestone_job = sync_jobs.admit_sync('milestones')
    revenue_job = sync_jobs.admit_sync('revenue')
    loop = threading.Thread(target=job_queue.run_worker_loop, kwargs={
        'app': app, 'stop_event': stop, 'poll_interval': 0.02}, daemon=True)
    loop.start()
    try:
        assert all(event.wait(5) for event in entered.values())
        assert writing.wait(5)
        db.session.refresh(revenue_job)
        wait_until(lambda: (sync_runtime.read(revenue_job) or {}).get('waiting_for_write'))
        snapshot = client.get(f'/api/jobs/{revenue_job.id}').json['job']
        assert snapshot['state'] == 'waiting_for_write'
        assert snapshot['blocker']['id'] == milestone_job.id
        assert snapshot['blocker']['name'] == 'Milestones'
        assert snapshot['progress']['message'] == 'Source data fetched.'
        assert write_order == ['milestones']
    finally:
        release.set()
        stop.set()
        loop.join(10)

    def completed():
        """Observe both terminal worker outcomes before checking durable data."""
        db.session.expire_all()
        return all(db.session.get(Job, job.id).status == 'done'
                   for job in (milestone_job, revenue_job))

    try:
        wait_until(completed)
        assert write_order == ['milestones', 'revenue']
        assert db.session.get(Customer, customer_id).nickname == 'revenue'
        assert sync_runtime.write_owner() is None
    finally:
        db.session.delete(db.session.get(Customer, customer_id))
        db.session.commit()


def test_fresh_file_heartbeat_prevents_false_stale_recovery():
    """SQLite's old heartbeat is not evidence of death while live status is fresh."""
    job = sync_jobs.admit_sync('favicons')
    job_queue.claim_next('healthy')
    owner = sync_runtime.start(job)
    job.heartbeat_at = job_queue._now() - timedelta(seconds=Job.HEARTBEAT_STALE_SECONDS + 10)
    db.session.commit()
    sync_runtime.update(owner, heartbeat_at=sync_runtime.timestamp())
    assert job_queue.reclaim_stale() == 0
    assert sync_jobs.job_snapshot(job)['state'] == 'running'


def test_old_attempt_cannot_publish_after_replacement():
    """Attempt fencing prevents a late callback from corrupting current live progress."""
    job = sync_jobs.admit_sync('favicons')
    job_queue.claim_next('old')
    old = sync_runtime.start(job)
    job.attempts += 1
    job.claimed_by = 'replacement'
    db.session.commit()
    current = sync_runtime.start(job)
    with pytest.raises(RuntimeError, match='different worker attempt'):
        sync_runtime.update(old, progress={'message': 'Obsolete'})
    sync_runtime.update(current, progress={'message': 'Current'})
    assert sync_runtime.read(job)['progress']['message'] == 'Current'


def test_writer_lock_is_released_by_process_exit():
    """A dead writer cannot strand the gate, even when its owner JSON remains."""
    path = sync_runtime.state_directory() / 'writer.lock'
    script = """
import os, sys
handle = open(sys.argv[1], 'a+b')
handle.write(b'0')
handle.flush()
handle.seek(0)
if os.name == 'nt':
    import msvcrt
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
os._exit(0)
"""
    subprocess.run([sys.executable, '-c', script, str(path)], check=True, timeout=10)
    assert sync_runtime.write_owner() is None


def test_corrupt_live_status_is_explicitly_unavailable(client):
    """A broken file cannot silently turn a running sync into a healthy idle state."""
    job = sync_jobs.admit_sync('favicons')
    job_queue.claim_next('worker')
    sync_runtime.start(job)
    path = sync_runtime.state_directory() / f'job-{job.id}.json'
    original = path.read_text()
    try:
        path.write_text('{"incomplete":')
        response = client.get(f'/api/jobs/{job.id}')
        assert response.status_code == 503
        assert not response.json['success']
    finally:
        path.write_text(original)


def test_idempotency_key_cannot_be_reused_for_different_options(client):
    """Active-request dedupe cannot override the identity of a different request key."""
    headers = {'Idempotency-Key': 'one-request'}
    assert client.post('/api/jobs/start/favicons', json={'missing_only': True},
                       headers=headers).status_code == 202
    assert client.post('/api/jobs/start/favicons', json={'missing_only': False},
                       headers=headers).status_code == 409


def test_queued_options_do_not_hide_the_running_operation():
    """The console retains current progress when another request for the same sync queues."""
    running = sync_jobs.admit_sync('favicons', {'missing_only': True})
    job_queue.claim_next('worker')
    queued = sync_jobs.admit_sync('favicons', {'missing_only': False})
    row = next(row for row in sync_jobs.operation_snapshots()['operations']
               if row['operation'] == 'favicons')
    assert row['latest']['id'] == running.id
    assert row['queued'][0]['id'] == queued.id
    assert row['queued'][0]['blocker']['id'] == running.id


def test_group_dependency_can_execute_without_deadlocking_parent(app, monkeypatch):
    """Preparation can refresh a prerequisite within its reserved logical group."""
    seen = []
    db.session.add(Customer(name='Dependency', tpid=987))
    db.session.commit()

    def run(operation, options):
        seen.append(operation)
        if operation == 'meeting-preparation':
            sync_jobs.run_dependency('milestones')
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)
    sync_jobs.admit_sync('meeting-preparation')
    job_queue.process_one('worker', app=app)
    assert seen[:2] == ['meeting-preparation', 'milestones']
    assert Job.query.filter_by(job_type='sync:meeting-preparation').one().status == 'done'


@pytest.mark.parametrize('native_form', [True, False])
def test_interactive_save_wait_reports_actual_owner_until_request_finishes(
    app, client, monkeypatch, native_form,
):
    """Native and fetch saves expose real SQLite contention without joining a priority queue."""
    from app.models import Seller
    job = sync_jobs.admit_sync('favicons')
    job_queue.claim_next('writer')
    entered = threading.Event()
    release = threading.Event()
    save_id = str(uuid.uuid4())

    def run(*args):
        db.session.add(Customer(name='Background transaction', tpid=777))
        db.session.flush()
        entered.set()
        assert release.wait(15)
        db.session.commit()
        return {'success': True}

    monkeypatch.setattr(sync_jobs, '_execute_operation', run)

    def writer():
        with app.app_context():
            job_queue._execute(db.session.get(Job, job.id), app=app)

    def save():
        data = {'name': 'Waiting seller'}
        if native_form:
            data['_sb_save_id'] = save_id
        return app.test_client().post('/seller/new', data=data, headers={} if native_form else {
            'X-SB-Save-Id': save_id})

    with ThreadPoolExecutor(max_workers=2) as pool:
        background = pool.submit(writer)
        assert entered.wait(5)
        request = pool.submit(save)
        try:
            wait_until(lambda: (sync_runtime.save_status(save_id) or {}).get('pending'))
            start = time.monotonic()
            status = client.get(f'/api/jobs/write-status?save_id={save_id}').json
            assert time.monotonic() - start < 1
            assert status['save']['pending']
            assert status['save']['waiting']
            assert status['writer']['id'] == job.id
            assert not request.done()
        finally:
            release.set()
        background.result(timeout=10)
        assert request.result(timeout=10).status_code == 302
    db.session.expire_all()
    assert Seller.query.filter_by(name='Waiting seller').count() == 1
    assert sync_runtime.save_status(save_id)['pending'] is False
    assert sync_runtime.write_owner() is None


def test_obsolete_attempt_cannot_write_after_durable_reclaim():
    """A still-alive old thread is fenced even before the replacement publishes its file."""
    job = sync_jobs.admit_sync('favicons')
    job_queue.claim_next('old')
    owner = sync_runtime.start(job)
    job_queue.fail(job.id, 'Worker was interrupted', retry=True)
    token = job_queue.CURRENT_SYNC_OWNER.set({'identity': owner, 'operation': 'favicons'})
    try:
        db.session.add(Customer(name='Obsolete write', tpid=998))
        with pytest.raises(RuntimeError, match='obsolete sync attempt'):
            db.session.flush()
        db.session.rollback()
    finally:
        job_queue.CURRENT_SYNC_OWNER.reset(token)
    assert Customer.query.filter_by(tpid=998).first() is None
    assert sync_runtime.write_owner() is None
