const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function shellHarness({ boot = false, minimized = false, cachedTheme = null,
  devClient = false, renderingPrefs = null, renderingReadError = null,
  renderingWriteError = null, renderingRenameError = null, restartChoice = 1,
  windowState = null, startup = null, upgrade = null, teardownFree = true,
  commandFailure = null } = {}) {
  const bootId = '12345678-1234-4234-8234-123456789abc';
  const commit = 'a'.repeat(40);
  const commands = [];
  const children = [];
  const defaultStatus = () => ({schema_version: 1, state: 'ready', phase: 'ready',
    boot_id: bootId, expected_commit: commit, started_at: new Date(0).toISOString(),
    updated_at: new Date(now).toISOString(), children: [
      {name: 'web', pid: 101, attempts: 1, ready: true},
      {name: 'worker', pid: 102, attempts: 1, ready: true}]});
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const requests = [];
  const logs = [];
  const openedWindows = [];
  const renderingWrites = [];
  const renderingRenames = [];
  const windowWrites = [];
  const helperWrites = [];
  const appEvents = new EventEmitter();
  const exits = [];
  const dialogs = [];
  const errors = [];
  const relaunches = [];
  const focusDiagnostics = [];
  let accelerationDisabled = false;
  let ready = false;
  let quitCount = 0;
  const stdout = new EventEmitter();
  stdout.write = () => {};
  let bootCallback;
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'electron', 'main.js'), 'utf8');
  const electron = {
    app: {
      isPackaged: false, requestSingleInstanceLock: () => boot, quit() { quitCount++; },
      getPath: () => 'C:\\test-shell-profile',
      on: (...args) => appEvents.on(...args),
      exit: code => exits.push(code),
      setAppUserModelId() {}, setPath() {},
      disableHardwareAcceleration() {
        assert.equal(ready, false, 'rendering must be configured before app readiness');
        accelerationDisabled = true;
      },
      relaunch: options => relaunches.push(options),
      whenReady: () => ({ then: (callback) => { bootCallback = callback; } }),
    },
    dialog: {
      showMessageBox: async options => {
        dialogs.push(options);
        return {response: restartChoice};
      },
      showErrorBox: (title, message) => errors.push({title, message}),
      showSaveDialog: async () => ({canceled: false, filePath: 'chosen.jsonl'}),
    },
    BrowserWindow: function (options) {
      const win = electron.nextWindow || fakeWindow();
      win.options = options;
      openedWindows.push(win);
      return win;
    },
    menuTemplates: [],
    Menu: { buildFromTemplate: (template) => {
      electron.menuTemplates.push(template);
      return {};
    }, setApplicationMenu() {} },
    Tray: class extends EventEmitter { setToolTip() {} setContextMenu() {} },
    screen: Object.assign(new EventEmitter(), {
      getAllDisplays: () => [{bounds: {x: 0, y: 0, width: 1920, height: 1080},
        workArea: {x: 0, y: 0, width: 1920, height: 1040}}],
      getPrimaryDisplay() { return this.getAllDisplays()[0]; },
      getDisplayNearestPoint() { return this.getPrimaryDisplay(); },
      getCursorScreenPoint: () => ({x: 100, y: 100}),
    }),
  };
  electron.BrowserWindow.getFocusedWindow = () => null;
  const context = vm.createContext({
    require(name) {
      if (name === 'electron') return electron;
      if (name === './package.json') return { version: 'test' };
      if (name === './status-screen') return require('../../electron/status-screen');
      if (name === './upgrade-safety') return require('../../electron/upgrade-safety');
      if (name === 'crypto') return {randomUUID: () => bootId};
      if (name === './shell-theme') return require('../../electron/shell-theme');
      if (name === './shell-menu') return require('../../electron/shell-menu');
      if (name === './window-state') return require('../../electron/window-state');
      if (name === './input-focus') return {
        createInputFocusDiagnostics: () => ({
          attach: win => focusDiagnostics.push({event: 'attach', win}),
          markIncident: () => focusDiagnostics.push({event: 'incident'}),
          save: file => focusDiagnostics.push({event: 'save', file}),
          shutdown: () => focusDiagnostics.push({event: 'shutdown'}),
        }),
      };
      if (name === 'fs') return {
        readFileSync: file => {
          if (file.endsWith('manifest.json')) return JSON.stringify(upgrade);
          if (file.endsWith('recover.py')) return Buffer.from('frozen recovery runner');
          if (file.endsWith('db_paths.py')) return Buffer.from('frozen backup helper');
          if (file.endsWith('startup-state.json')) return JSON.stringify(startup || defaultStatus());
          if (file.endsWith('upgrade-state.json')) return JSON.stringify(upgrade);
          if (file.includes('window-state') && file.endsWith('state.json')) {
            if (windowState !== null) return JSON.stringify(windowState);
            throw Object.assign(new Error('not found'), {code: 'ENOENT'});
          }
          if (file.endsWith('rendering-preferences.json')) {
            if (renderingReadError) throw renderingReadError;
            if (renderingPrefs === null) {
              throw Object.assign(new Error('not found'), {code: 'ENOENT'});
            }
            return renderingPrefs;
          }
          return file.endsWith('shell-theme.json') && cachedTheme
            ? JSON.stringify(cachedTheme) : '';
        },
        writeFileSync(file, value) {
          if (file.endsWith('shell-rebuild-helper.cmd')) helperWrites.push({file, value});
          if (file.endsWith('state.json.tmp')) windowWrites.push({file, value});
          if (file.endsWith('rendering-preferences.json.tmp')) {
            if (renderingWriteError) throw renderingWriteError;
            renderingWrites.push({file, value});
          }
        },
        renameSync(from, to) {
          if (renderingRenameError) throw renderingRenameError;
          renderingRenames.push({from, to});
        },
        mkdirSync() {}, existsSync: () => true,
        openSync: () => 1, unlinkSync() {},
        appendFileSync: (_file, line) => logs.push(line),
      };
      if (name === 'child_process') return {
        spawn(file, args, options) {
          const child = new EventEmitter();
          child.pid = 100;
          children.push({file, args, options, child});
          return child;
        },
        execFileSync(file, args) {
          if (args.includes('rev-parse')) return Buffer.from(commit);
          if (args.some(arg => String(arg).includes('Get-NetTCPConnection'))) {
            return Buffer.from(teardownFree ? 'FREE' : 'BUSY');
          }
          return Buffer.from('');
        },
        execFile(file, args, options, callback) {
          commands.push({file, args, options});
          callback(commandFailure && args.includes(commandFailure)
            ? Object.assign(new Error('private failure'), {code: 1}) : null, '', '');
        },
      };
      if (name === 'http') return {
        get(url, callback) {
          const request = new EventEmitter();
          request.url = url;
          request.respond = (statusCode, health = {status: 'healthy', database: 'connected',
            worker_heartbeat_at: new Date(now).toISOString(), boot_id: bootId,
            commit_full: commit, web_pid: 101, worker: 'alive', worker_boot_id: bootId,
            worker_pid: 102, worker_ready: true}) => {
            const response = new EventEmitter();
            response.statusCode = statusCode;
            callback(response);
            response.emit('data', JSON.stringify(health));
            response.emit('end');
          };
          request.setTimeout = (_ms, handler) => { request.timeout = handler; };
          request.destroy = () => request.emit('error', new Error('request destroyed'));
          requests.push(request);
          return request;
        },
      };
      return require(name);
    },
    __dirname: path.join(__dirname, '..', '..', 'electron'),
    process: {
      env: {}, platform: 'win32',
      pid: 200,
      argv: ['electron', 'main.js', ...(minimized ? ['--minimized', '--other'] : []),
        ...(devClient ? ['--dev-client'] : [])],
      stdout,
    },
    URL,
    Date: class extends Date { static now() { return now; } },
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, at: now + delay });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    setInterval() {},
  });
  vm.runInContext(source + `
    bootIdentity = {bootId: '${bootId}', commit: '${commit}', started: 0, exited: false};
    globalThis.api = {
      waitForServer, configureWindow, loadWindowUrl, openNewWindow, showWindow,
      ensureWindow, relaunchVisible, exitShell, buildAppMenu, createTray, refreshTrayMenu,
      runUpdate, retryStartup, startStack, statusAction, showFailure,
      setUpdating: (value) => { isUpdating = value; },
      setQuitting: (value) => { isQuitting = value; },
      setBooting: (value) => { isBooting = value; },
    };
  `, context);
  function tick(milliseconds) {
    now += milliseconds;
    const due = [...timers.entries()].filter(([, timer]) => timer.at <= now);
    for (const [id, timer] of due) {
      if (!timers.delete(id)) continue;
      timer.callback();
    }
  }
  return {
    api: context.api, requests, logs, timers, tick, electron, openedWindows, stdout,
    commands, children,
    renderingWrites, renderingRenames, dialogs, errors, relaunches, windowWrites, helperWrites,
    appEvents, exits,
    focusDiagnostics,
    accelerationDisabled: () => accelerationDisabled,
    quitCount: () => quitCount,
    boot: () => { ready = true; return bootCallback(); },
  };
}

