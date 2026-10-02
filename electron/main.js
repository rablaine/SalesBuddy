// Sales Buddy - Electron desktop shell.
//
// This is the "shell/supervisor" model (option 1): Electron does NOT replace the
// Flask app - it wraps the SAME git-pulled repo. On launch it starts the Python
// supervisor (which spawns the web + worker under SALESBUDDY_SUPERVISED), waits
// for the web server to come up, then loads it in a BrowserWindow. It lives in
// the tray so the background worker keeps running (morning jobs), and it still
// works in a real browser tab because it's just a local web server.
//
// Updates come via git-pull of the repo (same mechanism as the standalone web
// app), so a single `git pull` updates the app logic in both worlds. Electron
// itself only needs updating when this shell code changes.

const { app, BrowserWindow, Tray, Menu, shell, dialog } = require('electron');
const { spawn, execFile, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const APP_VERSION = require('./package.json').version;

// Where the git-pulled Flask app lives (venv, supervisor, static, .env). In dev
// we run `electron .` from inside the repo, so the repo is one level up from
// electron/. When packaged, the signed exe is dropped in a subfolder of the
// install dir (e.g. <install>\electron-dist\Sales Buddy.exe), so the repo is one
// level up from the exe's folder. SALESBUDDY_REPO overrides both if set.
const REPO_ROOT = process.env.SALESBUDDY_REPO
  ? process.env.SALESBUDDY_REPO
  : (app.isPackaged
    ? path.resolve(path.dirname(process.execPath), '..')
    : path.resolve(__dirname, '..'));
const IS_WIN = process.platform === 'win32';
const PYTHON = path.join(
  REPO_ROOT, 'venv', 'Scripts', IS_WIN ? 'python.exe' : 'python'
);
const ICON = path.join(REPO_ROOT, 'static', 'icon.ico');
const LOG_DIR = path.join(REPO_ROOT, 'logs');
const MAIN_LOG = path.join(LOG_DIR, 'electron-main.log');
const STACK_LOG = path.join(LOG_DIR, 'electron-stack.log');
// Sentinel the web app drops (from the Electron window OR a real browser tab)
// to ask the shell to run a git-pull update. Electron owns the update because
// it owns the supervisor process; letting server.ps1 tear the tree down would
// race with our own supervisor-restart handler.
const UPDATE_REQUEST_FILE = path.join(REPO_ROOT, 'data', 'electron-update.request');
// Sentinel the installer drops to ask us to quit cleanly before it restages the
// shell, so it can delete our exe without fighting a file lock (and the user
// doesn't have to manually Quit from the tray first). We watch it the same way
// as the update request. Shells that predate this just get force-killed by the
// installer instead - the DB snapshot/restore covers either path.
const SHUTDOWN_REQUEST_FILE = path.join(REPO_ROOT, 'data', 'shutdown.request');
// Sentinel the web app drops to ask the shell to rebuild itself from the current
// on-disk repo source (Danger Zone "Rebuild desktop app" button, and the
// auto-chain after a shell-touching update). Handled like the update request but
// runs the build/stage chain instead of a git pull.
const REBUILD_REQUEST_FILE = path.join(REPO_ROOT, 'data', 'electron-rebuild.request');
// Small JSON the backend mirrors shell-relevant preferences into, so the shell
// can read them synchronously at boot before the backend is up (currently just
// start_minimized).
const SHELL_PREFS_FILE = path.join(REPO_ROOT, 'data', 'shell-prefs.json');
// Scripts invoked for a self-rebuild: build.ps1 produces a fresh win-unpacked,
// migrate-to-electron.ps1 stages it into electron-dist and relaunches.
const BUILD_SCRIPT = path.join(REPO_ROOT, 'electron', 'build.ps1');
const MIGRATE_SCRIPT = path.join(REPO_ROOT, 'scripts', 'migrate-to-electron.ps1');
const PORT = readEnvPort();
const HEALTH_URL = `http://localhost:${PORT}/health`;
const BASE_URL = `http://localhost:${PORT}/`;

let supervisorProc = null;
const windows = new Set();
let tray = null;
let isQuitting = false;
let isUpdating = false;
let isBooting = true;
let restartTimer = null;

// Launch flags. --startup = an automatic launch (login autostart / installer
// warm-up): honor the start-minimized preference. --minimized = force hidden
// regardless of the pref (installer warm-up, so it stays hidden until the user
// clicks Finish). No flag = an explicit user launch (shortcut / pin) -> always
// show the window.
const ARG_STARTUP = process.argv.includes('--startup');
const ARG_MINIMIZED = process.argv.includes('--minimized');
// A show requested during boot (tray click, second-instance, or the installer's
// Finish launch) is remembered and honored once the server is ready.
let pendingShowRequest = false;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function log(msg) {
  const line = `${new Date().toISOString()} ${msg}\n`;
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(MAIN_LOG, line);
  } catch (_) { /* ignore */ }
  process.stdout.write(line);
}

function readEnvPort() {
  try {
    const txt = fs.readFileSync(path.join(REPO_ROOT, '.env'), 'utf8');
    const m = txt.match(/^\s*PORT\s*=\s*(\d+)/m);
    if (m) return parseInt(m[1], 10);
  } catch (_) { /* ignore */ }
  return 5151;
}

