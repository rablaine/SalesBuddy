const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', 'ui-hvas.js'), 'utf8',
);

function harness({
    empty = false, startError = null, withTask = false, completionRequest = null,
    reopening = false, categoryChange = false,
} = {}) {
    const handlers = {};
    const element = name => ({
        hidden: false, disabled: false, attributes: {},
        addEventListener(event, listener) { handlers[`${name}:${event}`] = listener; },
        setAttribute(name, value) { this.attributes[name] = value; },
        focus() { state.focused = name; },
    });
    const table = element('table');
    table.querySelectorAll = () => [
        {dataset: {hvaSort: 'created'}}, {dataset: {hvaSort: 'due'}},
    ];
    const progressRoot = {};
    const elements = {
        '#hvaTable': empty ? null : table,
        '#hvaPageSize': element('size'),
        '#hvaRefresh': element('refresh'),
        '[data-sb-sync-progress]': progressRoot,
    };
    const state = {starts: 0, reloads: 0, errors: [], navigations: [], requests: [], timers: []};
    const controls = {
        '[data-hva-toggle]': element('toggle'),
        '[data-hva-category]': {...element('category'), value: '861980004'},
        '[data-hva-result]': element('result'),
    };
    controls['[data-hva-result]'].hidden = true;
    const action = element('action');
    action.dataset = {
        action: categoryChange ? 'category' : (reopening ? 'reopen' : 'complete'),
        actionUrl: `/api/reports/hvas/17/${categoryChange ? 'category' :
            (reopening ? 'reopen' : 'complete')}`,
    };
    action.querySelector = selector => controls[selector];
    action.closest = () => ({querySelector: selector => controls[selector]});
    const window = {
        location: {
            href: 'https://salesbuddy.test/reports/hvas?status=completed&search=followup' +
                '&customer_id=12&date_from=2026-10-01&sort=created&direction=desc' +
                '&page=3&per_page=25',
            assign(url) { state.navigations.push(new URL(url)); },
            reload() { state.reloads++; },
        },
        SBSyncProgress: {
            create(root, options) {
                assert.equal(root, progressRoot);
                state.options = options;
                return {start() {
                    state.starts++;
                    return startError ? Promise.reject(startError) : Promise.resolve();
                }};
            },
        },
    };
    vm.runInNewContext(source, {
        window, URL,
        document: {querySelector: () => ({
            querySelector: selector => elements[selector],
            querySelectorAll: () => withTask ? [action] : [],
        })},
        console: {error: (...args) => state.errors.push(args)},
        fetch: (...args) => {
            state.requests.push(args);
            return completionRequest ? completionRequest(...args) :
                Promise.resolve({ok: true, json: async () => ({success: true})});
        },
        setTimeout(callback, delay) { state.timers.push({callback, delay}); },
    });
    return {state, handlers, controls, action};
}

test('sorting navigates globally, resets the page and retains every active filter', () => {
    const {state, handlers} = harness();
    handlers['table:sb:table-sort']({detail: {column: 1, direction: 'ascending'}});
    const params = state.navigations[0].searchParams;
    assert.equal(params.get('sort'), 'due');
    assert.equal(params.get('direction'), 'asc');
    assert.equal(params.get('page'), '1');
    assert.equal(params.get('status'), 'completed');
    assert.equal(params.get('search'), 'followup');
    assert.equal(params.get('customer_id'), '12');
    assert.equal(params.get('date_from'), '2026-10-01');
    assert.equal(params.get('per_page'), '25');
    handlers['table:sb:table-sort']({detail: {column: 0, direction: 'descending'}});
    assert.equal(state.navigations[1].searchParams.get('direction'), 'desc');
});

test('page-size navigation also works in an empty filtered inventory', () => {
    const {state, handlers} = harness({empty: true});
    handlers['size:change']({target: {value: '100'}});
    const params = state.navigations[0].searchParams;
    assert.equal(params.get('per_page'), '100');
    assert.equal(params.get('page'), '1');
    assert.equal(params.get('status'), 'completed');
    assert.equal(params.get('sort'), 'created');
    assert.equal(params.get('direction'), 'desc');
});

test('arrival does not launch work or reload; explicit refresh delegates to shared progress', () => {
    const {state, handlers} = harness();
    assert.equal(state.starts, 0);
    assert.equal(state.reloads, 0);
    handlers['refresh:click']();
    assert.equal(state.starts, 1);
    assert.equal(state.reloads, 0);
    state.options.onComplete();
    assert.equal(state.reloads, 1);
    assert.equal(state.navigations.length, 0);
});

test('shared-progress refresh failures remain observable and never trigger a reload', async () => {
    const {state, handlers} = harness({startError: new Error('Sync failed')});
    handlers['refresh:click']();
    await Promise.resolve();
    assert.equal(state.errors.length, 1);
    assert.equal(state.errors[0][1].message, 'Sync failed');
    assert.equal(state.reloads, 0);
});

test('other pages are untouched', () => {
    vm.runInNewContext(source, {document: {querySelector: () => null}});
});