test('focus diagnostics start with the shell, attach to windows and export from Help', async () => {
  const h = shellHarness({boot: true, devClient: true});
  h.boot();
  assert.ok(h.focusDiagnostics.some(entry => entry.event === 'attach'));
  const help = h.electron.menuTemplates.at(-1).find(item => item.role === 'help');
  await help.submenu.find(item => item.label === 'Save Input Focus Diagnostics...').click();
  assert.ok(h.focusDiagnostics.some(entry => entry.event === 'incident'));
  assert.ok(h.focusDiagnostics.some(entry => entry.event === 'save' && entry.file === 'chosen.jsonl'));
  h.appEvents.emit('before-quit');
  assert.ok(h.focusDiagnostics.some(entry => entry.event === 'shutdown'));
});

function renderingItem(harness) {
  harness.api.buildAppMenu();
  return harness.electron.menuTemplates.at(-1).find(item => item.label === 'View')
    .submenu.find(item => item.type === 'checkbox');
}

test('GPU rendering remains the default for new and explicitly enabled profiles', () => {
  for (const renderingPrefs of [null, '{"use_gpu_rendering":true}']) {
    const h = shellHarness({renderingPrefs});
    assert.equal(h.accelerationDisabled(), false);
    assert.equal(renderingItem(h).checked, true);
  }
});

test('software rendering is configured before app readiness from the saved preference', () => {
  const h = shellHarness({boot: true, devClient: true,
    renderingPrefs: '{"use_gpu_rendering":false}'});
  assert.equal(h.accelerationDisabled(), true);
  h.boot();
  assert.equal(renderingItem(h).checked, false);
});

