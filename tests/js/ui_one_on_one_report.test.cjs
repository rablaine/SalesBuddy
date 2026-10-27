const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {sellerWorkspaceUrl, validateView} = require('../../static/js/ui-one-on-one-report.js');
const source = fs.readFileSync(path.resolve(__dirname, '..', '..', 'static', 'js',
    'ui-one-on-one-report.js'), 'utf8');
const view = {tab: 'alltime', scroll: {recent: 81, alltime: 321},
    closed: ['oo-alltime-2'], notes: ['notes-1'], tables: {oneOnOneQuarter: 110}};

class Node {
    constructor(id) {
        this.id = id;
        this.attrs = {};
        this.events = {};
        this.scrollTop = 0;
        this.open = true;
        this.hidden = false;
    }
    setAttribute(key, value) { this.attrs[key] = value; }
    getAttribute(key) { return this.attrs[key]; }
    addEventListener(type, listener) { this.events[type] = listener; }
    focus() { this.focused = true; }
    fire(type, event = {}) { this.events[type]?.(event); }
}

function harness({saved, embedded = false, rejectSave = false, period = 'Last 2 Weeks',
    periodDays = '14', storedPeriod = null, href = 'http://localhost/reports/one-on-one',
    rejectStorageRead = false, rejectStorageWrite = false} = {}) {
    const ids = Object.fromEntries(['oneOnOneReading', 'oneOnOneScopeStatus',
        'oneOnOneNavigationStatus', 'oneOnOneLauncherStatus', 'openSellerOneOnOne',
        'oneOnOneSellerSelect', 'oneOnOnePeriod', 'recent', 'alltime']
        .map(id => [id, new Node(id)]));
    ids.oneOnOneSellerSelect.value = '7';
    ids.oneOnOneNavigationStatus.hidden = ids.oneOnOneLauncherStatus.hidden = true;
    ids.recent.querySelectorAll = () => [1];
    ids.alltime.querySelectorAll = () => [1, 2];
    const buttons = ['recent', 'alltime'].map(tab => {
        const node = new Node(tab + '-tab');
        node.attrs['aria-controls'] = tab;
        return node;
    });
    const customers = ['oo-recent-1', 'oo-alltime-1', 'oo-alltime-2'].map(id => new Node(id));
    const note = new Node('notes-1');
    note.open = false;
    const table = new Node('oneOnOneQuarter');
    table.parentElement = new Node('quarterScroll');
    ids[table.id] = table;
    const surface = {
        dataset: {embedded: String(embedded), periodLabel: period},
        querySelectorAll(selector) {
            if (selector === '[role="tab"]') return buttons;
            if (selector === '.oo-customer') return customers;
            if (selector === '.oo-customer:not([open])') return customers.filter(row => !row.open);
            if (selector === '.oo-notes') return [note];
            if (selector === '.oo-notes[open]') return note.open ? [note] : [];
            if (selector === '.oo-table') return [table];
            throw new Error('Unexpected selector: ' + selector);
        },
    };
    const events = {}, errors = [], locations = [], replacements = [], submissions = [];
    const storage = new Map(storedPeriod === null ? [] :
        [['salesbuddy_one_on_one_period_days', storedPeriod]]);
    ids.oneOnOnePeriod.value = periodDays;
    ids.oneOnOnePeriod.form = {requestSubmit: () => submissions.push(ids.oneOnOnePeriod.value)};
    const root = {
        document: {readyState: 'complete', getElementById: id => ids[id],
            querySelector: selector => selector === '.sb-one-on-one' ? surface : null},
        history: {
            state: {...(saved ? {sbOneOnOneReport: saved} : {}), sbTableSort: {quarter: 'retained'}},
            replaceState(value) {
                if (rejectSave) throw new Error('History unavailable');
                this.state = value;
            },
        },
        console: {error: (...args) => errors.push(args)},
        location: {href, assign: url => locations.push(url),
            replace: url => replacements.push(url)},
        localStorage: {
            getItem(key) {
                if (rejectStorageRead) throw new Error('Storage read unavailable');
                return storage.get(key) ?? null;
            },
            setItem(key, value) {
                if (rejectStorageWrite) throw new Error('Storage write unavailable');
                storage.set(key, value);
            },
        },
        requestAnimationFrame: callback => callback(),
        addEventListener: (type, listener) => { events[type] = listener; },
    };
    vm.runInNewContext(source, {window: root, URL});
    return {root, ids, buttons, customers, note, table, events, errors, locations,
        replacements, storage, submissions};
}

