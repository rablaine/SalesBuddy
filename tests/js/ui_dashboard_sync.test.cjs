const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const {createCoordinator} = require('../../static/js/ui-dashboard-sync.js');

const state = {operation: 'fabric', name: 'Fabric', error: null};
const job = {id: 12, status: 'running', progress: {percent: 37}};
const response = data => ({ok: true, json: async () => ({success: true, ...data})});

test('widgets share one admission and observer and both receive actual progress', async () => {
    let starts = 0;
    let observes = 0;
    let finish;
    let publish;
    const coordinator = createCoordinator(async (url, options) => {
        starts++;
        assert.match(url, /widgets\/fabric.expansion\/sync$/);
        assert.deepEqual(JSON.parse(options.body), {retry: false});
        return response({ready: false, sync: {...state, job}});
    }, {observe: async (id, {onProgress}) => {
        observes++;
        assert.equal(id, job.id);
        publish = onProgress;
        await new Promise(resolve => { finish = resolve; });
        return {...job, status: 'done'};
    }});
    const a = [];
    const b = [];
    const one = coordinator.follow(state, 'fabric.expansion', false, value => a.push(value));
    const two = coordinator.follow(state, 'fabric.trial_conversion', false, value => b.push(value));
    await new Promise(resolve => setImmediate(resolve));
    publish(job);
    assert.deepEqual(a, [job]);
    assert.deepEqual(b, [job]);
    finish();
    await Promise.all([one, two]);
    assert.equal(starts, 1);
    assert.equal(observes, 1);
});

test('late subscribers get the current progress without another admission', async () => {
    let finish;
    const coordinator = createCoordinator(async () => response({
        ready: false, sync: {...state, job},
    }), {observe: async (_, {onProgress}) => {
        onProgress(job);
        await new Promise(resolve => { finish = resolve; });
        return {...job, status: 'done'};
    }});
    const a = coordinator.follow(state, 'fabric.expansion', false, () => {});
    await new Promise(resolve => setImmediate(resolve));
    const received = [];
    const b = coordinator.follow(state, 'fabric.trial_conversion', false,
        value => received.push(value));
    assert.deepEqual(received, [job]);
    finish();
    await Promise.all([a, b]);
});

test('already ready or newly input-blocked states redraw without observing', async () => {
    for (const ready of [false, true]) {
        const coordinator = createCoordinator(async () => response({
            ready, sync: null, requirements: !ready,
        }), {observe() { assert.fail('No sync required'); }});
        await coordinator.follow(state, 'fabric.expansion', false, () => {});
    }
});

test('failures are explicit, cleared from the coordinator, and retry is user initiated', async () => {
    let attempts = 0;
    const coordinator = createCoordinator(async (_, options) => {
        attempts++;
        assert.equal(JSON.parse(options.body).retry, attempts === 2);
        return response({ready: false, sync: {...state, job}});
    }, {observe: async () => ({...job, status: 'failed', error: 'VPN unavailable'})});
    await assert.rejects(coordinator.follow(state, 'fabric.expansion', false, () => {}),
        /VPN unavailable/);
    assert.equal(attempts, 1);
    await assert.rejects(coordinator.follow(state, 'fabric.expansion', true, () => {}),
        /VPN unavailable/);
    assert.equal(attempts, 2);
});

test('invalid responses, missing identities, and admission failures never look successful', async () => {
    for (const data of [
        {ready: false, sync: {...state, error: 'Import accounts first'}},
        {ready: false, sync: {...state, job: {}}},
        {ready: false, sync: {...state, job: {id: -1}}},
        {},
    ]) {
        const coordinator = createCoordinator(async () => response(data), {
            observe() { assert.fail('No valid job'); },
        });
        await assert.rejects(coordinator.follow(state, 'fabric.expansion', false, () => {}));
    }
    const coordinator = createCoordinator(async () => ({
        ok: false, json: async () => ({error: 'Admission unavailable'}),
    }), {});
    await assert.rejects(coordinator.follow(state, 'fabric.expansion', false, () => {}),
        /Admission unavailable/);
});

test('leaving the page stops observation without cancelling the durable job', async () => {
    let stopped = false;
    const requests = [];
    const coordinator = createCoordinator(async (url) => {
        requests.push(url);
        return response({ready: false, sync: {...state, job}});
    }, {observe: async (_, {signal}) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => {
            stopped = true;
            reject(new DOMException('Observation aborted', 'AbortError'));
        }, {once: true});
    })});
    const pending = coordinator.follow(state, 'fabric.expansion', false, () => {});
    await new Promise(resolve => setImmediate(resolve));
    coordinator.stop();
    await assert.rejects(pending, {name: 'AbortError'});
    assert.equal(stopped, true);
    assert.equal(requests.length, 1);
    assert.ok(requests.every(url => !url.includes('cancel')));
});