function readFlaskEnv() {
  try {
    const txt = fs.readFileSync(path.join(REPO_ROOT, '.env'), 'utf8');
    const m = txt.match(/^\s*FLASK_ENV\s*=\s*(.+?)\s*$/m);
    if (m) return m[1].trim().replace(/^['"]|['"]$/g, '').toLowerCase();
  } catch (_) { /* ignore */ }
  return 'production';
}

// Read the start-minimized preference the backend mirrors into shell-prefs.json.
// Synchronous + defensive so it works at boot before the backend is up.
function readStartMinimizedPref() {
  try {
    const obj = JSON.parse(fs.readFileSync(SHELL_PREFS_FILE, 'utf8'));
    return !!(obj && obj.start_minimized);
  } catch (_) { return false; }
}

// Whether this launch should boot straight to the tray (no window) once ready.
function shouldBootHidden() {
  if (ARG_MINIMIZED) return true;                            // warm-up: always hidden
  if (ARG_STARTUP && readStartMinimizedPref()) return true;  // login + pref on
  return false;                                              // explicit launch / off
}

// Updates are user-initiated, so their restart must restore the window even if
// this shell originally started at login. Electron otherwise preserves the
// original --startup/--minimized args and boots the replacement into the tray.
function relaunchVisible() {
  const args = process.argv.slice(1).filter(
    (arg) => arg !== '--startup' && arg !== '--minimized'
  );
  app.relaunch({ args });
}

// Build the environment for the spawned stack. Mirrors scripts/supervisor.ps1:
// point the Azure CLI at the app's isolated per-environment config dir so MSX
// token acquisition uses the same file-based cache as the web/server, and pass
// the port through. Respects an already-set AZURE_CONFIG_DIR.
function buildStackEnv() {
  // SALESBUDDY_ELECTRON tells the backend it's running under the shell, so the
  // admin Update button delegates to Electron (sentinel file) instead of
  // spawning server.ps1 and fighting our supervisor.
  const env = { ...process.env, PORT: String(PORT), SALESBUDDY_ELECTRON: '1' };
  if (!env.AZURE_CONFIG_DIR) {
    const profileFolder =
      readFlaskEnv() === 'development' ? 'SalesBuddyDev' : 'SalesBuddy';
    const home = path.join(os.homedir(), profileFolder);
    env.SALESBUDDY_HOME = home;
    env.AZURE_CONFIG_DIR = path.join(home, '.azure');
  }
  return env;
}

// ---------------------------------------------------------------------------
// Backend stack (Python supervisor -> web + worker)
// ---------------------------------------------------------------------------

function startStack() {
  if (!fs.existsSync(PYTHON)) {
    log(`ERROR: python not found at ${PYTHON}`);
    dialog.showErrorBox(
      'Sales Buddy',
      `Python virtual environment not found at:\n${PYTHON}\n\n` +
      'Run the installer or start.bat once to set it up.'
    );
    return;
  }

  fs.mkdirSync(LOG_DIR, { recursive: true });
  const out = fs.openSync(STACK_LOG, 'a');

  log(`Starting supervisor: ${PYTHON} -m app.supervisor (PORT=${PORT})`);
  supervisorProc = spawn(PYTHON, ['-m', 'app.supervisor'], {
    cwd: REPO_ROOT,
    env: buildStackEnv(),
    // Write stdout/stderr to a file (not a pipe) so nothing can deadlock on a
    // full, unread pipe buffer.
    stdio: ['ignore', out, out],
    windowsHide: true,
  });

  supervisorProc.on('exit', (code, signal) => {
    log(`Supervisor exited (code=${code} signal=${signal})`);
    supervisorProc = null;
    if (!isQuitting && !isUpdating) {
      // The Python supervisor should not exit on its own; if it does, bring the
      // whole stack back up after a short delay. Suppressed during an update -
      // runUpdate() stops the stack on purpose and relaunches when done.
      restartTimer = setTimeout(startStack, 2000);
    }
  });
}

// Block the calling thread for `ms` milliseconds. Used between teardown sweeps
// so killed handles have a beat to release before we recheck the port. Node has
// no synchronous sleep, so we park on an Atomics.wait against a throwaway buffer.
function sleepSync(ms) {
  try { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms); }
  catch (_) { /* SharedArrayBuffer unavailable - skip the pause */ }
}

// True when nothing is LISTENING on `port`. We ask Windows directly (rather than
// trying an async bind) so this stays synchronous inside teardown. On any error
// we conservatively report "busy" so callers keep trying rather than racing a
// squatter for the port.
function portIsFree(port) {
  if (!IS_WIN) return true;
  try {
    const out = execFileSync('powershell',
      ['-NoProfile', '-NonInteractive', '-Command',
        `if (Get-NetTCPConnection -LocalPort ${port} -State Listen ` +
        `-ErrorAction SilentlyContinue) { 'BUSY' } else { 'FREE' }`],
      { windowsHide: true, timeout: 10000 }).toString();
    return out.includes('FREE');
  } catch (_) {
    return false; // uncertain -> treat as busy
  }
}

// Kill every backend process rooted in THIS install (python/waitress shims plus
// any wedged WorkIQ node child) and wait for `port` to actually free up. We run
// a bounded number of passes rather than trusting a single kill, because a
// wedged process (e.g. a stuck WorkIQ subprocess) can take a moment to die.
// Returns true once the port is free, false if it never freed.
function sweepInstallBackendUntilPortFree(port, passes) {
  const root = REPO_ROOT.replace(/'/g, "''");
  const ps =
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR " +
    "Name='pythonw.exe' OR Name='waitress-serve.exe' OR Name='node.exe'\" | " +
    "Where-Object { $_.CommandLine -like '*" + root + "*' } | ForEach-Object { " +
    "taskkill /PID $_.ProcessId /T /F 2>$null }";
  for (let i = 0; i < passes; i++) {
    try {
      execFileSync('powershell',
        ['-NoProfile', '-NonInteractive', '-Command', ps],
        { windowsHide: true, stdio: 'ignore', timeout: 15000 });
    } catch (_) { /* best effort */ }
    if (portIsFree(port)) return true;
    sleepSync(500); // let handles release before the next pass
  }
  return portIsFree(port);
}

function stopStack() {
  if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
  const pid = supervisorProc ? supervisorProc.pid : null;
  supervisorProc = null;
  if (!IS_WIN) {
    if (pid) { try { process.kill(-pid, 'SIGTERM'); } catch (_) {} }
    return;
  }
  // Windows teardown MUST be synchronous and belt-and-suspenders. Two reasons:
  //   1. before-quit does not await async work, so a fire-and-forget taskkill
  //      gets abandoned when Electron exits - leaving the backend alive. Use the
  //      *Sync* variants so quit actually waits for the kills to finish.
  //   2. The venv launcher shims (python.exe / waitress-serve.exe) spawn the REAL
  //      interpreter as a grandchild and then exit, breaking the parent-PID chain.
  //      So `taskkill /T` from the supervisor pid MISSES those orphans. That is
  //      exactly why a tray Quit left waitress + workers running on port 5151,
  //      whose stale WAL then corrupted a later DB restore. So we ALSO sweep any
  //      backend process whose command line lives under THIS install dir.
  if (pid) {
    log(`Stopping supervisor tree (pid ${pid})`);
    try {
      // NOTE: a timeout is essential. Without it, a taskkill that blocks on a
      // process wedged in the kernel would hang stopStack forever, which in turn
      // would keep runUpdate from ever reaching app.exit() - so the old shell
      // lingers holding the single-instance lock and the relaunched shell can
      // never take over. That is the exact shape of the prod update outage.
      execFileSync('taskkill', ['/pid', String(pid), '/T', '/F'],
        { windowsHide: true, stdio: 'ignore', timeout: 20000 });
    } catch (_) { /* already gone, or timed out - the sweep below is the backstop */ }
  }
  // Bounded-retry scoped sweep: kill any backend whose command line references
  // this install dir (catching shim-orphaned grandchildren and wedged WorkIQ node
  // children taskkill /T can't see), retrying until port PORT is actually free.
  const freed = sweepInstallBackendUntilPortFree(PORT, 6);
  if (!freed) {
    log(`WARNING: port ${PORT} still busy after teardown sweeps - the next ` +
      `supervisor boot will self-heal via its pre-flight clear`);
  }
  log('Stack stopped');
}

function waitForServer(onReady, onSlow = () => {}) {
  const deadline = Date.now() + 60000;
  let stopped = false;
  let slow = false;
  let timer = null;
  let request = null;
  let lastError = null;
  const cancel = () => {
    stopped = true;
    clearTimeout(timer);
    if (request) request.destroy();
  };
  const retry = () => {
    if (stopped) return;
    if (isQuitting || isUpdating) return cancel();
    if (!slow && Date.now() >= deadline) {
      slow = true;
      log('Backend startup is taking longer than 60s; continuing readiness checks');
      onSlow();
    }
    timer = setTimeout(attempt, slow ? 2000 : 500);
  };
  const attempt = () => {
    if (stopped) return;
    if (isQuitting || isUpdating) return cancel();
    let settled = false;
    const complete = (ready) => {
      if (settled || stopped) return;
      settled = true;
      if (!ready) return retry();
      cancel();
      onReady();
    };
    const req = http.get(HEALTH_URL, (res) => {
      res.resume();
      complete(res.statusCode === 200);
    });
    request = req;
    req.on('error', (error) => {
      if (!stopped && error.message !== lastError) {
        log(`Backend readiness request failed: ${error.message}`);
        lastError = error.message;
      }
      complete(false);
    });
    req.setTimeout(3000, () => {
      complete(false);
      req.destroy();
    });
  };
  attempt();
  return cancel;
}

// ---------------------------------------------------------------------------
// Updates (Electron-managed git pull)
// ---------------------------------------------------------------------------

// Run one external command to completion, logging its output. Rejects on a
// non-zero exit so the update chain can stop and roll back.
function runStep(file, args, cwd) {
  return new Promise((resolve, reject) => {
    log(`> ${file} ${args.join(' ')}`);
    execFile(
      file, args,
      { cwd, windowsHide: true, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (stdout && stdout.trim()) log(stdout.trim());
        if (stderr && stderr.trim()) log(stderr.trim());
        if (err) return reject(err);
        resolve((stdout || '').trim());
      }
    );
  });
}

// Replace the window contents with a lightweight splash so the user isn't
// staring at a dead backend while we pull/reinstall or rebuild the shell.
function statusScreenUrl(title, subtitle) {
  const h1 = title || 'Updating Sales Buddy';
  const p = subtitle || 'Pulling the latest version. The app will restart automatically.';
  const html =
    '<!doctype html><html><head><meta charset="utf-8"><style>' +
    'body{font-family:Segoe UI,system-ui,sans-serif;background:#0d1117;color:#e6edf3;' +
    'display:flex;flex-direction:column;align-items:center;justify-content:center;' +
    'height:100vh;margin:0}.s{width:42px;height:42px;border:4px solid #30363d;' +
    'border-top-color:#2f81f7;border-radius:50%;animation:spin 1s linear infinite;' +
    'margin-bottom:22px}@keyframes spin{to{transform:rotate(360deg)}}h1{font-weight:600;' +
    'font-size:20px;margin:0 0 8px}p{color:#8b949e;margin:0}</style></head><body>' +
    '<div class=\"s\"></div><h1>' + h1 + '</h1>' +
    '<p>' + p + '</p>' +
    '</body></html>';
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
}

function loadWindowUrl(win, url) {
  if (win.isDestroyed()) return;
  const label = url.startsWith('data:') ? 'status screen' : url;
  win.loadURL(url).catch((error) => log(`Window navigation failed (${label}): ${error.message}`));
}

function showUpdatingScreen(title, subtitle) {
  ensureWindow();
  const data = statusScreenUrl(title, subtitle);
  for (const w of windows) loadWindowUrl(w, data);
}

// True when `cmd` resolves on PATH. Used to confirm git is reachable BEFORE we
// tear the backend down for an update.
function commandAvailable(cmd) {
  try {
    execFileSync(IS_WIN ? 'where' : 'which', [cmd],
      { windowsHide: true, stdio: 'ignore', timeout: 5000 });
    return true;
  } catch (_) {
    return false;
  }
}

// Stop the stack, git pull, reinstall deps, then relaunch Electron. Relaunch
// re-runs this shell (picks up main.js changes) and re-spawns the stack (picks
// up backend changes); DB migrations run automatically on the next boot.
async function runUpdate(trigger, rebuildAfter = false) {
  if (isUpdating) return;
  isUpdating = true;
  log(`Update starting (trigger=${trigger})`);
  // Confirm git is reachable BEFORE tearing the backend down. On a fresh install
  // the shell's PATH may not have picked up git yet (winget just added it); if we
  // stopped the stack first and THEN failed the pull, the app would be left dead.
  // Missing git = no-op: leave the running version untouched.
  if (!commandAvailable('git')) {
    log('Update aborted: git not found on PATH; leaving current version running');
    isUpdating = false;
    dialog.showErrorBox(
      'Sales Buddy Update',
      'Git was not found, so the update could not run.\n\n' +
      'The current version will keep running. If updates keep failing, restart ' +
      'your computer (so PATH refreshes) or reinstall Sales Buddy.'
    );
    return;
  }
  showUpdatingScreen();
  // Never let a teardown error keep us from proceeding to relaunch - the next
  // supervisor boot self-heals the port via its pre-flight clear regardless.
  try { stopStack(); }
  catch (e) { log(`stopStack error during update (continuing): ${(e && e.message) || e}`); }
  try {
    // Discard any local changes to tracked files before pulling. Users have
    // no reason to have edited the install-dir source, but half-finished prior
    // updates, CRLF churn, or WorkIQ/build artifacts can leave the tree dirty
    // and break `git pull --ff-only` with "local changes would be overwritten".
    // fetch + reset --hard is equivalent to a fast-forward pull for a clean
    // tree, and drops the local churn for a dirty one.
    await runStep('git', ['fetch', 'origin', 'main'], REPO_ROOT);
    await runStep('git', ['reset', '--hard', 'origin/main'], REPO_ROOT);
    await runStep(
      PYTHON, ['-m', 'pip', 'install', '-q', '-r', 'requirements.txt'], REPO_ROOT
    );
    if (rebuildAfter) {
      // The incoming update touches the Electron shell (a git pull alone won't
      // apply main.js changes bundled in the exe). Chain the shell rebuild in
      // the same motion instead of relaunching the stale shell. runRebuild owns
      // the relaunch/exit from here.
      log('Update complete; chaining desktop-app rebuild');
      await runRebuild('update');
      return;
    }
    log('Update complete; relaunching');
    isQuitting = true;
    relaunchVisible();
    app.exit(0);
  } catch (e) {
    const msg = (e && e.message) || String(e);
    log(`Update failed: ${msg}`);
    isUpdating = false;
    // Bring the backend back up FIRST, then tell the user. showErrorBox is a
    // blocking modal - if we showed it before restarting, the app would sit dead
    // behind the dialog the whole time the user reads it.
    startStack();
    reloadAllWindows();
    dialog.showErrorBox(
      'Sales Buddy Update',
      `The update could not be completed:\n\n${msg}\n\n` +
      'The current version will keep running.'
    );
  }
}

// Rebuild the Electron shell from the current on-disk repo source, then relaunch.
// Two phases:
//   1. Build a fresh shell (build.ps1 -SkipSign) into electron/dist/win-unpacked
//      WHILE we're still alive. That dir is separate from the live electron-dist,
//      so there's no file conflict, a "Building..." splash covers the slow part,
//      and a build failure leaves the current app running (we just relaunch).
//   2. A running exe can't overwrite its own files, so hand off to a DETACHED
//      helper (migrate-to-electron.ps1 -SkipPull) that waits for our exe handle
//      to release, stages the freshly built shell into electron-dist, repoints
//      autostart/shortcuts, and relaunches. Then we exit so it can take the lock.
// Per decision 4 this is a full teardown (Option B): before-quit tears the
// backend down and the new shell boots it fresh - simpler and race-free.
// May be called standalone (Danger Zone button) or chained from runUpdate.
async function runRebuild(trigger) {
  isUpdating = true; // idempotent: already true when chained from runUpdate
  log(`Shell rebuild starting (trigger=${trigger})`);

  if (!fs.existsSync(BUILD_SCRIPT) || !fs.existsSync(MIGRATE_SCRIPT)) {
    log('Rebuild aborted: build.ps1 or migrate-to-electron.ps1 missing; relaunching');
    isQuitting = true;
    relaunchVisible();
    app.exit(0);
    return;
  }

  showUpdatingScreen(
    'Building the desktop app',
    'Applying a desktop-app update. This takes a minute or two, then Sales Buddy restarts itself.'
  );

  // Phase 1: build (guarded - failure leaves the current shell intact).
  try {
    await runStep(
      'powershell.exe',
      ['-ExecutionPolicy', 'Bypass', '-NoProfile', '-File', BUILD_SCRIPT, '-SkipSign'],
      REPO_ROOT
    );
  } catch (e) {
    const msg = (e && e.message) || String(e);
    log(`Shell rebuild (build phase) failed: ${msg}`);
    dialog.showErrorBox(
      'Sales Buddy',
      `The desktop-app update could not be built:\n\n${msg}\n\n` +
      'Sales Buddy will restart on the current version.'
    );
    isQuitting = true;
    relaunchVisible();
    app.exit(0);
    return;
  }

  // Phase 2: hand off to a helper that stages the freshly built shell and
  // relaunches. It uses the win-unpacked we just built (no -Rebuild) and skips
  // the pull. TWO Windows gotchas handled here:
  //   1. Job object: Electron puts spawned children in a job object that is
  //      terminated when the app exits. A plain `spawn(..., {detached:true})`
  //      helper therefore gets KILLED the instant we app.exit() - which is
  //      exactly why an earlier version built the shell but never restaged. We
  //      launch through `cmd /c start`, which starts the helper in a process that
  //      breaks out of that job so it survives our exit. We also wait for the
  //      launcher to actually fire `start` before exiting.
  //   2. Blindness: the helper's output is teed to logs/shell-rebuild.log so a
  //      failure in this phase is diagnosable instead of silent.
  log('Build complete; handing off to restage helper');
  const rebuildLog = path.join(LOG_DIR, 'shell-rebuild.log');
  const helperCmd = path.join(REPO_ROOT, 'data', 'shell-rebuild-helper.cmd');
  try {
    fs.mkdirSync(path.dirname(helperCmd), { recursive: true });
    fs.writeFileSync(
      helperCmd,
      '@echo off\r\n' +
      `cd /d "${REPO_ROOT}"\r\n` +
      `powershell.exe -ExecutionPolicy Bypass -NoProfile -File "${MIGRATE_SCRIPT}" ` +
      `-SkipPull > "${rebuildLog}" 2>&1\r\n`
    );
    const finishExit = () => { isQuitting = true; app.exit(0); };
    // `start "" /min "<helper>"` breaks the helper out of Electron's job object.
    // Wait for the short-lived cmd launcher to exit (meaning `start` has fired
    // and the helper is now independent) before we quit; a safety timeout covers
    // the case where the exit event never arrives.
    const child = spawn(
      'cmd.exe', ['/c', 'start', '', '/min', helperCmd],
      { cwd: REPO_ROOT, stdio: 'ignore', windowsHide: true }
    );
    child.on('exit', finishExit);
    child.on('error', (e) => {
      log(`Restage launcher error: ${(e && e.message) || e}; relaunching current shell`);
      isQuitting = true;
      relaunchVisible();
      app.exit(0);
    });
    setTimeout(finishExit, 5000);
  } catch (e) {
    const msg = (e && e.message) || String(e);
    log(`Failed to launch restage helper: ${msg}; relaunching current shell`);
    dialog.showErrorBox(
      'Sales Buddy',
      'The desktop-app update could not be finished. Sales Buddy will restart on ' +
      'the current version. You can retry from Admin > Danger Zone > Rebuild desktop app.'
    );
    isQuitting = true;
    relaunchVisible();
    app.exit(0);
  }
}

// Poll for the web-app sentinel. Works whether the user clicked Update in the
// Electron window or in a real browser tab pointed at the same local server.
function startUpdateRequestWatcher() {
  // A prior shell that was force-killed or crashed (e.g. the installer closing us
  // via the fallback path) can leave a stale shutdown.request behind. Clear it
  // once at startup so THIS freshly-launched shell doesn't read a request meant
  // for a previous instance and immediately quit. We only honor sentinels that
  // appear AFTER we start watching.
  try { fs.unlinkSync(SHUTDOWN_REQUEST_FILE); } catch (_) { /* not present */ }
  setInterval(() => {
    try {
      // Installer asking us to close so it can restage the shell. Quit the same
      // way the tray "Quit" does: isQuitting bypasses close-to-tray, and
      // before-quit tears the backend down. Checked before the update sentinel
      // (and regardless of isUpdating) so a shutdown request always wins.
      if (fs.existsSync(SHUTDOWN_REQUEST_FILE)) {
        try { fs.unlinkSync(SHUTDOWN_REQUEST_FILE); } catch (_) { /* ignore */ }
        log('Shutdown requested (installer sentinel) - quitting cleanly');
        isQuitting = true;
        app.quit();
        return;
      }
      if (isUpdating) return;
      // Rebuild request (Danger Zone failsafe): rebuild the shell from the
      // current on-disk source, no git pull.
      if (fs.existsSync(REBUILD_REQUEST_FILE)) {
        try { fs.unlinkSync(REBUILD_REQUEST_FILE); } catch (_) { /* ignore */ }
        runRebuild('web');
        return;
      }
      if (fs.existsSync(UPDATE_REQUEST_FILE)) {
        // Content 'rebuild' = the incoming update touches the shell, so chain a
        // rebuild after the pull. Anything else (a timestamp) = pull + relaunch.
        let content = '';
        try { content = fs.readFileSync(UPDATE_REQUEST_FILE, 'utf8'); } catch (_) { /* ignore */ }
        try { fs.unlinkSync(UPDATE_REQUEST_FILE); } catch (_) { /* ignore */ }
        const rebuildAfter = content.trim().toLowerCase() === 'rebuild';
        runUpdate('web', rebuildAfter);
      }
    } catch (_) { /* ignore */ }
  }, 1500);
}

// Compare the local HEAD against its upstream. Prompts to update when behind;
// when interactive, also reports "up to date" and surfaces errors.
async function checkForUpdates(interactive) {
  if (isUpdating) return;
  try {
    await runStep('git', ['fetch', '--quiet'], REPO_ROOT);
    const out = await runStep('git', ['rev-list', '--count', 'HEAD..@{u}'], REPO_ROOT);
    const behind = parseInt(out, 10) || 0;
    if (behind > 0) {
      const choice = dialog.showMessageBoxSync({
        type: 'question',
        buttons: ['Update Now', 'Later'],
        defaultId: 0,
        cancelId: 1,
        title: 'Sales Buddy',
        message: `An update is available (${behind} new change${behind === 1 ? '' : 's'}).`,
        detail: 'Sales Buddy will restart to apply the update.',
      });
      if (choice === 0) runUpdate('menu');
    } else if (interactive) {
      dialog.showMessageBox({
        type: 'info', title: 'Sales Buddy', buttons: ['OK'],
        message: "You're up to date.",
      });
    }
  } catch (e) {
    const msg = (e && e.message) || String(e);
    log(`Update check failed: ${msg}`);
    if (interactive) {
      dialog.showErrorBox('Sales Buddy', `Could not check for updates:\n\n${msg}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Window + tray
// ---------------------------------------------------------------------------

// The whole app is same-origin HTTP navigation served from the local backend,
// so navigations must stay INSIDE the window; only genuinely off-site links
// (MSX, aka.ms, etc. - all target=_blank) go to the real browser. The old check
// was a string-prefix allowlist for exactly "localhost:PORT" / "127.0.0.1:PORT".
// That breaks on machines where the window's origin isn't one of those exact
// spellings - e.g. it loaded via [::1] (IPv6 localhost), the machine hostname,
// or the LAN IP because waitress binds 0.0.0.0. There, EVERY relative link
// resolves to that origin, fails the allowlist, and gets shoved to the browser
// (the "every GET opens a new browser window" bug). Fix: treat a URL as internal
// if it shares the host of the page we're currently on (covers all relative
// links regardless of the window's actual origin) or is any loopback host.
function safeUrl(wc) {
  try { return (wc && wc.getURL()) || ''; } catch (_) { return ''; }
}

// The focused app window (for menu actions), or the most recent one.
function activeWindow() {
  const f = BrowserWindow.getFocusedWindow();
  if (f && windows.has(f) && !f.isDestroyed()) return f;
  const arr = [...windows].filter((w) => !w.isDestroyed());
  return arr.length ? arr[arr.length - 1] : null;
}

function reloadAllWindows() {
  for (const w of windows) loadWindowUrl(w, BASE_URL);
}

function isInternalUrl(target, fromUrl) {
  let u;
  try { u = new URL(target); } catch (_) { return true; } // relative/unparsable
  const scheme = u.protocol;
  if (scheme === 'data:' || scheme === 'blob:' || scheme === 'about:') return true;
  if (scheme !== 'http:' && scheme !== 'https:') return false; // mailto:, tel:, ...
  // Same host as the page the link came from -> in-app navigation.
  try {
    const cur = new URL(fromUrl || '');
    if (cur.host && u.host === cur.host) return true;
  } catch (_) { /* no current page yet */ }
  // Any loopback host (any spelling, any port) is this machine's own backend.
  const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  return host === 'localhost' || host === '127.0.0.1' || host === '::1';
}

// Shared options for every app window.
const WINDOW_OPTS = {
  width: 1440,
  height: 920,
  minWidth: 900,
  minHeight: 600,
  show: false,
  title: 'Sales Buddy',
  icon: ICON,
  backgroundColor: '#0d1117',
  autoHideMenuBar: false,
  webPreferences: {
    contextIsolation: true,
    nodeIntegration: false,
    preload: path.join(__dirname, 'preload.js'),
  },
};

// Open a NEW app window (browser-tab style). Every window is an independent view
// of the same local backend - the server is the shared state - so this is nearly
// free. Used by File > New Window, the tray, and internal-link "open in new
// window" (middle-click / Ctrl+click / right-click).
function openNewWindow(url) {
  const win = new BrowserWindow(WINDOW_OPTS);
  windows.add(win);
  configureWindow(win);
  loadWindowUrl(win, isBooting ? statusScreenUrl(
    'Starting Sales Buddy', 'Waiting for the local backend. This window will open automatically.'
  ) : (url || BASE_URL));
  win.once('ready-to-show', () => win.show());
  return win;
}

// Boot / tray "Open": make sure at least one window is showing. Idempotent so a
// second-instance during boot (or a double server-ready) can't spawn an orphaned
// duplicate window.
function ensureWindow() {
  const alive = [...windows].filter((w) => !w.isDestroyed());
  if (alive.length) {
    const w = activeWindow() || alive[alive.length - 1];
    if (w.isMinimized()) w.restore();
    w.show();
    w.focus();
    return w;
  }
  return openNewWindow();
}

// Wire the browser-like behavior onto a window: title mirroring, internal-vs-
// external nav gating, open-links-in-new-window, F5, the right-click menu, and
// the close-to-tray-on-last-window rule.
function configureWindow(win) {
  const wc = win.webContents;
  let cancelRecovery = null;
  let recoveryTimer = null;
  const stopRecovery = () => {
    clearTimeout(recoveryTimer);
    recoveryTimer = null;
    if (cancelRecovery) cancelRecovery();
    cancelRecovery = null;
  };
  win.on('closed', stopRecovery);
  wc.on('did-start-navigation', (_event, url, _inPlace, isMainFrame) => {
    if (isMainFrame && /^https?:/.test(url)) stopRecovery();
  });
  wc.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
    if (!isMainFrame || code === -3 || isQuitting || isUpdating) return;
    log(`Page load failed: code=${code}, reason=${description}, url=${url}`);
    let failed;
    try { failed = new URL(url); } catch (_) { return; }
    const host = failed.hostname.replace(/^\[|\]$/g, '').toLowerCase();
    if (!['localhost', '127.0.0.1', '::1'].includes(host) ||
        failed.port !== String(PORT)) return;
    stopRecovery();
    loadWindowUrl(win, statusScreenUrl(
      'Reconnecting to Sales Buddy',
      'The local backend is unavailable. Retrying automatically. Help > Open Logs Folder has details.'
    ));
    recoveryTimer = setTimeout(() => {
      recoveryTimer = null;
      cancelRecovery = waitForServer(() => {
        cancelRecovery = null;
        if (win.isDestroyed() || isQuitting || isUpdating) return;
        log(`Backend ready; retrying page: ${url}`);
        loadWindowUrl(win, url);
      });
    }, 1000);
  });
  wc.on('render-process-gone', (_event, details) => {
    log(`Renderer exited: reason=${details.reason}, exitCode=${details.exitCode}`);
  });

  // Mirror the page's <title> (already ends in "- Sales Buddy"); fall back to the
  // bare app name when a page provides none.
  win.on('page-title-updated', (e) => {
    e.preventDefault();
    win.setTitle(wc.getTitle() || 'Sales Buddy');
  });

  // Internal target=_blank / window.open / middle-click / Ctrl+click -> a managed
  // NEW app window. Genuinely off-site links -> the real browser.
  wc.setWindowOpenHandler(({ url }) => {
    if (isInternalUrl(url, safeUrl(wc))) {
      openNewWindow(url);
      return { action: 'deny' };
    }
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Full-page navigations stay in-window unless they're genuinely off-site.
  wc.on('will-navigate', (e, url) => {
    if (!isInternalUrl(url, safeUrl(wc))) {
      e.preventDefault();
      log(`nav -> external browser: ${url} (from ${safeUrl(wc)})`);
      shell.openExternal(url);
    }
  });

  // Keep the Back/Forward menu state synced to whichever window is active.
  wc.on('did-navigate', buildAppMenu);
  wc.on('did-navigate-in-page', buildAppMenu);
  win.on('focus', buildAppMenu);

  // F5 reloads (Ctrl+R / Ctrl+Shift+R come from the View menu roles).
  wc.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.key === 'F5') wc.reload();
  });

  // Browser-style right-click menu (Electron ships none). Adds "Open Link in New
  // Window" for internal links.
  wc.on('context-menu', (_e, params) => {
    const h = wc.navigationHistory;
    const items = [
      { label: 'Back', enabled: !!(h && h.canGoBack()), click: () => { if (h.canGoBack()) h.goBack(); } },
      { label: 'Forward', enabled: !!(h && h.canGoForward()), click: () => { if (h.canGoForward()) h.goForward(); } },
      { role: 'reload' },
    ];
    if (params.linkURL && isInternalUrl(params.linkURL, safeUrl(wc))) {
      items.push({ type: 'separator' });
      items.push({ label: 'Open Link in New Window', click: () => openNewWindow(params.linkURL) });
    }
    items.push(
      { type: 'separator' },
      { role: 'cut', enabled: params.editFlags.canCut },
      { role: 'copy', enabled: params.editFlags.canCopy },
      { role: 'paste', enabled: params.editFlags.canPaste },
      { role: 'selectAll' }
    );
    Menu.buildFromTemplate(items).popup({ window: win });
  });

  // Closing a window destroys just that window; the LAST window hides to tray
  // instead (keeps the background worker alive). A real quit closes everything.
  win.on('close', (e) => {
    if (isQuitting) return;
    const alive = [...windows].filter((w) => !w.isDestroyed());
    if (alive.length > 1) return; // others remain -> let this one close
    e.preventDefault();
    win.hide();
  });

  win.on('closed', () => windows.delete(win));
}

// Direct-action navigation bound to the active page's history. Used by the
// top-level "< Back" / "Forward >" menu buttons and their accelerators.
function navHistory() {
  const w = activeWindow();
  return (w && !w.isDestroyed()) ? w.webContents.navigationHistory : null;
}
function goBack() {
  const h = navHistory();
  if (h && h.canGoBack()) h.goBack();
}
function goForward() {
  const h = navHistory();
  if (h && h.canGoForward()) h.goForward();
}

// Native application menu. Gives the shell the standard File/Edit/View/Window/
// Help structure users expect from a desktop app (plus our own actions). The
// leading "< Back" / "Forward >" entries are top-level buttons: clicking them
// navigates immediately, no submenu.
function buildAppMenu() {
  const h = navHistory();
  const template = [
    {
      label: 'File',
      submenu: [
        {
          label: 'New Window',
          accelerator: 'CmdOrCtrl+N',
          click: () => openNewWindow(),
        },
        { label: 'Open in Browser', click: () => shell.openExternal(BASE_URL) },
        { type: 'separator' },
        { label: 'Check for Updates...', click: () => checkForUpdates(true) },
        {
          label: 'Restart Backend',
          click: () => {
            stopStack();
            startStack();
            reloadAllWindows();
          },
        },
        { type: 'separator' },
        {
          label: 'Close Window',
          accelerator: 'CmdOrCtrl+W',
          click: () => { const w = activeWindow(); if (w) w.close(); },
        },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
        { type: 'separator' },
        {
          label: 'Find in Page',
          accelerator: 'CmdOrCtrl+F',
          click: () => { const w = activeWindow(); if (w) w.webContents.send('find:show'); },
        },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
      ],
    },
    {
      role: 'help',
      submenu: [
        { label: 'Open Logs Folder', click: () => shell.openPath(LOG_DIR) },
        { label: 'Open App Folder', click: () => shell.openPath(REPO_ROOT) },
        { type: 'separator' },
        { label: 'About Sales Buddy', click: showAbout },
      ],
    },
    {
      label: '< Back',
      enabled: !!(h && h.canGoBack()),
      accelerator: 'Alt+Left',
      click: goBack,
    },
    {
      label: 'Forward >',
      enabled: !!(h && h.canGoForward()),
      accelerator: 'Alt+Right',
      click: goForward,
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function showAbout() {
  const opts = {
    type: 'info',
    title: 'About Sales Buddy',
    message: 'Sales Buddy',
    detail:
      `Version ${APP_VERSION}\n` +
      `Electron ${process.versions.electron}  •  ` +
      `Chromium ${process.versions.chrome}  •  Node ${process.versions.node}\n\n` +
      'A note-taking desktop app for Azure technical sellers.',
    buttons: ['OK'],
    icon: fs.existsSync(ICON) ? ICON : undefined,
    noLink: true,
  };
  const w = activeWindow();
  if (w) dialog.showMessageBox(w, opts); else dialog.showMessageBox(opts);
}

function showWindow() {
  // During boot the first window is deferred until /health answers, so a tray
  // click or second-instance must not create an early "Not Running" window.
  // Remember the request so it fires once boot completes (covers a minimized
  // boot and the installer's Finish launch racing our startup).
  if (isBooting) { pendingShowRequest = true; return; }
  // Un-hide the window that was last closed to tray, if any; else ensure one.
  const hidden = [...windows].find((w) => !w.isDestroyed() && !w.isVisible());
  if (hidden) {
    if (hidden.isMinimized()) hidden.restore();
    hidden.show();
    hidden.focus();
    return;
  }
  ensureWindow();
}

function createTray() {
  tray = new Tray(ICON);
  tray.setToolTip('Sales Buddy');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open Sales Buddy', click: showWindow },
    { label: 'New Window', click: () => openNewWindow() },
    { label: 'Open in Browser', click: () => shell.openExternal(BASE_URL) },
    { type: 'separator' },
    { label: 'Check for Updates', click: () => checkForUpdates(true) },
    { label: 'Restart Backend', click: () => { stopStack(); startStack(); reloadAllWindows(); } },
    { type: 'separator' },
    { label: 'Quit', click: () => { isQuitting = true; app.quit(); } },
  ]));
  tray.on('click', showWindow);
  tray.on('double-click', showWindow);
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

// Single instance: focus the existing window instead of launching a second copy.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);

  app.whenReady().then(() => {
    log(`Electron ready (v${APP_VERSION}, packaged=${app.isPackaged}, repo=${REPO_ROOT})`);
    // Group under one taskbar icon and identify our notifications on Windows.
    if (IS_WIN) app.setAppUserModelId('com.salesbuddy.desktop');
    buildAppMenu();
    startStack();
    createTray();
    startUpdateRequestWatcher();
    if (!shouldBootHidden() || pendingShowRequest) ensureWindow();
    waitForServer(() => {
      isBooting = false;
      reloadAllWindows();
      // Boot straight to the tray (no window) for an automatic launch when the
      // user asked to start minimized, or when forced (installer warm-up) -
      // unless a show was already requested during boot (e.g. the installer's
      // Finish launch fired a second-instance while we were still starting).
      if (!shouldBootHidden() || pendingShowRequest) {
        ensureWindow();
      } else {
        log('Starting minimized to the system tray.');
      }
      // Quiet check shortly after boot: only prompts if an update is available.
      setTimeout(() => checkForUpdates(false), 5000);
    }, () => {
      if (!shouldBootHidden() || pendingShowRequest) {
        showUpdatingScreen(
          'Starting Sales Buddy',
          'Startup is taking longer than usual. Retrying automatically. Help > Open Logs Folder has details.'
        );
      }
    });
  });

  // Stay alive in the tray when the window is closed.
  app.on('window-all-closed', (e) => {
    // Intentionally do nothing (don't quit) - the tray keeps us running.
  });

  app.on('before-quit', () => {
    isQuitting = true;
    stopStack();
  });
}
