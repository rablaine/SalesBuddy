const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../templates/metrics.html'), 'utf8');
const source = template.match(/<script>\s*([\s\S]*?)<\/script>/)[1]
    .replace('{{ days }}', '7');

function fixture(payloads = {}, rejects = false) {
    const keys = ['activity_summary', 'dau', 'page_loads', 'install_inventory',
        'installs_by_role', 'se_specialties', 'app_versions', 'workiq_uptime'];
    const modes = {activity_summary: 'activity_summary', dau: 'dau_line',
        workiq_uptime: 'workiq_uptime'};
    const cards = new Map(keys.map(key => [key, {
        innerHTML: '', classList: {add() {}},
        getAttribute: name => name === 'data-card' ? key : modes[key],
        querySelector: () => ({}),
    }]));
    const banner = {innerHTML: '', className: '', textContent: ''};
    const handlers = {}, requests = [], charts = [], timers = [];
    const context = {
        document: {
            body: {}, documentElement: {},
            querySelectorAll: selector => selector === '[data-card]' ? [...cards.values()] : [],
            getElementById: () => banner,
            addEventListener: (name, callback) => { handlers[name] = callback; },
        },
        window: {Share: {isShareEnabled: () => true}},
        MutationObserver: class {observe() {}},
        getComputedStyle: () => ({getPropertyValue: () => '#808080'}),
        Chart: class {
            constructor(_canvas, options) { charts.push(options); }
            destroy() {}
        },
        fetch: url => {
            requests.push(url);
            if (rejects) return Promise.reject(new Error('offline'));
            const key = url.includes('/sign-in') ? 'sign-in' :
                url.match(/\/card\/([^?]+)/)[1];
            return Promise.resolve({
                json: () => Promise.resolve({
                    rows: [], ...payloads[key],
                }),
            });
        },
        setTimeout: callback => timers.push(callback),
    };
    vm.runInNewContext(source, context);
    return {cards, banner, handlers, requests, charts, timers};
}

function settled() {
    return new Promise(resolve => setImmediate(resolve));
}

test('page initialization reads cards without starting interactive sign-in', async () => {
    const h = fixture();
    await settled();
    assert.equal(h.requests.length, h.cards.size + 1);
    assert.ok(h.requests.every(url => url.startsWith('/api/metrics/card/')));
    assert.match(h.cards.get('page_loads').innerHTML, /No data in window/);
    assert.match(h.banner.innerHTML, /No WorkIQ calls/);
});

test('activity overview and DAU consume the exact backend output shape', async () => {
    const h = fixture({
        activity_summary: {rows: [{active_today: 3, active_installs: 5, page_loads: 42}]},
        dau: {rows: [
            {timestamp: '2026-10-25T00:00:00Z', user_role: 'SE', DAU: 2},
            {timestamp: '2026-10-25T00:00:00Z', user_role: 'Seller', DAU: 1},
            {timestamp: '2026-10-26T00:00:00Z', user_role: 'Seller', DAU: 2},
        ]},
    });
    await settled();
    const overview = h.cards.get('activity_summary').innerHTML;
    assert.match(overview, /Active today \(UTC\).*<dd>3<\/dd>/);
    assert.match(overview, /Active in selected range.*<dd>5<\/dd>/);
    assert.match(overview, /Page loads in selected range.*<dd>42<\/dd>/);
    const datasets = h.charts[0].data.datasets;
    assert.deepEqual(Array.from(datasets[0].data), [3, 2]);
    assert.deepEqual(Array.from(datasets[1].data), [2, 0]);
    assert.deepEqual(Array.from(datasets[2].data), [1, 2]);
    assert.deepEqual(Array.from(h.charts[0].data.labels, label => Array.from(label)), [
        ['Sun', '10/25'], ['Mon', '10/26'],
    ]);
});

