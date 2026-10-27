const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/js/ui-admin-panel.js'), 'utf8');
const admin = fs.readFileSync(path.join(__dirname, '../../templates/admin_panel.html'), 'utf8');

test('update restart polling compares full hashes even when short labels match', async () => {
    const pollSource = admin.match(/function pollForServer[\s\S]*?(?=\n\/\/ Separate display)/)[0];
    const before = '2abcc4507bed30c94061059fc6c4812668264229';
    const after = '2abcc4517bed30c94061059fc6c4812668264229';
    assert.match(admin, /const preCommit = last\.boot_commit_full \|\| last\.local_commit_full/);
    for (const [commit, expectedDelay] of [[before, 1000], [after, 500]]) {
        const delays = [];
        vm.runInNewContext(`${pollSource}\npollForServer(0, '${before}', false);`, {
            fetch: () => Promise.resolve({ok: true, json: () => Promise.resolve({
                commit: commit.slice(0, 7), commit_full: commit,
            })}),
            window: {}, document: {getElementById: () => ({textContent: ''})},
            setTimeout: (_callback, delay) => delays.push(delay),
        });
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(delays, [expectedDelay]);
    }
});

function fixture(saved = {}, fields = [], drafts = {}) {
    const classes = new Set(), events = [], handlers = {}, attributes = {};
    const panel = {classList: {
        contains: name => classes.has(name),
        toggle(name, force) { force ? classes.add(name) : classes.delete(name); },
    }, dispatchEvent: event => events.push(event.type)};
    const trigger = {dataset: {sbAdminDisclosure: 'fyCollapse'}, handlers,
        setAttribute: (name, value) => { attributes[name] = value; },
        classList: {toggle() {}}, closest: () => null,
        addEventListener: (name, handler) => { handlers[name] = handler; }};
    const root = {
        classList: {contains: () => false},
        querySelectorAll: selector => selector.startsWith('[data-sb-admin-disclosure') ?
            [trigger] : selector.startsWith('#exportName') ? fields : [],
    };
    const observers = [];
    const documentHandlers = {}, history = {state: {sbAdminDisclosure: saved, sbAdminFields: drafts},
        replaceState(state) { this.state = state; }};
    vm.runInNewContext(source, {
        document: {addEventListener: (name, handler) => { documentHandlers[name] = handler; },
            querySelector: () => root, getElementById: () => panel},
        HTMLButtonElement: class {}, history, window: {}, location: {pathname: '/admin'},
        MutationObserver: class {
            constructor(callback) { observers.push(callback); }
            observe() {}
        },
        CustomEvent: class {constructor(type) { this.type = type; }},
    });
    documentHandlers.DOMContentLoaded();
    return {trigger, panel, attributes, events, history,
        refresh: () => observers.forEach(callback => callback())};
}