test('invalid or unreadable rendering preferences log the problem and keep GPU rendering', () => {
  for (const renderingPrefs of ['broken json', '{}', 'null', '{"use_gpu_rendering":"false"}']) {
    const h = shellHarness({renderingPrefs});
    assert.equal(h.accelerationDisabled(), false);
    assert.ok(h.logs.some(line => line.includes('Could not read rendering preference')));
  }
  const h = shellHarness({renderingReadError: Object.assign(new Error('denied'), {code: 'EACCES'})});
  assert.equal(h.accelerationDisabled(), false);
  assert.ok(h.logs.some(line => line.includes('denied')));
});

test('rendering toggle saves atomically and Later does not restart or change the current renderer',
  async () => {
    const h = shellHarness({boot: true, devClient: true});
    const item = renderingItem(h);
    item.checked = false;
    await item.click(item);
    assert.deepEqual(JSON.parse(h.renderingWrites[0].value), {use_gpu_rendering: false});
    assert.match(h.renderingRenames[0].to, /rendering-preferences\.json$/);
    assert.match(h.renderingWrites[0].file, /test-shell-profile/);
    assert.equal(h.accelerationDisabled(), false);
    assert.equal(h.relaunches.length, 0);
    assert.equal(h.quitCount(), 0);
    assert.equal(h.dialogs[0].defaultId, 1);
    assert.match(h.dialogs[0].message, /software rendering/);
    assert.match(renderingItem(h).label, /restart required/);
    assert.equal(renderingItem(h).checked, false);
  });

test('switching back to the running mode cancels the pending change without another prompt',
  async () => {
    const h = shellHarness();
    let item = renderingItem(h);
    item.checked = false;
    await item.click(item);
    item = renderingItem(h);
    item.checked = true;
    await item.click(item);
    assert.deepEqual(JSON.parse(h.renderingWrites.at(-1).value), {use_gpu_rendering: true});
    assert.equal(h.dialogs.length, 1);
    assert.equal(renderingItem(h).label, 'Use GPU Rendering');
  });

test('software profiles can re-enable GPU rendering and explicitly restart visibly', async () => {
  const h = shellHarness({boot: true, minimized: true, restartChoice: 0,
    renderingPrefs: '{"use_gpu_rendering":false}'});
  const item = renderingItem(h);
  item.checked = true;
  await item.click(item);
  assert.deepEqual(JSON.parse(h.renderingWrites[0].value), {use_gpu_rendering: true});
  assert.equal(h.relaunches.length, 1);
  assert.equal(h.relaunches[0].args.includes('--minimized'), false);
  assert.equal(h.quitCount(), 1);
  assert.match(h.dialogs[0].message, /GPU rendering/);
});

test('rendering save failures keep the original checkbox and surface an error', async () => {
  for (const failure of ['renderingWriteError', 'renderingRenameError']) {
    const h = shellHarness({[failure]: new Error('disk unavailable')});
    const item = renderingItem(h);
    item.checked = false;
    await item.click(item);
    assert.equal(item.checked, true);
    assert.equal(renderingItem(h).checked, true);
    assert.equal(h.dialogs.length, 0);
    assert.equal(h.errors.length, 1);
    assert.ok(h.logs.some(line => line.includes('Could not save rendering preference')));
  }
});

test('rendering mode cannot be changed during an app update', () => {
  const h = shellHarness();
  h.api.setUpdating(true);
  assert.equal(renderingItem(h).enabled, false);
});

function fakeWindow() {
  const win = new EventEmitter();
  win.webContents = new EventEmitter();
  win.webContents.ipc = new EventEmitter();
  win.ipcHandlers = new Map();
  win.webContents.ipc.handle = (channel, handler) => win.ipcHandlers.set(channel, handler);
  win.webContents.getURL = () => win.loaded.at(-1) || '';
  win.webContents.mainFrame = {url: ''};
  win.webContents.send = () => {};
  win.webContents.isDestroyed = () => false;
  win.webContents.setWindowOpenHandler = () => {};
  win.loaded = [];
  win.destroyed = false;
  win.isDestroyed = () => win.destroyed;
  win.isMinimized = () => false;
  win.isMaximized = () => !!win.maximized;
  win.isFullScreen = () => false;
  win.getNormalBounds = () => ({x: win.options.x, y: win.options.y,
    width: win.options.width, height: win.options.height});
  win.maximize = () => { win.maximized = true; };
  win.show = () => { win.shown = true; };
  win.focus = () => {};
  win.loadURL = (url) => {
    win.loaded.push(url);
    win.webContents.mainFrame.url = url;
    return Promise.resolve();
  };
  return win;
}

test('native navigation publishes activity for main-frame pages and clears on stop', () => {
  const h = shellHarness();
  const win = fakeWindow();
  const sent = [];
  win.webContents.send = (channel, value) => sent.push({channel, value});
  h.api.configureWindow(win);
  for (const [inPlace, mainFrame] of [[false, false], [true, true]]) {
    win.webContents.emit('did-start-navigation', {}, 'http://localhost:5151/customers',
      inPlace, mainFrame);
  }
  assert.equal(sent.length, 0);
  win.webContents.emit('did-start-navigation', {}, 'http://localhost:5151/customers', false, true);
  assert.deepEqual(sent, [{channel: 'shell:loading', value: true}]);
  win.webContents.emit('did-stop-loading');
  assert.deepEqual(sent.at(-1), {channel: 'shell:loading', value: false});
  win.destroyed = true;
  win.webContents.emit('did-stop-loading');
  assert.equal(sent.length, 2);
});