test('DAU weekday labels and full-date tooltips use UTC across year boundaries', async () => {
    const h = fixture({dau: {rows: [
        {timestamp: '2025-12-31T00:00:00Z', user_role: 'SE', DAU: 1},
        {timestamp: '2026-01-01T00:00:00Z', user_role: 'SE', DAU: 2},
    ]}});
    await settled();
    const chart = h.charts[0];
    assert.deepEqual(Array.from(chart.data.labels, label => Array.from(label)), [
        ['Wed', '12/31'], ['Thu', '1/1'],
    ]);
    const title = chart.options.plugins.tooltip.callbacks.title;
    assert.equal(title([{dataIndex: 0}]), 'Wednesday, Dec 31, 2025 (UTC)');
    assert.equal(title([{dataIndex: 1}]), 'Thursday, Jan 1, 2026 (UTC)');
    assert.equal(chart.options.scales.x.ticks.autoSkip, true);
    assert.equal(chart.options.scales.x.ticks.maxTicksLimit, 14);
});

test('inventory and page tables escape values and retain numeric sorting', async () => {
    const h = fixture({
        page_loads: {rows: [{
            feature: '<script>bad</script>', page: '/customer/<int:id>', page_loads: 100,
        }]},
        install_inventory: {rows: [{
            instance_id: 'anonymous-install', app_version: 'a'.repeat(40),
            last_seen_utc: '2026-10-26T00:00:00Z', last_active_utc: null,
        }]},
    });
    await settled();
    const pages = h.cards.get('page_loads').innerHTML;
    assert.ok(!pages.includes('<script>bad</script>'));
    assert.match(pages, /&lt;script&gt;bad&lt;\/script&gt;/);
    assert.match(pages, /\/customer\/&lt;int:id&gt;/);
    assert.match(pages, /data-sb-sort-type="number"/);
    assert.match(pages, /data-sb-sort-value="100"/);
    const inventory = h.cards.get('install_inventory').innerHTML;
    assert.match(inventory, /Anonymous install/);
    assert.match(inventory, /Boot commit/);
    assert.match(inventory, /Last page activity \(UTC\)/);
    assert.match(inventory, new RegExp('a'.repeat(40)));
});

test('authentication failures show explicit sign-in controls but never invoke login', async () => {
    const h = fixture({
        activity_summary: {auth_required: true, error: 'Sign in required'},
        page_loads: {error: 'Query failed'},
    });
    await settled();
    assert.match(h.cards.get('activity_summary').innerHTML, /metrics-sign-in/);
    assert.match(h.cards.get('page_loads').innerHTML, /Query failed/);
    assert.ok(!h.requests.some(url => url.includes('/sign-in')));
});

test('version dates are sortable and missing Git matches are explicitly unknown', async () => {
    const h = fixture({app_versions: {rows: [
        {app_version: 'abcdef0', commit_date: '2026-10-26', installs: 3},
        {app_version: 'unknown', commit_date: null, installs: 2},
    ]}});
    await settled();
    const versions = h.cards.get('app_versions').innerHTML;
    assert.match(versions, /<th data-sb-sort-type="date">Commit date<\/th>/);
    assert.match(versions, /data-sb-sort-value="2026-10-26">2026-10-26<\/td>/);
    assert.match(versions, /data-sb-sort-value="">Unknown<\/td>/);
});

test('network failures surface on individual cards and the WorkIQ banner', async () => {
    const h = fixture({}, true);
    await settled();
    assert.match(h.cards.get('dau').innerHTML, /Network error: offline/);
    assert.match(h.banner.textContent, /Could not load WorkIQ uptime. offline/);
});

test('sign-in polling does not mistake a query error for authentication success', async () => {
    const h = fixture({
        'sign-in': {success: true},
        activity_summary: {error: 'Query failed', auth_required: false},
    });
    await settled();
    const status = {textContent: ''};
    const button = {disabled: false, parentElement: {querySelector: () => status}};
    h.handlers.click({target: {closest: () => button}, preventDefault() {}});
    await settled();
    assert.match(status.textContent, /Waiting for sign-in/);
    h.timers.shift()();
    await settled();
    assert.match(status.textContent, /Query failed/);
    assert.equal(button.disabled, false);
    assert.equal(h.timers.length, 0);
});
