const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {startupDecision, MAX_STARTUP_MS, trustedStatusEvent, diagnosticReport, recoveryPoint}
  = require('../../electron/upgrade-safety');
const {verifyRecoveryIntegrity} = require('../../electron/upgrade-safety');
const {createHash} = require('node:crypto');
const {statusScreenUrl} = require('../../electron/status-screen');
const {bundledShellMatchesSource} = require('../../electron/upgrade-safety');

test('packaged source parity does not depend on stripped electron-builder metadata', () => {
  const files = ['main.js', 'preload.js', 'status.html', 'status.css', 'input-focus.ps1'];
  const content = Object.fromEntries(files.map(file => [file, `actual ${file}`]));
  const bundled = path.resolve('fixture', 'resources', 'app.asar');
  const repo = path.resolve('fixture', 'install');
  let mismatch = null;
  const fixtureFs = {
    readdirSync: root => {
      assert.equal(root, bundled);
      return [...files, 'package.json'];
    },
    readFileSync: file => {
      const name = path.basename(file);
      assert.notEqual(name, 'package.json', 'production package.json has no build metadata');
      return file.startsWith(bundled) && mismatch === name ? 'older source' : content[name];
    },
  };
  assert.equal(bundledShellMatchesSource(fixtureFs, bundled, repo), true);
  for (const file of files) {
    mismatch = file;
    assert.equal(bundledShellMatchesSource(fixtureFs, bundled, repo), false, file);
  }
  fixtureFs.readdirSync = () => ['package.json'];
  assert.equal(bundledShellMatchesSource(fixtureFs, bundled, repo), false);
});

function fixture() {
  const now = Date.now();
  const identity = {bootId: '12345678-1234-4234-8234-123456789abc',
    commit: 'a'.repeat(40), started: now - 1000, exited: false};
  const status = {schema_version: 1, state: 'ready', phase: 'ready',
    boot_id: identity.bootId, expected_commit: identity.commit,
    started_at: new Date(identity.started).toISOString(), updated_at: new Date(now).toISOString(),
    children: [{name: 'web', pid: 101, ready: true}, {name: 'worker', pid: 102, ready: true}]};
  const health = {status: 'healthy', database: 'connected',
    worker_heartbeat_at: new Date(now).toISOString(),
    boot_id: identity.bootId, commit_full: identity.commit,
    web_pid: 101, worker: 'alive', worker_boot_id: identity.bootId, worker_pid: 102,
    worker_ready: true};
  return {now, identity, status, health};
}

test('readiness independently proves current supervisor, revision, web and fresh worker', () => {
  const f = fixture();
  assert.equal(startupDecision(f.status, f.health, f.identity, f.now), 'ready');
  for (const patch of [{}, {boot_id: 'old'}, {commit_full: 'a'.repeat(7)},
    {worker: 'dead'}, {worker_ready: false}, {worker_ready: undefined},
    {worker_boot_id: 'old'}, {worker_pid: 999}, {web_pid: 999},
    {status: 'starting'}, {database: 'disconnected'},
    {worker_heartbeat_at: new Date(f.now - 91000).toISOString()},
    {worker_heartbeat_at: new Date(f.now + 1).toISOString()},
    {worker_heartbeat_at: undefined}]) {
    const health = Object.keys(patch).length ? {...f.health, ...patch} : {status: 'ok'};
    assert.equal(startupDecision(f.status, health, f.identity, f.now), 'wait');
  }
  for (const patch of [{boot_id: 'old'}, {expected_commit: 'b'.repeat(40)},
    {started_at: new Date(f.now - 60000).toISOString()},
    {updated_at: new Date(f.now - 31000).toISOString()}, {state: 'starting'},
    {phase: 'activating', state: 'starting', activation_allowed: true},
    {phase: 'activating'},
    {children: [{...f.status.children[0], ready: 'true'}, f.status.children[1]]},
    {children: [null, {name: 'worker', pid: 102, ready: true}]},
    {children: [...f.status.children, f.status.children[0]]},
    {children: [{name: 'worker', pid: 102, ready: true}]}]) {
    assert.equal(startupDecision({...f.status, ...patch}, f.health, f.identity, f.now), 'wait');
  }
});

