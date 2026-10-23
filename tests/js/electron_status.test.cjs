const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { statusScreenUrl } = require('../../electron/status-screen');

test('bundled status document is self-contained and all eight palettes are selectable', () => {
  for (const mode of ['dark', 'light']) {
    for (const family of ['graphite', 'midnight', 'harbor', 'carbon']) {
      const html = decodeURIComponent(statusScreenUrl('Starting Sales Buddy', 'Waiting', {
        mode, family,
      }));
      assert.ok(html.includes(`data-sb-mode="${mode}"`));
      assert.ok(html.includes(`data-sb-family="${family}"`));
      assert.ok(html.includes('--sb-ui-surface'));
      assert.ok(html.includes('role="progressbar"'));
      assert.ok(!html.includes('<script src='));
      assert.ok(!html.includes('<link rel="stylesheet"'));
      assert.ok(!html.includes('../static'));
      assert.ok(!html.includes('aria-valuenow'));
    }
  }
});

test('status phases preserve actual messages and escape dynamic markup', () => {
  for (const [title, subtitle, state] of [
    ['Starting Sales Buddy', 'Waiting for readiness', 'starting'],
    ['Starting Sales Buddy', 'Startup is taking longer than usual.', 'slow'],
    ['Reconnecting to Sales Buddy', 'Retrying automatically', 'reconnecting'],
    ['Building the desktop app', 'Keep the app open', 'building'],
    ['Installing the update', 'Updating dependencies', 'updating'],
  ]) {
    const html = decodeURIComponent(statusScreenUrl(title, subtitle));
    assert.ok(html.includes(`data-state="${state}"`));
    assert.ok(html.includes(`${subtitle}</p>`));
  }
  const escaped = decodeURIComponent(statusScreenUrl('<img src=x>', '<script>bad()</script>'));
  assert.ok(escaped.includes('&lt;img src=x&gt;'));
  assert.ok(escaped.includes('&lt;script&gt;bad()&lt;/script&gt;'));
  assert.ok(!escaped.includes('<script>bad()'));
});

test('build manifests include status assets and refresh the approved token snapshot', () => {
  const root = path.join(__dirname, '../..');
  const manifest = require('../../electron/package.json');
  for (const file of ['status-screen.js', 'status.html', 'status.css', 'status.js', 'theme-tokens.css']) {
    assert.ok(manifest.build.files.includes(file));
    assert.ok(fs.existsSync(path.join(root, 'electron', file)));
  }
  assert.equal(fs.readFileSync(path.join(root, 'electron/theme-tokens.css'), 'utf8'),
    fs.readFileSync(path.join(root, 'static/css/ui-tokens.css'), 'utf8'));
  assert.match(fs.readFileSync(path.join(root, 'electron/build.ps1'), 'utf8'),
    /Copy-Item[\s\S]*theme-tokens\.css/);
});

test('automatic recovery is a progress state, not an actionable failure', () => {
  const html = decodeURIComponent(statusScreenUrl('Restoring your previous version',
    'Please wait; no action is needed.', {recovering: true, managed: true}));
  assert.ok(html.includes('data-state="recovering"'));
  assert.ok(html.includes('id="failure-actions" hidden'));
  assert.match(html, /Please wait; no action is needed/);
});