test('startup exceeds 60 seconds without loading an unavailable backend', () => {
  const harness = shellHarness();
  let ready = 0;
  let slow = 0;
  harness.api.waitForServer(() => ready++, () => slow++);
  harness.requests[0].emit('error', new Error('ECONNREFUSED'));
  harness.tick(60000);
  harness.requests[1].respond(503);
  assert.equal(ready, 0);
  assert.equal(slow, 1);
  assert.equal(harness.timers.size, 1);
  harness.tick(2000);
  harness.requests[2].respond(200);
  assert.equal(ready, 1);
  assert.equal(harness.timers.size, 0);
  assert.ok(harness.logs.some((line) => line.includes('continuing readiness checks')));
});

test('new startup windows use the saved caption colors and bundled palette before Flask is ready', () => {
  const cachedTheme = {mode: 'light', family: 'carbon', color: '#e9e2d6',
    symbolColor: '#25211b', backgroundColor: '#f0ece5'};
  const h = shellHarness({cachedTheme});
  h.api.setBooting(true);
  const win = h.api.openNewWindow();
  assert.equal(win.options.titleBarStyle, 'hidden');
  assert.equal(win.options.titleBarOverlay.color, cachedTheme.color);
  assert.equal(win.options.titleBarOverlay.symbolColor, cachedTheme.symbolColor);
  assert.equal(win.options.backgroundColor, cachedTheme.backgroundColor);
  const html = decodeURIComponent(win.loaded[0]);
  assert.ok(html.includes('data-sb-family="carbon"'));
  assert.ok(html.includes('data-sb-mode="light"'));
});

test('dev client uses live port 5000 and disables backend restart and update actions', () => {
  const h = shellHarness({boot:true,devClient:true});
  h.boot();
  assert.equal(h.requests[0].url,'http://localhost:5000/health');
  h.requests[0].respond(200);
  assert.ok(h.openedWindows[0].loaded.includes('http://localhost:5000/'));
  const file = h.electron.menuTemplates.at(-1).find(item=>item.label==='File');
  assert.equal(file.submenu.find(item=>item.label==='Restart Backend').enabled,false);
  assert.equal(file.submenu.find(item=>item.label==='Check for Updates...').enabled,false);
  assert.ok(!h.logs.some(line=>line.includes('Starting supervisor:')));
});

test('a disconnected launch console does not interrupt startup or file logging', () => {
  for (const asynchronous of [false, true]) {
    const h = shellHarness({boot:true,devClient:true});
    const error = Object.assign(new Error('broken pipe'), {code:'EPIPE'});
    if (asynchronous) h.stdout.emit('error', error);
    else h.stdout.write = () => { throw error; };
    assert.doesNotThrow(() => h.boot());
    h.requests[0].respond(200);
    assert.ok(h.openedWindows[0].loaded.includes('http://localhost:5000/'));
    assert.ok(h.logs.some(line => line.includes('console pipe closed (EPIPE)')));
    assert.ok(h.logs.some(line => line.includes('Electron ready')));
  }
});

test('unexpected console errors are not hidden by broken-pipe handling', () => {
  const h = shellHarness();
  const error = Object.assign(new Error('unexpected output failure'), {code:'EIO'});
  assert.throws(() => h.stdout.emit('error', error), /unexpected output failure/);
});

test('closing the last dev-client window does not hide it to an absent tray', () => {
  const h = shellHarness({devClient:true});
  const win = h.api.openNewWindow();
  let prevented = false;
  win.emit('close', {preventDefault: () => { prevented = true; }});
  assert.equal(prevented, false);
});

test('request timeout and subsequent error schedule just one retry', () => {
  const harness = shellHarness();
  harness.api.waitForServer(() => assert.fail('Not healthy yet'));
  harness.requests[0].timeout();
  assert.equal(harness.timers.size, 1);
  harness.tick(500);
  assert.equal(harness.requests.length, 2);
});

test('cancellation, update and quit stop readiness polling', () => {
  for (const reason of ['cancel', 'update', 'quit']) {
    const harness = shellHarness();
    const cancel = harness.api.waitForServer(() => assert.fail('Cancelled'));
    harness.requests[0].respond(503);
    if (reason === 'cancel') cancel();
    if (reason === 'update') harness.api.setUpdating(true);
    if (reason === 'quit') harness.api.setQuitting(true);
    harness.tick(2000);
    assert.equal(harness.timers.size, 0);
    assert.equal(harness.requests.length, 1);
  }
});

test('failed local navigation shows status then retries the original milestone URL', () => {
  for (const host of ['localhost', '127.0.0.1']) {
    const harness = shellHarness();
    const win = fakeWindow();
    harness.api.configureWindow(win);
    const url = `http://${host}:5151/milestone/2519?tab=tasks`;
    win.webContents.emit('did-fail-load', {}, -102, 'ERR_CONNECTION_REFUSED', url, true);
    assert.match(decodeURIComponent(win.loaded[0]), /Reconnecting to Sales Buddy/);
    assert.equal(win.loaded.length, 1);
    assert.equal(harness.requests.length, 0);
    harness.tick(1000);
    harness.requests[0].respond(503);
    harness.tick(500);
    harness.requests[1].respond(200);
    assert.equal(win.loaded[1], url);
    assert.ok(harness.logs.some((line) => line.includes('code=-102')));
  }
});