test('terminal startup state, supervisor exit and absolute deadline cannot poll forever', () => {
  const f = fixture();
  assert.equal(startupDecision({...f.status, state: 'failed'}, null, f.identity, f.now), 'failed');
  assert.equal(startupDecision(null, null, {...f.identity, exited: true}, f.now), 'exited');
  assert.equal(startupDecision(null, null, f.identity,
    f.identity.started + MAX_STARTUP_MS), 'deadline');
});

test('reconnection has its own bounded deadline without changing the original boot identity', () => {
  const f = fixture();
  const now = f.identity.started + MAX_STARTUP_MS + 1000;
  const identity = {...f.identity, recoveryStarted: now - 1000};
  const status = {...f.status, updated_at: new Date(now).toISOString()};
  const health = {...f.health, worker_heartbeat_at: new Date(now).toISOString()};
  assert.equal(startupDecision(status, health, f.identity, now), 'deadline');
  assert.equal(startupDecision(status, health, identity, now), 'ready');
  assert.equal(startupDecision(status, health, identity,
    identity.recoveryStarted + MAX_STARTUP_MS), 'deadline');
});

test('status IPC accepts only the current managed document main frame', () => {
  const url = 'data:text/html;charset=utf-8,bundled';
  const mainFrame = {url};
  const webContents = {mainFrame, getURL: () => url};
  const win = {_sbStatusUrl: url, webContents, isDestroyed: () => false};
  const event = {sender: webContents, senderFrame: mainFrame};
  assert.equal(trustedStatusEvent(event, win), true);
  assert.equal(trustedStatusEvent({...event, senderFrame: {url}}, win), false);
  assert.equal(trustedStatusEvent({...event, sender: {}}, win), false);
  for (const other of ['http://localhost:5151/', 'data:text/html,forged', 'https://example.com']) {
    mainFrame.url = other;
    assert.equal(trustedStatusEvent(event, win), false);
  }
  mainFrame.url = url;
  assert.equal(trustedStatusEvent(event, {...win, _sbStatusUrl: null}), false);
});

test('private reports reconstruct an allowlist rather than copying manifests or exceptions', () => {
  const secret = 'PRIVATE_CUSTOMER_987';
  const report = diagnosticReport({phase: 'failed', error: {
    type: secret, category: secret, message: secret, row_id: secret,
    frames: [{file: 'C:\\Users\\alex\\private.py', line: 1, function: secret},
      {file: 'upgrade.py', line: 7, function: secret}],
    violation_count: 4, parent_tables: ['notes', secret], exit_code: 1},
    children: [{name: 'worker', attempts: 3, ready: false, records: secret}],
    logs: secret}, {phase: 'failed', repo: secret, database: secret, attempt_id: secret,
    recovery_ready: true, failed_phase: 'rehearsing', notes: secret}, '1.2.3', 'win32');
  assert.ok(!JSON.stringify(report).includes(secret));
  assert.deepEqual(report.startup.error.frames, [{file: 'upgrade.py', line: 7}]);
  assert.deepEqual(report.startup.error.parent_tables, ['notes', 'other']);
  assert.equal(report.startup.error.violation_count, 4);
  assert.equal(report.upgrade.recovery_ready, true);
  assert.equal(report.upgrade.failed_phase, 'rehearsing');
});