test('reporting period changes persist both choices before submitting', () => {
    const h = harness();
    for (const days of ['30', '14']) {
        h.ids.oneOnOnePeriod.value = days;
        h.ids.oneOnOnePeriod.fire('change');
        assert.equal(h.storage.get('salesbuddy_one_on_one_period_days'), days);
    }
    assert.deepEqual(h.submissions, ['30', '14']);
});

test('a bare visit restores the saved period and preserves other URL state', () => {
    const h = harness({storedPeriod: '30', embedded: true,
        href: 'http://localhost/reports/one-on-one?activity_sort=acr&milestone_focus=15&embed=1#recent'});
    const destination = new URL(h.replacements[0]);
    assert.equal(destination.searchParams.get('days'), '30');
    assert.equal(destination.searchParams.get('activity_sort'), 'acr');
    assert.equal(destination.searchParams.get('milestone_focus'), '15');
    assert.equal(destination.searchParams.get('embed'), '1');
    assert.equal(destination.hash, '#recent');
    assert.deepEqual(h.submissions, []);
    assert.equal(h.root.history.state.sbOneOnOneReport, undefined);
    const loaded = harness({storedPeriod: '30', periodDays: '30', href: destination.href});
    assert.deepEqual(loaded.replacements, []);
});

test('explicit URL periods override storage without replacing the saved preference', () => {
    for (const days of ['14', '30']) {
        const storedPeriod = days === '14' ? '30' : '14';
        const h = harness({storedPeriod, periodDays: days,
            href: 'http://localhost/reports/one-on-one?days=' + days});
        assert.deepEqual(h.replacements, []);
        assert.equal(h.storage.get('salesbuddy_one_on_one_period_days'), storedPeriod);
    }
});

test('fresh and already matching visits do not redirect or invent preferences', () => {
    for (const storedPeriod of [null, '14']) {
        const h = harness({storedPeriod});
        assert.deepEqual(h.replacements, []);
        assert.equal(h.storage.get('salesbuddy_one_on_one_period_days'), storedPeriod ?? undefined);
        assert.deepEqual(h.errors, []);
    }
});

test('invalid or unreadable stored periods report failure without blocking the report', () => {
    for (const options of [{storedPeriod: '7'}, {storedPeriod: ''}, {rejectStorageRead: true}]) {
        const h = harness(options);
        assert.deepEqual(h.replacements, []);
        assert.equal(h.ids.recent.hidden, false);
        assert.equal(h.ids.oneOnOneNavigationStatus.hidden, false);
        assert.match(h.ids.oneOnOneNavigationStatus.textContent, /Could not restore/);
        assert.equal(h.errors.length, 1);
    }
});

test('storage write failure does not prevent applying the chosen reporting period', () => {
    const h = harness({rejectStorageWrite: true});
    h.ids.oneOnOnePeriod.value = '30';
    h.ids.oneOnOnePeriod.fire('change');
    assert.deepEqual(h.submissions, ['30']);
    assert.equal(h.ids.oneOnOneNavigationStatus.hidden, false);
    assert.match(h.ids.oneOnOneNavigationStatus.textContent, /Could not remember/);
    assert.equal(h.errors.length, 1);
});

test('seller navigation keeps its existing numeric destination and rejects invalid selection', () => {
    assert.equal(sellerWorkspaceUrl('7'), '/seller/7/one-on-one');
    for (const id of ['', '0', '07', '-1', '1.5', null, 7, '../admin']) {
        assert.throws(() => sellerWorkspaceUrl(id), /Select a valid seller/);
    }
});

test('reading state preserves both lenses, expanded notes and independent table position', () => {
    assert.equal(validateView(view), view);
    for (const invalid of [null, [], {}, {...view, tab: 'unknown'},
        {...view, scroll: {recent: 1, alltime: -1}}, {...view, scroll: {recent: 1}},
        {...view, notes: ['wrong-1']}, {...view, closed: ['notes-1']},
        {...view, tables: []}, {...view, tables: {other: 20}},
        {...view, tables: {oneOnOneQuarter: Infinity}}]) {
        assert.throws(() => validateView(invalid), /Invalid saved/);
    }
});