function browser({
    error = null, editor = false, library = false, failRefresh = false, deferRefresh = false,
    failSync = false,
} = {}) {
    const events = {};
    const progress = {hidden: false, removeAttribute() { this.indeterminate = true; }};
    const button = {hidden: true,
        addEventListener(_, callback) { this.click = callback; },
        removeEventListener() { delete this.click; }};
    const actions = {hidden: !error};
    const message = {classList: {add() {}, remove() {}}};
    const label = {};
    const body = {
        isConnected: true, dataset: {
            widgetSync: JSON.stringify({...state, error}),
            syncPreview: JSON.stringify({
                key: 'fabric.expansion', variant: 'detailed', width: 6, instance_id: 11,
            }),
        },
        querySelector(selector) {
            return {'[data-sync-message]': message, progress,
                '[data-widget-sync-retry]': button, '[data-widget-sync-actions]': actions,
                '[data-sync-label]': label}[selector];
        },
        closest(selector) {
            if (selector === '.db-preview-widget') return library ? {} : null;
            return tile;
        },
        parentElement: {},
        insertAdjacentHTML(_, html) { this.html = html; },
        remove() { this.isConnected = false; },
    };
    const tile = {dataset: {widgetId: '11', widgetFilters: '{}'}};
    const calls = [];
    let finish;
    let refreshed;
    let publish;
    let scope = {live: {seller_ids: [7]}};
    const window = {
        document: {
            readyState: 'complete',
            querySelector: () => ({}),
            querySelectorAll: () => [body],
            getElementById: () => null,
        },
        fetch: async (url, options) => {
            calls.push({url, options});
            if (options?.method === 'POST') return response({ready: false, sync: {...state, job}});
            if (failRefresh) throw new Error('Refresh unavailable');
            if (deferRefresh) await new Promise(resolve => { refreshed = resolve; });
            return response({html: '<div>Real synced content</div>'});
        },
        SBJobs: {
            message: current => current.progress.message || 'Sync running',
            observe: async (_, {onProgress}) => {
                publish = onProgress;
                await new Promise(resolve => { finish = resolve; });
                return {...job, status: failSync ? 'failed' : 'done',
                    ...(failSync ? {error: 'VPN unavailable'} : {})};
            },
        },
        SBDashboardFilters: {context: () => scope},
        addEventListener(name, handler) { events[name] = handler; },
        dispatchEvent(event) { events[event.type] = event.detail; },
        ...(editor ? {SBDashboardEditor: {
            refreshWidget: async id => { calls.push({editor: id}); },
        }} : {}),
    };
    vm.runInNewContext(fs.readFileSync('static/js/ui-dashboard-sync.js', 'utf8'), {
        window, document: window.document, AbortController, URLSearchParams,
        MutationObserver: class {observe() {} disconnect() {}},
        CustomEvent: class {constructor(type, options) { this.type = type; this.detail = options.detail; }},
        console: {error() {}},
    });
    return {calls, body, button, actions, progress, message, events,
        publish: current => publish(current), finish: () => finish(),
        refreshed: () => refreshed(),
        setScope(value) { scope = value; }};
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test('inline progress follows measured values then refreshes the same instance and filters', async () => {
    const page = browser();
    await tick();
    assert.equal(page.actions.hidden, true);
    page.publish({...job, progress: {percent: 37, message: 'Importing capacities'}});
    assert.equal(page.progress.value, 37);
    assert.equal(page.message.textContent, 'Importing capacities');
    page.publish({...job, progress: {}});
    assert.equal(page.progress.indeterminate, true);
    page.finish();
    await tick();
    assert.equal(page.body.isConnected, false);
    assert.match(page.calls[1].url, /instance_id=11/);
    assert.match(decodeURIComponent(page.calls[1].url), /"seller_ids":\[7\]/);
});

test('failed states wait for Retry and deleted widgets ignore completed requests', async () => {
    const page = browser({error: 'VPN unavailable'});
    assert.equal(page.calls.length, 0);
    assert.equal(page.button.hidden, false);
    assert.equal(page.actions.hidden, false);
    assert.equal(page.message.textContent, 'VPN unavailable');
    page.button.click();
    assert.equal(page.actions.hidden, true);
    await tick();
    assert.equal(JSON.parse(page.calls[0].options.body).retry, true);
    page.body.isConnected = false;
    page.finish();
    await tick();
    assert.equal(page.calls.length, 1);
});

test('queued and running syncs show actions only after a failure and hide them on retry', async () => {
    const page = browser({failSync: true});
    await tick();
    for (const status of ['pending', 'running']) {
        page.publish({...job, status});
        assert.equal(page.actions.hidden, true);
        assert.equal(page.button.hidden, true);
        assert.equal(page.progress.hidden, false);
    }
    page.finish();
    await tick();
    assert.equal(page.actions.hidden, false);
    assert.equal(page.button.hidden, false);
    assert.equal(page.progress.hidden, true);
    assert.equal(page.message.textContent, 'VPN unavailable');
    page.button.click();
    assert.equal(page.actions.hidden, true);
    assert.equal(page.button.hidden, true);
    await tick();
    page.finish();
    await tick();
    assert.equal(page.actions.hidden, false);
});

test('editor refresh uses its stale-safe loader without publishing or changing layout', async () => {
    const page = browser({editor: true});
    await tick();
    page.finish();
    await tick();
    assert.deepEqual(page.calls[1], {editor: 11});
    assert.equal(page.calls.length, 2);
});

test('library completion uses its generation-safe preview loader and remeasures content', async () => {
    const page = browser({library: true});
    await tick();
    page.finish();
    await tick();
    assert.equal(page.events['sb:widget-sync-ready'].body, page.body);
    assert.equal(page.calls.length, 1);
});

test('failed redraw retries loading without admitting the successful source again', async () => {
    const page = browser({failRefresh: true});
    await tick();
    page.finish();
    await tick();
    assert.equal(page.button.textContent, 'Retry loading widget');
    assert.equal(page.actions.hidden, false);
    page.button.click();
    assert.equal(page.actions.hidden, true);
    await tick();
    assert.equal(page.calls.filter(call => call.options?.method === 'POST').length, 1);
});

test('late viewer responses cannot replace a newly selected filter scope', async () => {
    const page = browser({deferRefresh: true});
    await tick();
    page.finish();
    await tick();
    page.setScope({live: {seller_ids: [9]}});
    page.refreshed();
    await tick();
    assert.equal(page.body.isConnected, true);
    assert.equal(page.body.html, undefined);
});