test('aborted loads, subframes and non-backend pages are not auto-retried', () => {
  const harness = shellHarness();
  const win = fakeWindow();
  harness.api.configureWindow(win);
  for (const [code, url, main] of [
    [-3, 'http://localhost:5151/', true],
    [-102, 'http://localhost:5151/', false],
    [-102, 'https://example.com/', true],
    [-102, 'http://localhost:5000/', true],
    [-102, 'data:text/html,error', true],
  ]) {
    win.webContents.emit('did-fail-load', {}, code, 'failed', url, main);
  }
  assert.equal(harness.requests.length, 0);
  assert.equal(win.loaded.length, 0);
});

test('closing or navigating a recovering window cancels retries', () => {
  for (const action of ['close', 'navigate']) {
    const harness = shellHarness();
    const win = fakeWindow();
    harness.api.configureWindow(win);
    win.webContents.emit(
      'did-fail-load', {}, -102, 'refused', 'http://localhost:5151/', true
    );
    harness.tick(1000);
    harness.requests[0].respond(503);
    if (action === 'close') {
      win.destroyed = true;
      win.emit('closed');
    } else {
      win.webContents.emit('did-start-navigation', {}, 'http://localhost:5151/notes', false, true);
    }
    harness.tick(1000);
    assert.equal(harness.requests.length, 1);
    assert.equal(harness.timers.size, 0);
  }
});

test('renderer exits and rejected navigation promises are logged', async () => {
  const harness = shellHarness();
  const win = fakeWindow();
  harness.api.configureWindow(win);
  win.webContents.emit('render-process-gone', {}, { reason: 'crashed', exitCode: 7 });
  win.loadURL = () => Promise.reject(new Error('ERR_CONNECTION_REFUSED'));
  harness.api.loadWindowUrl(win, 'http://localhost:5151/');
  await Promise.resolve();
  assert.ok(harness.logs.some((line) => line.includes('reason=crashed, exitCode=7')));
  assert.ok(harness.logs.some((line) => line.includes('Window navigation failed')));
});

test('windows opened during boot load a status screen rather than the backend', () => {
  const harness = shellHarness();
  const win = fakeWindow();
  harness.electron.nextWindow = win;
  harness.api.openNewWindow();
  assert.match(decodeURIComponent(win.loaded[0]), /Starting Sales Buddy/);
  assert.match(win.loaded[0], /^data:text\/html/);
});

test('full boot opens status, survives timeout and loads the app after readiness', () => {
  const harness = shellHarness({ boot: true });
  harness.boot();
  assert.equal(harness.openedWindows.length, 1);
  const win = harness.openedWindows[0];
  assert.match(decodeURIComponent(win.loaded[0]), /Starting Sales Buddy/);
  harness.requests[0].respond(503);
  harness.tick(60000);
  harness.requests[1].respond(503);
  assert.equal(win.loaded.some((url) => url.startsWith('http:')), false);
  assert.match(decodeURIComponent(win.loaded.at(-1)), /longer than usual/);
  harness.tick(2000);
  harness.requests[2].respond(200);
  assert.equal(win.loaded.at(-1), 'http://localhost:5151/');
  assert.equal(harness.openedWindows.length, 1);
  assert.equal(win.shown, true);
});

test('minimized boot stays hidden through slow startup and readiness', () => {
  const harness = shellHarness({ boot: true, minimized: true });
  harness.boot();
  harness.requests[0].respond(503);
  harness.tick(60000);
  harness.requests[1].respond(503);
  assert.equal(harness.openedWindows.length, 0);
  harness.tick(2000);
  harness.requests[2].respond(200);
  assert.equal(harness.openedWindows.length, 0);
});

test('a long-running healthy app can reconnect without restarting its original boot', () => {
  const h = shellHarness({boot: true});
  h.boot();
  h.requests[0].respond(200);
  h.tick(720000 + 1000);
  let ready = false;
  h.api.waitForServer(() => { ready = true; });
  assert.equal(h.requests.length, 2);
  h.requests[1].respond(200);
  assert.equal(ready, true);
  assert.equal(h.children.length, 1);
});

test('opening from tray during minimized boot is honored when the backend is ready', () => {
  const harness = shellHarness({ boot: true, minimized: true });
  harness.boot();
  harness.api.showWindow();
  harness.requests[0].respond(200);
  assert.equal(harness.openedWindows.length, 1);
  assert.equal(harness.openedWindows[0].loaded[0], 'http://localhost:5151/');
});

test('persistent page failure is rate-limited even if backend health succeeds', () => {
  const harness = shellHarness();
  const win = fakeWindow();
  harness.api.configureWindow(win);
  const fail = () => win.webContents.emit(
    'did-fail-load', {}, -102, 'refused', 'http://localhost:5151/', true
  );
  fail();
  harness.tick(1000);
  harness.requests[0].respond(200);
  fail();
  assert.equal(harness.requests.length, 1);
  harness.tick(999);
  assert.equal(harness.requests.length, 1);
  harness.tick(1);
  assert.equal(harness.requests.length, 2);
});

