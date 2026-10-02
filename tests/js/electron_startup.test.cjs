const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function shellHarness({ boot = false, minimized = false } = {}) {
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const requests = [];
  const logs = [];
  const openedWindows = [];
  let bootCallback;
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'electron', 'main.js'), 'utf8');
  const electron = {
    app: {
      isPackaged: false, requestSingleInstanceLock: () => boot, quit() {},
      on() {}, setAppUserModelId() {},
      whenReady: () => ({ then: (callback) => { bootCallback = callback; } }),
    },
    BrowserWindow: function () {
      const win = electron.nextWindow || fakeWindow();
      openedWindows.push(win);
      return win;
    },
    Menu: { buildFromTemplate: () => ({}), setApplicationMenu() {} },
    Tray: class extends EventEmitter { setToolTip() {} setContextMenu() {} },
  };
  electron.BrowserWindow.getFocusedWindow = () => null;
  const context = vm.createContext({
    require(name) {
      if (name === 'electron') return electron;
      if (name === './package.json') return { version: 'test' };
      if (name === 'fs') return {
        readFileSync: () => '', mkdirSync() {}, existsSync: () => true,
        openSync: () => 1, unlinkSync() {},
        appendFileSync: (_file, line) => logs.push(line),
      };
      if (name === 'child_process') return { spawn: () => new EventEmitter() };
      if (name === 'http') return {
        get(url, callback) {
          const request = new EventEmitter();
          request.url = url;
          request.respond = (statusCode) => callback({ statusCode, resume() {} });
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
      env: {}, platform: 'win32', argv: minimized ? ['--minimized'] : [],
      stdout: { write() {} },
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
    globalThis.api = {
      waitForServer, configureWindow, loadWindowUrl, openNewWindow, showWindow,
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
    api: context.api, requests, logs, timers, tick, electron, openedWindows,
    boot: () => bootCallback(),
  };
}

function fakeWindow() {
  const win = new EventEmitter();
  win.webContents = new EventEmitter();
  win.webContents.setWindowOpenHandler = () => {};
  win.loaded = [];
  win.destroyed = false;
  win.isDestroyed = () => win.destroyed;
  win.isMinimized = () => false;
  win.show = () => { win.shown = true; };
  win.focus = () => {};
  win.loadURL = (url) => { win.loaded.push(url); return Promise.resolve(); };
  return win;
}

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