for (const reopening of [false, true]) {
test(`one-click ${reopening ? 'reopen' : 'complete'} guards duplicate writes and reloads on success`, async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const {state, handlers, controls, action} = harness({
        withTask: true, completionRequest: () => response, reopening,
    });
    const running = handlers['toggle:click']();
    await handlers['toggle:click']();
    assert.equal(state.requests.length, 1);
    assert.equal(state.requests[0][0], `/api/reports/hvas/17/${reopening ? 'reopen' : 'complete'}`);
    assert.equal(state.requests[0][1].method, 'POST');
    assert.equal(controls['[data-hva-toggle]'].disabled, true);
    assert.equal(action.attributes['aria-busy'], 'true');
    assert.match(controls['[data-hva-result]'].textContent, reopening ? /Reopening/ : /Completing/);
    resolve({ok: true, json: async () => ({success: true})});
    await running;
    assert.equal(action.attributes['aria-busy'], 'false');
    assert.equal(state.reloads, 1);
    assert.equal(state.timers.length, 0);
    await handlers['toggle:click']();
    assert.equal(state.requests.length, 1);
    assert.equal(state.navigations.length, 0);
});
}

test('failed completion displays the server error and offers an explicit retry', async () => {
    let failed = true;
    const {state, handlers, controls} = harness({
        withTask: true,
        completionRequest: async () => ({
            ok: !failed, json: async () => failed ?
                {success: false, error: 'Access denied'} : {success: true},
        }),
    });
    await handlers['toggle:click']();
    assert.equal(controls['[data-hva-result]'].attributes.role, 'alert');
    assert.equal(controls['[data-hva-result]'].textContent, 'Access denied');
    assert.equal(controls['[data-hva-toggle]'].disabled, false);
    assert.equal(state.focused, 'toggle');
    assert.equal(state.errors.length, 1);
    assert.equal(state.timers.length, 0);
    assert.equal(state.reloads, 0);
    failed = false;
    await handlers['toggle:click']();
    assert.equal(state.requests.length, 2);
    assert.equal(controls['[data-hva-result]'].attributes.role, 'status');
    assert.equal(state.reloads, 1);
    assert.equal(state.timers.length, 0);
});

test('remote success with a cache failure requires refresh instead of another write', async () => {
    const {state, handlers, controls} = harness({
        withTask: true,
        completionRequest: async () => ({
            ok: false, json: async () => ({
                success: false, remote_changed: true, error: 'Refresh MSX tasks before retrying.',
            }),
        }),
    });
    await handlers['toggle:click']();
    assert.equal(controls['[data-hva-toggle]'].disabled, true);
    assert.match(controls['[data-hva-result]'].textContent, /Refresh MSX tasks/);
    await handlers['toggle:click']();
    assert.equal(state.requests.length, 1);
    assert.equal(state.reloads, 0);
});

test('network and malformed responses never claim completion', async () => {
    for (const completionRequest of [
        async () => { throw new Error('Network unavailable'); },
        async () => ({ok: false, json: async () => { throw new SyntaxError('Invalid JSON'); }}),
        async () => ({ok: true, json: async () => null}),
    ]) {
        const {state, handlers, controls} = harness({withTask: true, completionRequest});
        await handlers['toggle:click']();
        assert.equal(controls['[data-hva-result]'].attributes.role, 'alert');
        assert.equal(controls['[data-hva-toggle]'].disabled, false);
        assert.equal(state.errors.length, 1);
        assert.equal(state.timers.length, 0);
    }
});

test('category changes immediately save a numeric option and reload only after MSX succeeds', async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const {state, handlers, controls} = harness({
        withTask: true, categoryChange: true, completionRequest: () => response,
    });
    const select = controls['[data-hva-category]'];
    await handlers['category:change']();
    assert.equal(state.requests.length, 0);
    select.value = '606820000';
    const running = handlers['category:change']();
    await handlers['category:change']();
    assert.equal(state.requests.length, 1);
    assert.equal(select.disabled, true);
    assert.equal(state.requests[0][0], '/api/reports/hvas/17/category');
    assert.equal(state.requests[0][1].headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(state.requests[0][1].body), {category: 606820000});
    assert.match(controls['[data-hva-result]'].textContent, /Updating category in MSX/);
    assert.equal(state.reloads, 0);
    resolve({ok: true, json: async () => ({success: true})});
    await running;
    assert.equal(state.reloads, 1);
    assert.equal(select.disabled, true);
});

for (const remoteChanged of [false, true]) {
    test(`category failure handles remote_changed=${remoteChanged} without false success`, async () => {
        const {state, handlers, controls} = harness({
            withTask: true, categoryChange: true,
            completionRequest: async () => ({
                ok: false, json: async () => ({
                    success: false, remote_changed: remoteChanged,
                    error: remoteChanged ? 'Refresh MSX tasks.' : 'Access denied',
                }),
            }),
        });
        const select = controls['[data-hva-category]'];
        select.value = '606820000';
        await handlers['category:change']();
        assert.equal(state.reloads, 0);
        assert.equal(select.disabled, remoteChanged);
        assert.equal(select.value, remoteChanged ? '606820000' : '861980004');
        assert.equal(controls['[data-hva-result]'].attributes.role, 'alert');
        if (!remoteChanged) {
            select.value = '606820000';
            await handlers['category:change']();
            assert.equal(state.requests.length, 2);
        }
    });
}