test('tray restore keeps the existing minimized window and native dimensions', () => {
  const harness = shellHarness();
  const win = fakeWindow();
  let restored = 0;
  let focused = 0;
  win.isMinimized = () => true;
  win.restore = () => { restored++; };
  win.focus = () => { focused++; };
  harness.electron.nextWindow = win;
  harness.api.openNewWindow();
  assert.equal(harness.api.ensureWindow(), win);
  assert.equal(restored, 1);
  assert.equal(focused, 1);
  assert.equal(harness.openedWindows.length, 1);
  assert.equal(win.options.width, 1600);
  assert.equal(win.options.height, 1000);
  assert.equal(win.options.minWidth, 900);
  assert.equal(win.options.minHeight, 600);
  assert.equal(win.options.webPreferences.contextIsolation, true);
  assert.equal(win.options.webPreferences.nodeIntegration, false);
  assert.equal(win.options.frame, undefined);
});

test('last window closes to tray while multiple windows close independently', () => {
  const harness = shellHarness();
  const first = fakeWindow();
  first.hide = () => { first.hidden = true; };
  harness.electron.nextWindow = first;
  harness.api.openNewWindow();
  first.emit('ready-to-show');
  first.options.width = 1111;
  first.emit('resize');
  let prevented = 0;
  first.emit('close', { preventDefault() { prevented++; } });
  assert.equal(prevented, 1);
  assert.equal(first.hidden, true);
  assert.equal(JSON.parse(harness.windowWrites.at(-1).value).normalBounds.width, 1111);
  harness.electron.nextWindow = fakeWindow();
  harness.api.openNewWindow();
  first.emit('close', { preventDefault() { prevented++; } });
  assert.equal(prevented, 1);
});

test('update relaunch removes hidden startup flags but preserves other arguments', () => {
  const harness = shellHarness({ minimized: true });
  let args;
  harness.electron.app.relaunch = (config) => { args = config.args; };
  harness.api.relaunchVisible();
  assert.equal(args.includes('--startup'), false);
  assert.equal(args.includes('--minimized'), false);
  assert.equal(args.includes('main.js'), true);
  assert.equal(args.includes('--other'), true);
});

test('reconnect inherits the last real app document theme without exposing a bridge', async () => {
  const harness = shellHarness();
  const win = fakeWindow();
  win.webContents.getURL = () => 'http://localhost:5151/customer/12';
  win.webContents.executeJavaScript = () => Promise.resolve({ mode: 'light', family: 'harbor' });
  harness.api.configureWindow(win);
  win.webContents.emit('did-finish-load');
  await Promise.resolve();
  win.webContents.emit(
    'did-fail-load', {}, -102, 'refused', 'http://localhost:5151/customer/12', true
  );
  assert.match(decodeURIComponent(win.loaded[0]), /data-sb-mode="light"/);
  assert.match(decodeURIComponent(win.loaded[0]), /data-sb-family="harbor"/);
});

test('native update and backend restart controls disable while an update is pending', () => {
  const h = shellHarness();
  h.api.setUpdating(true);
  h.api.buildAppMenu();
  const file = h.electron.menuTemplates.at(-1).find((item) => item.label === 'File');
  for (const label of ['Check for Updates...', 'Restart Backend']) {
    assert.equal(file.submenu.find((item) => item.label === label).enabled, false);
  }
  h.api.createTray();
  let trayMenu = h.electron.menuTemplates.at(-1);
  assert.equal(trayMenu.find((item) => item.label === 'Restart Backend').enabled, false);
  h.api.setUpdating(false);
  h.api.refreshTrayMenu();
  trayMenu = h.electron.menuTemplates.at(-1);
  assert.equal(trayMenu.find((item) => item.label === 'Restart Backend').enabled, true);
});

test('new windows opened during an update show pending source instead of a stopped backend', () => {
  const h = shellHarness();
  h.api.setBooting(false);
  h.api.setUpdating(true);
  const win = h.api.openNewWindow();
  assert.match(decodeURIComponent(win.loaded[0]), /Updating Sales Buddy/);
  assert.equal(win.loaded.some((url) => url.startsWith('http:')), false);
});

test('saved geometry is applied before show and stays separate from the backend preferences', () => {
  const normalBounds = {x: 100, y: 90, width: 1100, height: 750};
  const h = shellHarness({windowState: {version: 1, normalBounds, maximized: true}});
  const win = h.api.openNewWindow();
  assert.equal(win.options.width, normalBounds.width);
  assert.equal(win.options.x, normalBounds.x);
  assert.equal(win.shown, undefined);
  win.show = () => {
    assert.equal(win.maximized, true, 'maximize before making the restored window visible');
    win.shown = true;
  };
  win.emit('ready-to-show');
  assert.equal(win.shown, true);
  h.api.relaunchVisible();
  assert.equal(h.windowWrites.length, 0, 'unchanged saved geometry needs no write');
  assert.ok(h.logs.some(line => line.includes('test-shell-profile') &&
    line.includes('window-state')));
});

test('pending geometry is flushed before relaunch, forced exits, tray close and ordinary quit', () => {
  for (const action of ['relaunch', 'exit', 'close', 'quit']) {
    const h = shellHarness({boot: true, devClient: true});
    h.boot();
    const win = h.openedWindows[0];
    win.emit('ready-to-show');
    win.options.x = 123;
    win.emit('move');
    assert.equal(h.windowWrites.length, 0);
    if (action === 'relaunch') {
      h.electron.app.relaunch = () => assert.equal(h.windowWrites.length, 1);
      h.api.relaunchVisible();
    } else if (action === 'exit') {
      h.electron.app.exit = code => {
        assert.equal(h.windowWrites.length, 1);
        assert.equal(code, 0);
      };
      h.api.exitShell();
    } else if (action === 'close') {
      win.emit('close', {preventDefault() {}});
    } else {
      h.appEvents.emit('before-quit');
    }
    assert.equal(JSON.parse(h.windowWrites[0].value).normalBounds.x, 123);
  }
});

