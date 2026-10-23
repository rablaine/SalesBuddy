// Pure readiness and report boundaries. Never pass durable records to a renderer.
const path = require('path');
const {createHash} = require('crypto');
const MAX_STARTUP_MS = 720000;
const FULL_COMMIT = /^[0-9a-f]{40,64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PHASES = new Set(['preflight', 'starting', 'activating', 'ready', 'failed', 'preparing', 'prepared',
  'code', 'dependencies', 'rehearsing', 'migrating', 'restoring', 'restored', 'shell', 'startup']);
const TABLES = new Set(['customers', 'notes', 'milestones', 'msx_tasks', 'engagements',
  '_old_call_logs', 'call_logs_backup', '_old_milestones', 'manager_initiative_items',
  'initiative_discussed_points']);
const CODE_FILES = new Set(['upgrade.py', 'migrations.py', 'supervisor.py', '__init__.py',
  'models.py', 'db_paths.py', 'main.js', 'upgrade-safety.js', 'startup_preflight.py',
  'migration_rehearsal.py', 'upgrade_rehearsal.py', 'database_startup.py', 'worker.py']);
const integer = (value, max = 1000000000) =>
  Number.isInteger(value) && value >= 0 && value <= max ? value : null;

function bundledShellMatchesSource(fs, bundledRoot, repo) {
  const files = fs.readdirSync(bundledRoot).filter(file => /\.(js|html|css|ps1)$/.test(file));
  return files.length > 0 && files.every(file =>
    fs.readFileSync(path.join(bundledRoot, file), 'utf8') ===
    fs.readFileSync(path.join(repo, 'electron', file), 'utf8'));
}

function startupDecision(status, health, identity, now = Date.now()) {
  if (identity.exited) return 'exited';
  if (now - (identity.recoveryStarted ?? identity.started) >= MAX_STARTUP_MS) return 'deadline';
  if (!status || status.schema_version !== 1 || status.boot_id !== identity.bootId) return 'wait';
  if (status.state === 'failed') return 'failed';
  const fresh = Date.parse(status.updated_at);
  const started = Date.parse(status.started_at);
  if (status.state !== 'ready' || status.phase !== 'ready' ||
      !FULL_COMMIT.test(identity.commit || '') ||
      status.expected_commit !== identity.commit || !Number.isFinite(started) ||
      started < identity.started - 2000 || !Number.isFinite(fresh) ||
      fresh < now - 30000 || fresh > now || fresh < started) return 'wait';
  const children = Array.isArray(status.children) ? status.children : [];
  const web = children.find(child => child?.name === 'web');
  const worker = children.find(child => child?.name === 'worker');
  if (children.length !== 2 || web?.ready !== true || worker?.ready !== true ||
      !integer(web.pid) || !integer(worker.pid)) return 'wait';
  const heartbeat = Date.parse(health?.worker_heartbeat_at);
  return health?.status === 'healthy' && health.database === 'connected' &&
    Number.isFinite(heartbeat) && heartbeat >= started && heartbeat >= now - 90000 &&
    heartbeat <= now && health.boot_id === identity.bootId && health.commit_full === identity.commit &&
    health.web_pid === web.pid && health.worker === 'alive' && health.worker_ready === true &&
    health.worker_boot_id === identity.bootId && health.worker_pid === worker.pid
    ? 'ready' : 'wait';
}

function trustedStatusEvent(event, win) {
  return !!win && !win.isDestroyed() && !!win._sbStatusUrl &&
    event.sender === win.webContents && !!event.senderFrame &&
    event.senderFrame === win.webContents.mainFrame &&
    event.senderFrame.url === win._sbStatusUrl &&
    win.webContents.getURL() === win._sbStatusUrl;
}

function safeError(error) {
  const categories = new Set(['operation_failed', 'dependency_missing', 'migration_foreign_keys',
    'database_locked', 'backup_failed', 'disk_space', 'permission', 'timeout', 'rehearsal_failed']);
  const types = new Set(['Error', 'RuntimeError', 'ValueError', 'TimeoutError', 'PermissionError',
    'OSError', 'IntegrityError', 'OperationalError', 'ModuleNotFoundError', 'CalledProcessError',
    'StartupFailure', 'RehearsalError']);
  const result = {
    type: types.has(error?.type) ? error.type : 'Error',
    category: categories.has(error?.category) ? error.category : 'operation_failed',
  };
  if (Number.isInteger(error?.exit_code) && Math.abs(error.exit_code) <= 65535) {
    result.exit_code = error.exit_code;
  }
  if (integer(error?.violation_count) !== null) result.violation_count = error.violation_count;
  result.parent_tables = [...new Set((Array.isArray(error?.parent_tables)
    ? error.parent_tables : []).slice(0, 32).map(table => TABLES.has(table) ? table : 'other'))];
  result.frames = (Array.isArray(error?.frames) ? error.frames : []).slice(-16)
    .filter(frame => typeof frame?.file === 'string' && CODE_FILES.has(frame.file) &&
      integer(frame.line, 100000) !== null)
    .map(frame => ({file: frame.file, line: frame.line}));
  return result;
}

function diagnosticReport(startup, upgrade, version, platform) {
  const report = {schema_version: 1,
    shell: {version: /^[\w.+-]{1,32}$/.test(version) ? version : 'unknown',
      platform: ['win32', 'darwin', 'linux'].includes(platform) ? platform : 'unknown'}};
  for (const field of ['previous_commit', 'target_commit']) {
    if (typeof upgrade?.[field] === 'string' && FULL_COMMIT.test(upgrade[field])) {
      report[field] = upgrade[field];
    }
  }
  for (const [key, source] of [['startup', startup], ['upgrade', upgrade],
    ['original_upgrade', upgrade?.original_failure]]) {
    if (!source || typeof source !== 'object') continue;
    report[key] = {phase: PHASES.has(source.phase) ? source.phase : 'unknown',
      error: safeError(source.error)};
    if (PHASES.has(source.failed_phase)) report[key].failed_phase = source.failed_phase;
    for (const flag of ['changes_started', 'recovery_ready', 'database_existed', 'shell_existed']) {
      if (typeof source[flag] === 'boolean') report[key][flag] = source[flag];
    }
    report[key].children = (Array.isArray(source.children) ? source.children : [])
      .filter(child => ['web', 'worker'].includes(child?.name)).slice(0, 2)
      .map(child => ({name: child.name, attempts: integer(child.attempts, 3),
        ready: child.ready === true}));
  }
  return report;
}

function recoveryPoint(fs, repo, state) {
  if (!state?.recovery_ready || state.changes_started !== true ||
      !/^[0-9a-f]{32}$/.test(state.attempt_id || '') ||
      state.phase === 'ready' || (state.phase === 'restored' && !state.notification_pending) ||
      typeof state.repo !== 'string' || path.resolve(state.repo) !== path.resolve(repo) ||
      typeof state.database !== 'string' || !path.isAbsolute(state.database)) return null;
  const point = path.join(path.dirname(state.database), 'upgrade-recovery', state.attempt_id);
  try {
    const text = fs.readFileSync(path.join(point, 'manifest.json'), 'utf8');
    if (text.length > 262144) return null;
    const saved = JSON.parse(text);
    if (saved.attempt_id !== state.attempt_id || saved.repo !== state.repo ||
        saved.database !== state.database || saved.previous_commit !== state.previous_commit ||
        saved.runner_sha256 !== state.runner_sha256 ||
        saved.backup_helper_sha256 !== state.backup_helper_sha256 ||
        !saved.recovery_ready) return null;
    const python = path.join(point, 'runtime', 'Scripts', 'python.exe');
    const runner = path.join(point, 'recover.py');
    return fs.existsSync(python) && fs.existsSync(runner) ? {point, python, runner,
      backupHelper: path.join(point, 'db_paths.py')} : null;
  } catch (_) { return null; }
}

function verifyRecoveryIntegrity(fs, frozen, state) {
  if (!frozen) return false;
  try {
    return [[frozen.runner, state?.runner_sha256],
      [frozen.backupHelper, state?.backup_helper_sha256]].every(([file, digest]) =>
      /^[0-9a-f]{64}$/.test(digest || '') &&
      createHash('sha256').update(fs.readFileSync(file)).digest('hex') === digest);
  } catch (_) { return false; }
}

module.exports = {MAX_STARTUP_MS, UUID, startupDecision, trustedStatusEvent,
  diagnosticReport, recoveryPoint, verifyRecoveryIntegrity, bundledShellMatchesSource};
