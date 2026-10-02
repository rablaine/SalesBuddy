const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {TextDecoder} = require('node:util');

const template = fs.readFileSync(path.join(__dirname, '../../templates/milestone_tracker.html'), 'utf8');
const script = template.slice(
    template.indexOf('function syncMilestones()'),
    template.indexOf('// --- Show sync results')
);

async function runSync(events, status = 200, contentType = 'text/event-stream', split = false) {
    const elements = {};
    const removed = [];
    let reloads = 0;
    let vpnBanners = 0;
    for (const id of [
        'syncBtn', 'syncBtnEmpty', 'syncAlert', 'syncMessage', 'syncProgressWrap',
        'syncProgressBar', 'syncDetail', 'syncBrainrotToggle',
    ]) {
        const classes = new Set(['progress-bar-animated']);
        elements[id] = {
            disabled: false, innerHTML: '', textContent: '', style: {},
            classList: {
                add: value => classes.add(value), remove: value => classes.delete(value),
                contains: value => classes.has(value),
            },
            setAttribute() {},
        };
    }
    const chunks = split ? Array.from(events) : [events];
    const context = {
        document: {getElementById: id => elements[id]},
        window: {
            addEventListener() {},
            removeEventListener: name => removed.push(name),
            location: {reload: () => reloads++},
            showVpnBanner: () => vpnBanners++,
        },
        sessionStorage: {setItem() {}},
        TextDecoder,
        fetch: async () => ({
            ok: status < 400, status,
            headers: {get: () => contentType},
            body: {getReader: () => ({
                read: async () => chunks.length
                    ? {done: false, value: Buffer.from(chunks.shift())} : {done: true},
            })},
        }),
    };
    vm.runInNewContext(script, context);
    context.syncMilestones();
    await new Promise(resolve => setImmediate(resolve));
    return {elements, removed, reloads, vpnBanners};
}

const start = 'event: start\ndata: {"total": 1}\n\n';
const progress = 'event: progress\ndata: {"current":19,"total":29,"status":"fetching","customer":"Tasks batch 19/29","progress":89}\n\n';

test('explicit backend error stops the spinner and restores controls', async () => {
    const {elements, removed, reloads} = await runSync(start + progress +
        'event: error\ndata: {"message":"Milestone sync failed. Review the server log."}\n\n', 200, 'text/event-stream', true);
    assert.match(elements.syncMessage.textContent, /Milestone sync failed/);
    assert.equal(elements.syncAlert.className, 'alert alert-danger mb-4');
    assert.equal(elements.syncBtn.disabled, false);
    assert.equal(elements.syncProgressBar.classList.contains('progress-bar-animated'), false);
    assert.ok(removed.includes('beforeunload'));
    assert.equal(reloads, 0);
});

test('early EOF never leaves the UI appearing to sync forever', async () => {
    const {elements, reloads} = await runSync(start + progress);
    assert.match(elements.syncMessage.textContent, /ended before completion/);
    assert.equal(elements.syncBtn.disabled, false);
    assert.equal(elements.syncProgressBar.classList.contains('progress-bar-animated'), false);
    assert.equal(reloads, 0);
});

test('HTTP and non-stream failures are visible instead of ignored', async () => {
    for (const status of [400, 500]) {
        const {elements} = await runSync('<!doctype html>', status, 'text/html');
        assert.match(elements.syncMessage.textContent, new RegExp(`HTTP ${status}`));
        assert.equal(elements.syncBtn.disabled, false);
    }
});

test('successful completion still reloads with results', async () => {
    const {elements, reloads} = await runSync(start +
        'event: complete\ndata: {"success":true,"total":1,"synced":1,"duration":1}\n\n');
    assert.equal(reloads, 1);
    assert.equal(elements.syncProgressBar.textContent, '100%');
    assert.equal(elements.syncBtn.disabled, false);
});

test('VPN terminal event is not overwritten by an early EOF error', async () => {
    const {elements, vpnBanners} = await runSync(start +
        'event: vpn_blocked\ndata: {"message":"Connect to VPN"}\n\n');
    assert.match(elements.syncMessage.innerHTML, /VPN Required/);
    assert.equal(elements.syncMessage.textContent, '');
    assert.equal(vpnBanners, 1);
    assert.equal(elements.syncBtn.disabled, false);
});