test('initial view owns only the selected panel, with roving tab focus', () => {
    const {ids, buttons} = harness();
    assert.equal(ids.recent.hidden, false);
    assert.equal(ids.alltime.hidden, true);
    assert.equal(buttons[0].attrs['aria-selected'], 'true');
    assert.equal(buttons[0].tabIndex, 0);
    assert.equal(buttons[1].tabIndex, -1);
    assert.equal(ids.oneOnOneScopeStatus.textContent, 'Last 2 Weeks · 1 customer');
});

test('switching lenses retains the chosen 30-day period in the status', () => {
    const {ids, buttons} = harness({period: 'Last 30 Days'});
    assert.equal(ids.oneOnOneScopeStatus.textContent, 'Last 30 Days · 1 customer');
    buttons[1].fire('click');
    buttons[0].fire('click');
    assert.equal(ids.oneOnOneScopeStatus.textContent, 'Last 30 Days · 1 customer');
});

test('switching and keyboard navigation preserve each lens reading offset', () => {
    const {ids, buttons} = harness();
    ids.oneOnOneReading.scrollTop = 81;
    buttons[1].fire('click');
    assert.equal(ids.oneOnOneReading.scrollTop, 0);
    assert.equal(ids.recent.hidden, true);
    assert.equal(ids.alltime.hidden, false);
    ids.oneOnOneReading.scrollTop = 321;
    let prevented = false;
    buttons[1].fire('keydown', {key: 'ArrowRight', preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(buttons[0].focused, true);
    assert.equal(ids.oneOnOneReading.scrollTop, 81);
    buttons[0].fire('keydown', {key: 'End', preventDefault() {}});
    assert.equal(ids.oneOnOneReading.scrollTop, 321);
    assert.equal(ids.oneOnOneScopeStatus.textContent, 'All Open Engagements · 2 customers');
});

test('pagehide saves state without overwriting the shared table sorter state', () => {
    const {root, ids, buttons, customers, note, table, events} = harness();
    ids.oneOnOneReading.scrollTop = 81;
    buttons[1].fire('click');
    ids.oneOnOneReading.scrollTop = 321;
    customers[2].open = false;
    note.open = true;
    table.parentElement.scrollTop = 110;
    events.pagehide();
    assert.deepEqual(JSON.parse(JSON.stringify(root.history.state.sbOneOnOneReport)), view);
    assert.deepEqual(root.history.state.sbTableSort, {quarter: 'retained'});
});

test('Back restores tab, disclosures and positions on load and bfcache pageshow', () => {
    const {ids, buttons, customers, note, table, events} = harness({saved: view});
    assert.equal(buttons[1].attrs['aria-selected'], 'true');
    assert.equal(ids.oneOnOneReading.scrollTop, 321);
    assert.equal(customers[2].open, false);
    assert.equal(note.open, true);
    assert.equal(table.parentElement.scrollTop, 110);
    ids.oneOnOneReading.scrollTop = 0;
    events.pageshow();
    assert.equal(ids.oneOnOneReading.scrollTop, 321);
});

test('embeds do not read or change their parent history entry', () => {
    const {ids, events, root} = harness({saved: view, embedded: true});
    assert.equal(ids.alltime.hidden, true);
    assert.equal(events.pagehide, undefined);
    assert.equal(root.history.state.sbOneOnOneReport, view);
});

test('history read and write failures are logged and surfaced instead of hidden', () => {
    const broken = harness({saved: {...view, scroll: null}});
    assert.equal(broken.ids.oneOnOneNavigationStatus.hidden, false);
    assert.match(broken.ids.oneOnOneNavigationStatus.textContent, /Could not restore/);
    assert.equal(broken.errors.length, 1);
    const failed = harness({rejectSave: true});
    failed.events.pagehide();
    assert.equal(failed.ids.oneOnOneNavigationStatus.hidden, false);
    assert.match(failed.ids.oneOnOneNavigationStatus.textContent, /Could not save/);
    assert.equal(failed.errors.length, 1);
});

test('workspace Open routes valid selection, while invalid selection stays in the dialog', () => {
    const {ids, locations, errors} = harness();
    ids.openSellerOneOnOne.fire('click');
    assert.deepEqual(locations, ['/seller/7/one-on-one']);
    ids.oneOnOneSellerSelect.value = '';
    ids.openSellerOneOnOne.fire('click');
    assert.equal(locations.length, 1);
    assert.equal(ids.oneOnOneLauncherStatus.hidden, false);
    assert.match(ids.oneOnOneLauncherStatus.textContent, /Select a valid seller/);
    assert.equal(errors.length, 1);
});