test('reports retain the failing release and original update cause after a restore failure', () => {
  const report = diagnosticReport(null, {
    phase: 'failed', failed_phase: 'restoring',
    previous_commit: 'a'.repeat(40), target_commit: 'b'.repeat(40),
    original_failure: {phase: 'failed', failed_phase: 'migrating',
      error: {type: 'RehearsalError', category: 'migration_foreign_keys',
        message: 'PRIVATE_RECORD', frames: [{file: 'upgrade_rehearsal.py', line: 43}]}},
  }, '1.2.3', 'win32');
  assert.equal(report.previous_commit, 'a'.repeat(40));
  assert.equal(report.target_commit, 'b'.repeat(40));
  assert.equal(report.original_upgrade.failed_phase, 'migrating');
  assert.deepEqual(report.original_upgrade.error.frames, [{file: 'upgrade_rehearsal.py', line: 43}]);
  assert.ok(!JSON.stringify(report).includes('PRIVATE_RECORD'));
});

test('invalid or mismatched recovery points never become available', () => {
  const repo = path.resolve('installed');
  const state = {attempt_id: 'a'.repeat(32), repo, database: path.resolve('data', 'app.db'),
    recovery_ready: true, changes_started: true, previous_commit: 'b'.repeat(40)};
  const filesystem = {readFileSync: () => JSON.stringify({...state, attempt_id: 'c'.repeat(32)}),
    existsSync: () => true};
  assert.equal(recoveryPoint(filesystem, repo, state), null);
  assert.equal(recoveryPoint(filesystem, repo, {...state, attempt_id: '..'}), null);
  assert.equal(recoveryPoint(filesystem, repo, {...state, repo: path.resolve('other')}), null);
  assert.equal(recoveryPoint(filesystem, repo, {...state, phase: 'ready'}), null);
  assert.equal(recoveryPoint(filesystem, repo, {...state, phase: 'restored',
    notification_pending: false}), null);
});

test('recovery launch requires matching SHA256 of both frozen Python entry points', () => {
  const runner = Buffer.from('frozen recovery entry point');
  const helper = Buffer.from('frozen database path helper');
  const frozen = {runner: 'recover.py', backupHelper: 'db_paths.py'};
  const state = {runner_sha256: createHash('sha256').update(runner).digest('hex'),
    backup_helper_sha256: createHash('sha256').update(helper).digest('hex')};
  const filesystem = {readFileSync: file => file === 'recover.py' ? runner : helper};
  assert.equal(verifyRecoveryIntegrity(filesystem, frozen, state), true);
  for (const field of ['runner_sha256', 'backup_helper_sha256']) {
    assert.equal(verifyRecoveryIntegrity(filesystem, frozen,
      {...state, [field]: 'a'.repeat(64)}), false);
    assert.equal(verifyRecoveryIntegrity(filesystem, frozen,
      {...state, [field]: undefined}), false);
  }
  assert.equal(verifyRecoveryIntegrity({readFileSync: () => { throw new Error('missing'); }},
    frozen, state), false);
});

test('failure document stops progress and exposes accessible user-driven actions only when managed', () => {
  const url = statusScreenUrl('Sales Buddy needs attention', 'The workspace is not ready.',
    {failed: true, managed: true, recoveryAvailable: false});
  const html = decodeURIComponent(url);
  assert.match(html, /data-state="failed"/);
  assert.match(html, /id="failure-actions" aria-busy="false"/);
  assert.match(html, /data-status-action="recover" hidden/);
  const nodes = new Map();
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, {attributes: {}, setAttribute(k, v) {
      this.attributes[k] = v;
    }});
    return nodes.get(key);
  };
  const document = {documentElement: {dataset: {}}, body: {dataset: {state: 'failed'}},
    getElementById: node, querySelector: node};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../electron/status.js'), 'utf8'),
    {document, location: {search: ''}, URLSearchParams});
  assert.equal(node('[role="progressbar"]').hidden, true);
  assert.equal(node('.shell-status').attributes['aria-busy'], 'false');
  assert.match(node('status-detail').textContent, /recover automatically/);
  assert.match(node('status-footer').textContent, /not been confirmed/);
  const unmanaged = decodeURIComponent(statusScreenUrl('Unavailable', '', {failed: true}));
  assert.match(unmanaged, /id="failure-actions" hidden/);
});