test('hidden startup waits to restore saved geometry until a window is requested', () => {
  const normalBounds = {x: 150, y: 120, width: 1000, height: 700};
  const h = shellHarness({boot: true, minimized: true,
    windowState: {version: 1, normalBounds, maximized: true}});
  h.boot();
  h.requests[0].respond(200);
  assert.equal(h.openedWindows.length, 0);
  h.api.showWindow();
  const win = h.openedWindows[0];
  assert.equal(win.options.width, 1000);
  win.emit('ready-to-show');
  assert.equal(win.maximized, true);
  assert.equal(win.shown, true);
});

test('managed startup rejects HTTP success without current worker identity', () => {
  const h = shellHarness();
  let ready = false;
  h.api.waitForServer(() => { ready = true; });
  h.requests[0].respond(200, {status: 'ok'});
  assert.equal(ready, false);
  h.tick(500);
  h.requests[1].respond(200);
  assert.equal(ready, true);
});

test('startup deadline surfaces failure instead of continuing indefinitely', () => {
  const h = shellHarness();
  let failure;
  h.api.waitForServer(() => assert.fail('Unavailable'), () => {}, reason => { failure = reason; });
  h.requests[0].respond(503);
  h.tick(720000);
  assert.equal(failure, 'deadline');
  assert.equal(h.timers.size, 0);
  assert.equal(h.requests.length, 1);
});

test('supervisor exit is terminal and never schedules a shell respawn', () => {
  const h = shellHarness({boot: true});
  h.boot();
  h.children[0].child.emit('exit', 1, null);
  assert.match(decodeURIComponent(h.openedWindows[0].loaded.at(-1)), /data-state="failed"/);
  h.tick(720000);
  assert.equal(h.children.length, 1);
});

test('update pipeline fetches then freezes recovery before applying and relaunching', async () => {
  const h = shellHarness();
  await h.api.runUpdate('test');
  assert.deepEqual(h.commands.map(command => command.args.includes('fetch') ? 'fetch'
    : command.args.includes('prepare') ? 'prepare'
      : command.args.includes('apply') ? 'apply' : 'unexpected'), ['fetch', 'prepare', 'apply']);
  assert.equal(h.relaunches.length, 1);
  assert.equal(h.commands.some(command => command.args.includes('--hard')), false);
  assert.ok(h.commands.every(command => command.options.timeout > 0));
});

test('teardown failure prevents fetch, snapshot, apply and relaunch', async () => {
  const h = shellHarness({teardownFree: false});
  await h.api.runUpdate('test');
  assert.equal(h.commands.length, 0);
  assert.equal(h.relaunches.length, 0);
  assert.match(decodeURIComponent(h.openedWindows[0].loaded.at(-1)), /data-state="failed"/);
});

test('apply failure leaves the failure UI without restarting or claiming recovery', async () => {
  const h = shellHarness({commandFailure: 'apply'});
  await h.api.runUpdate('test');
  assert.equal(h.relaunches.length, 0);
  assert.equal(h.children.length, 0);
  assert.equal(h.errors.length, 0);
  const html = decodeURIComponent(h.openedWindows[0].loaded.at(-1));
  assert.match(html, /data-state="failed"/);
  assert.ok(!html.includes('recovers the current version'));
});

test('failure IPC rejects arbitrary documents, subframes, actions and all dev-client requests',
  async () => {
    const h = shellHarness({commandFailure: 'apply'});
    await h.api.runUpdate('test');
    const win = h.openedWindows[0];
    const handler = win.ipcHandlers.get('shell:status-action');
    const event = {sender: win.webContents, senderFrame: win.webContents.mainFrame};
    for (const [request, action] of [[event, 'arbitrary'],
      [{...event, senderFrame: {url: win._sbStatusUrl}}, 'export'],
      [{...event, sender: {}}, 'retry']]) {
      assert.equal((await handler(request, action)).ok, false);
    }
    const original = win.webContents.mainFrame.url;
    win.webContents.mainFrame.url = 'http://localhost:5151/';
    assert.equal((await handler(event, 'export')).ok, false);
    win.webContents.mainFrame.url = original;
    assert.equal((await handler(event, 'export')).ok, true);
    assert.equal(h.dialogs.length, 0, 'export does not ask to restore or touch Flask');
    const dev = shellHarness({devClient: true});
    const devWindow = dev.api.openNewWindow();
    assert.equal((await devWindow.ipcHandlers.get('shell:status-action')({
      sender: devWindow.webContents, senderFrame: devWindow.webContents.mainFrame,
    }, 'retry')).ok, false);
  });

test('pending startup acceptance precedes opening the backend', async () => {
  const h = shellHarness({boot: true, upgrade: {phase: 'starting', recovery_ready: true}});
  h.boot();
  const win = h.openedWindows[0];
  h.requests[0].respond(200);
  assert.equal(win.loaded.some(url => url.startsWith('http:')), false);
  assert.ok(h.commands.some(command => command.args.includes('accept')));
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(win.loaded.at(-1), 'http://localhost:5151/');
});

test('failed acceptance never opens the backend or reports a successful update', async () => {
  const h = shellHarness({boot: true, upgrade: {phase: 'starting', recovery_ready: true},
    commandFailure: 'accept'});
  h.boot();
  h.requests[0].respond(200);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(h.openedWindows[0].loaded.some(url => url.startsWith('http:')), false);
  assert.match(decodeURIComponent(h.openedWindows[0].loaded.at(-1)), /data-state="failed"/);
});