test('FY disclosure is keyboard-operable and persists native state without a request', () => {
    const h = fixture();
    assert.equal(h.trigger.tabIndex, 0);
    assert.equal(h.attributes['aria-expanded'], 'false');
    let prevented = false;
    h.trigger.handlers.keydown({key: 'Enter', target: h.trigger,
        preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(h.panel.classList.contains('show'), true);
    assert.equal(h.attributes['aria-expanded'], 'true');
    assert.equal(h.history.state.sbAdminDisclosure.fyCollapse, true);
    assert.deepEqual(h.events, ['sb:disclosure-opened']);
    h.trigger.handlers.click({target: h.trigger, preventDefault() {}});
    assert.equal(h.panel.classList.contains('show'), false);
    assert.deepEqual(h.events, ['sb:disclosure-opened', 'sb:disclosure-closed']);
});

test('Back restores the expanded FY inspection without changing business state', () => {
    const h = fixture({fyCollapse: true});
    assert.equal(h.panel.classList.contains('show'), true);
    assert.equal(h.attributes['aria-expanded'], 'true');
    assert.deepEqual(h.events, []);
});

test('interactive children do not collapse telemetry controls', () => {
    const h = fixture();
    h.trigger.handlers.click({target: {closest: () => ({})},
        preventDefault() { throw new Error('Interactive child was intercepted'); }});
    assert.equal(h.panel.classList.contains('show'), false);
});

test('Back retains a category draft until asynchronous options arrive', () => {
    function field(id, tagName) {
        return {id, tagName, type: 'text', value: '', handlers: {},
            matches: () => true,
            addEventListener(name, handler) { this.handlers[name] = handler; }};
    }
    const category = field('evtCategory', 'SELECT');
    category.type = 'select-one';
    category.options = [{value: ''}];
    let selected = '';
    Object.defineProperty(category, 'value', {
        get: () => selected,
        set(value) {
            selected = category.options.some(option => option.value === value) ? value : '';
        },
    });

    test('restored event search uses its pending category and only the latest response wins', async () => {
        const start = admin.indexOf('let evtCurrentPage = 1;');
        const end = admin.indexOf('// Populate category dropdown', start);
        const fields = new Map(), requests = [];
        function field(id) {
            if (!fields.has(id)) {
                const element = {value: '', checked: false, style: {}};
                element.querySelector = () => element.errorCell ||= {textContent: ''};
                fields.set(id, element);
            }
            return fields.get(id);
        }
        const history = {
            state: {sbAdminFields: {evtCategory: 'notes'}},
            replaceState(value) { this.state = value; },
        };
        const context = vm.createContext({
            document: {getElementById: field}, history, URLSearchParams,
            fetch: url => new Promise(resolve => requests.push({url, resolve})),
        });
        vm.runInContext(admin.slice(start, end), context);
        const older = context.loadEventLog(2);
        const newer = context.loadEventLog(3);
        assert.equal(new URL(requests[0].url, 'http://localhost').searchParams.get('category'), 'notes');
        assert.equal(history.state.sbAdminEventPage, 3);
        const response = endpoint => ({
            ok: true, json: async () => ({
                events: [{timestamp: '2026-07-01T00:00:00Z', method: 'GET', endpoint,
                    status_code: 200, response_time_ms: 0, category: 'notes'}],
                page: 3, pages: 3, total: 101,
            }),
        });
        requests[1].resolve(response('newer-query'));
        await newer;
        requests[0].resolve(response('older-query'));
        await older;
        assert.match(field('evtLogBody').innerHTML, /newer-query/);
        assert.doesNotMatch(field('evtLogBody').innerHTML, /older-query/);
        assert.match(field('evtLogBody').innerHTML, /data-sb-sort-value="0"/);
        field('evtCategory').value = 'Unsubmitted draft';
        const restored = context.loadEventLog(3, history.state.sbAdminEventQuery);
        assert.equal(new URL(requests[2].url, 'http://localhost').searchParams.get('category'), 'notes');
        assert.equal(field('evtCategory').value, 'Unsubmitted draft');
        requests[2].resolve(response('restored-query'));
        await restored;
        const failed = context.loadEventLog(1);
        requests[3].resolve({ok: false, json: async () => ({error: '<img>Literal error'})});
        await failed;
        assert.equal(field('evtLogBody').errorCell.textContent, 'Failed: <img>Literal error');
        assert.doesNotMatch(field('evtLogBody').innerHTML, /<img>/);
    });
    const endpoint = field('evtEndpoint', 'INPUT');
    const h = fixture({}, [category, endpoint], {evtCategory: 'notes'});
    assert.equal(category.value, '');
    endpoint.value = '/metrics';
    endpoint.handlers.input();
    assert.equal(h.history.state.sbAdminFields.evtCategory, 'notes');
    category.options.push({value: 'notes'});
    h.refresh();
    assert.equal(category.value, 'notes');
    assert.equal(h.history.state.sbAdminFields.evtEndpoint, '/metrics');
});

test('data-refresh sorting preserves real tbody and action nodes rather than cloning records', () => {
    assert.match(source, /next\.append\(body\)/);
    assert.match(source, /rows\.every\(row => previous\.rows\.includes\(row\)\)/);
    assert.match(source, /SBTableSort\.create\(table\)/);
    assert.match(source, /data-sb-sort-fixed/);
    assert.doesNotMatch(source, /bootstrap\./);
});

test('table membership refresh remounts once, preserving actual row actions and the history key', () => {
    const created = [];
    let observe, ready, table;
    const header = () => ({
        textContent: 'Count', cellIndex: 0, colSpan: 1, dataset: {},
        querySelector: () => null,
    });
    const head = () => ({
        rows: [{cells: [header()]}], cloneNode: () => head(), contains: () => false,
    });
    const row = value => ({
        action: {identity: value, isConnected: true,
            focus() { this.restored = true; }},
        cells: [{textContent: String(value), dataset: {},
            hasAttribute: () => false, querySelector: () => null}],
        querySelector: () => null, setAttribute() {},
    });
    const first = row(1), second = row(2);
    const body = {id: 'real-events', rows: [first, second]};
    const document = {
        activeElement: first.action,
        addEventListener(name, callback) { ready = callback; },
        querySelector: () => root,
    };
    function shell() {
        return {
            dataset: {}, tHead: null, tBodies: [],
            cloneNode() {
                const next = shell();
                next.dataset = {...this.dataset};
                return next;
            },
            append(node) {
                if (node.rows[0]?.cells && !node.id) this.tHead = node;
                else {
                    this.tBodies.push(node);
                    document.activeElement = null;
                }
            },
            setAttribute() {}, replaceWith(next) { table = next; },
        };
    }
    table = shell();
    table.tHead = head();
    table.tBodies = [body];
    const root = {
        classList: {contains: () => false},
        querySelectorAll: selector => selector === 'table.sb-admin-table' ? [table] : [],
    };
    const sorter = {create: target => created.push(target)};
    vm.runInNewContext(source, {
        document,
        window: {SBTableSort: sorter}, SBTableSort: sorter,
        history: {state: {}}, location: {pathname: '/admin'},
        MutationObserver: class {
            constructor(callback) { observe = callback; }
            observe() {}
        },
    });
    ready();
    assert.equal(created.length, 1);
    body.rows = [second, first];
    observe();
    assert.equal(created.length, 1, 'sorting alone must not remount');
    const replacement = row(3);
    body.rows = [first, replacement];
    observe();
    assert.equal(created.length, 2);
    assert.equal(table.tBodies[0], body);
    assert.equal(table.tBodies[0].rows[1], replacement);
    assert.equal(table.tBodies[0].rows[1].action, replacement.action);
    assert.equal(first.action.restored, true, 'moving the tbody must preserve action focus');
    assert.equal(table.dataset.sbSortKey, 'real-events');
    observe();
    assert.equal(created.length, 2, 'the observer must settle after a remount');
});