test('shell build failure persists pending shell failure without relaunch or acceptance', async () => {
  const h = shellHarness({commandFailure: '-SkipSign'});
  await h.api.runUpdate('test', true);
  const actions = h.commands.map(command => command.args);
  assert.ok(actions.find(args => args.includes('prepare')).includes('--shell-rebuild'));
  assert.ok(actions.some(args => args.includes('fail') && args.includes('shell')));
  assert.equal(actions.some(args => args.includes('accept')), false);
  assert.equal(h.relaunches.length, 0);
  assert.match(decodeURIComponent(h.openedWindows[0].loaded.at(-1)), /data-state="failed"/);
});

test('shell rebuild helper fallback launches through the independent desktop coordinator', async () => {
  const h = shellHarness();
  await h.api.runUpdate('test', true);
  assert.equal(h.helperWrites.length, 1);
  const helper = h.helperWrites[0].value;
  assert.match(helper, /-RecoveryOwnedByCaller/);
  assert.match(helper, /launch-recovery\r\nif errorlevel 1 .*--repo .*launch-desktop\r\n/);
  assert.doesNotMatch(helper, /if errorlevel 1 start /i);
});

test('retry of a complete pending snapshot never prepares a replacement recovery point', async () => {
  const h = shellHarness({upgrade: {phase: 'failed', recovery_ready: true}});
  await h.api.retryStartup();
  assert.ok(h.commands.some(command => command.args.includes('apply')));
  assert.equal(h.commands.some(command => command.args.includes('prepare')), false);
  assert.equal(h.children.length, 1);
  assert.equal(h.requests.length, 1);
});

test('retry of phase starting only restarts backend and waits for acceptance', async () => {
  const h = shellHarness({upgrade: {phase: 'starting', recovery_ready: true}});
  await h.api.retryStartup();
  assert.equal(h.commands.length, 0);
  assert.equal(h.children.length, 1);
  assert.equal(h.requests.length, 1);
  h.requests[0].respond(200);
  assert.ok(h.commands.some(command => command.args.includes('accept')));
});

test('frozen recovery verification failure keeps Electron alive without confirmation or handoff',
  async () => {
    const {createHash} = require('node:crypto');
    const repo = path.resolve(__dirname, '..', '..');
    const upgrade = {phase: 'failed', recovery_ready: true, changes_started: true,
      attempt_id: 'a'.repeat(32),
      previous_commit: 'b'.repeat(40), repo, database: path.join(repo, 'data', 'salesbuddy.db'),
      runner_sha256: createHash('sha256').update('frozen recovery runner').digest('hex'),
      backup_helper_sha256: createHash('sha256').update('frozen backup helper').digest('hex')};
    const h = shellHarness({upgrade, commandFailure: 'verify'});
    const win = h.api.openNewWindow();
    const result = await h.api.statusAction('recover');
    assert.equal(result.ok, false);
    assert.match(result.message, /could not pass verification/);
    assert.equal(h.commands.length, 1);
    assert.ok(h.commands[0].args.includes('verify'));
    assert.match(h.commands[0].file, /upgrade-recovery[\\/]a{32}[\\/]runtime/);
    assert.equal(h.dialogs.length, 0);
    assert.equal(h.exits.length, 0);
    assert.equal(h.children.length, 0);
    assert.equal(win.isDestroyed(), false);
  });

for (const phase of ['failed', 'restored']) {
  test(`automatic ${phase} recovery blocks boot and hands off without a confirmation dialog`,
    async () => {
      const {createHash} = require('node:crypto');
      const repo = path.resolve(__dirname, '..', '..');
      const upgrade = {phase, changes_started: true, recovery_ready: true,
        notification_pending: phase === 'restored', attempt_id: 'a'.repeat(32),
        previous_commit: 'b'.repeat(40), repo, database: path.join(repo, 'data', 'salesbuddy.db'),
        runner_sha256: createHash('sha256').update('frozen recovery runner').digest('hex'),
        backup_helper_sha256: createHash('sha256').update('frozen backup helper').digest('hex')};
      const h = shellHarness({boot: true, upgrade});
      h.boot();
      await new Promise(setImmediate);
      assert.equal(h.dialogs.length, 0);
      assert.equal(h.requests.length, 0, 'even a healthy old backend cannot release the gate');
      assert.equal(h.children.length, 1);
      assert.equal(h.children[0].file, 'cmd.exe');
      assert.ok(h.commands.every(command => command.args.includes('verify')));
      assert.ok(h.openedWindows.every(win => !win.loaded.some(url => url.startsWith('http:'))));
      const status = decodeURIComponent(h.openedWindows.at(-1).loaded.at(-1));
      assert.ok(status.includes('data-state="recovering"'));
      assert.ok(status.includes('id="failure-actions" hidden'));
      assert.match(status, /no action is needed/);
      h.api.showFailure('startup');
      const repeated = decodeURIComponent(h.openedWindows.at(-1).loaded.at(-1));
      assert.ok(repeated.includes('data-state="recovering"'),
        'late startup callbacks must not replace recovery progress with action buttons');
      h.children[0].child.emit('exit', 0);
      await new Promise(setImmediate);
      assert.deepEqual(h.exits, [0]);
    });
}
