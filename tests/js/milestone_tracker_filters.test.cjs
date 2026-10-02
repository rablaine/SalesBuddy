const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const partial = fs.readFileSync(
    path.join(__dirname, '../../templates/partials/milestone_tracker_content.html'), 'utf8'
);
const start = partial.indexOf('(function() {', partial.indexOf('// Self-contained filter'));
const script = partial.slice(start, partial.indexOf('function toggleMilestoneFavorite', start));

function element(value = '') {
    const classes = new Set();
    return {
        value, textContent: '', style: {}, checked: false, listeners: {}, selectedIndex: 0,
        classList: {
            add(name) { classes.add(name); },
            remove(name) { classes.delete(name); },
            contains: name => classes.has(name),
            toggle(name, force) {
                const active = force ?? !classes.has(name);
                if (active) classes.add(name); else classes.delete(name);
            },
        },
        focus() { this.focused = true; },
        setAttribute(name, value) { this[name] = value; },
        addEventListener(event, listener) { this.listeners[event] = listener; },
    };
}

function tracker(saved = null, locked = false) {
    const controls = {};
    for (const role of [
        'sellerFilter', 'customerFilter', 'searchFilter', 'statusDropdownBtn', 'areaDropdownBtn',
        'urgencyFilter', 'myTeamFilter', 'commitmentFilter', 'filteredCount', 'resetFiltersBtn',
        'quarterDropdown', 'quarterSelectAll', 'quarterSelectNone', 'favoritesOnlyBtn',
        'totalCount', 'totalValue', 'pastDueCount', 'thisWeekCount',
        'customerDropdownBtn', 'customerSearch', 'customerAll', 'customerNoMatches',
    ]) controls[role] = element();
    controls.customerFilter.options = [
        {value: '', textContent: 'All Customers'},
        {value: '1', textContent: 'Acme Corp'},
        {value: '2', textContent: 'Globex Inc'},
    ];
    Object.defineProperty(controls.customerFilter, 'selectedIndex', {
        get() { return this.options.findIndex(option => option.value === this.value); },
    });
    const customerButtons = controls.customerFilter.options.slice(1).map(option => {
        const button = element();
        button.textContent = option.textContent;
        button.dataset = {customerOption: option.value};
        return button;
    });
    let dropdownCloses = 0;
    if (locked) controls.sellerFilter = null;
    const rows = [
        {dataset: {
            customerId: '1', sellerId: '11', search: 'launch poc architect pilot',
            monthly: '1234', status: 'On Track', urgency: 'past_due',
            onMyTeam: 'true', commitment: 'Uncommitted', quarter: 'FY27 Q2', area: 'Data',
        }, style: {}},
        {dataset: {
            customerId: '2', sellerId: '12', search: 'poc for another customer',
            monthly: '500', status: 'Blocked', urgency: 'this_week',
            onMyTeam: 'false', commitment: 'Committed', quarter: 'FY27 Q2', area: 'Infra',
        }, style: {}},
    ];
    const quarter = Object.assign(element('FY27 Q2'), {checked: true});
    const status = element('On Track');
    const area = element('Data');
    let storage = saved ? JSON.stringify(saved) : null;
    let lastEvent;
    const container = {
        dataset: {total: '2', lockedSeller: '11'},
        classList: {contains: () => true},
        hasAttribute: () => locked,
        querySelector: selector => controls[selector.match(/"([^"]+)"/)[1]] || null,
        querySelectorAll(selector) {
            if (selector === '.milestone-row') return rows;
            if (selector === '[data-customer-option]') return customerButtons;
            const entries = selector.startsWith('.quarter-checkbox') ? [quarter]
                : selector.startsWith('.status-checkbox') ? [status]
                : selector.startsWith('.area-checkbox') ? [area] : [];
            return selector.includes(':checked') ? entries.filter(e => e.checked) : entries;
        },
        dispatchEvent(event) { lastEvent = event.detail; },
    };
    vm.runInNewContext(script, {
        document: {querySelectorAll: () => [{previousElementSibling: container}]},
        window: {location: {search: ''}},
        URLSearchParams,
        CustomEvent: class {constructor(_type, options) { this.detail = options.detail; }},
        bootstrap: {
            Dropdown: {getOrCreateInstance: () => ({hide() { dropdownCloses++; }})},
        },
        localStorage: {
            getItem: () => storage,
            setItem(_key, value) { storage = value; },
            removeItem() { storage = null; },
        },
    });
    return {
        controls, rows, status, area, customerButtons,
        closes: () => dropdownCloses,
        saved: () => JSON.parse(storage),
        event: () => lastEvent,
        visible: () => rows.filter(row => row.style.display !== 'none'),
    };
}

test('case-insensitive literal search combines with customer and summary totals', () => {
    const t = tracker();
    t.controls.searchFilter.value = '  PoC  ';
    t.controls.searchFilter.listeners.input();
    assert.equal(t.visible().length, 2);
    t.controls.customerFilter.value = '1';
    t.controls.customerFilter.listeners.change();
    assert.equal(t.visible().length, 1);
    assert.equal(t.controls.totalCount.textContent, 1);
    assert.equal(t.controls.totalValue.textContent, '$1,234');
    assert.equal(t.controls.pastDueCount.textContent, 1);
    assert.equal(t.controls.thisWeekCount.textContent, 0);
    assert.equal(t.event().customer, '1');
    assert.equal(t.event().search, 'poc');
    assert.equal(t.saved().search, '  PoC  ');
    assert.equal(t.controls.resetFiltersBtn.style.display, '');
});

test('search is literal and no-match results can be reset', () => {
    const t = tracker();
    t.controls.searchFilter.value = '.*';
    t.controls.searchFilter.listeners.input();
    assert.equal(t.visible().length, 0);
    assert.equal(t.controls.totalValue.textContent, '$0');
    t.controls.resetFiltersBtn.listeners.click();
    assert.equal(t.visible().length, 2);
    assert.equal(t.controls.searchFilter.value, '');
    assert.equal(t.controls.customerFilter.value, '');
    assert.equal(t.controls.resetFiltersBtn.style.display, 'none');
});

test('new filters persist and old saved settings still load', () => {
    const restored = tracker({search: 'pilot', customer: '1'});
    assert.equal(restored.visible().length, 1);
    assert.equal(restored.controls.searchFilter.value, 'pilot');
    assert.equal(restored.controls.customerFilter.value, '1');
    const old = tracker({seller: '12', status: 'Blocked'});
    assert.equal(old.controls.searchFilter.value, '');
    assert.equal(old.controls.customerFilter.value, '');
    assert.equal(old.visible().length, 1);
});

test('search/customer intersect seller, status, area, team, and commitment filters', () => {
    const t = tracker();
    t.controls.searchFilter.value = 'pilot';
    t.controls.customerFilter.value = '1';
    t.controls.sellerFilter.value = '11';
    t.controls.myTeamFilter.value = 'on';
    t.controls.commitmentFilter.value = 'Uncommitted';
    t.status.checked = true;
    t.area.checked = true;
    t.controls.searchFilter.listeners.input();
    assert.equal(t.visible().length, 1);
    t.controls.sellerFilter.value = '12';
    t.controls.sellerFilter.listeners.change();
    assert.equal(t.visible().length, 0);
});

test('seller-locked tracker supports the new filters without a seller control', () => {
    const t = tracker({search: 'pilot', customer: '1'}, true);
    assert.equal(t.visible().length, 1);
    t.controls.resetFiltersBtn.listeners.click();
    assert.equal(t.visible().length, 2);
});

test('customer dropdown narrows case-insensitively without changing the selected filter', () => {
    const t = tracker({customer: '1'});
    assert.equal(t.controls.customerDropdownBtn.textContent, 'Acme Corp');
    t.controls.customerDropdownBtn.listeners['shown.bs.dropdown']();
    assert.equal(t.controls.customerSearch.focused, true);
    t.controls.customerSearch.value = '  gLoB  ';
    t.controls.customerSearch.listeners.input();
    assert.equal(t.customerButtons[0].classList.contains('d-none'), true);
    assert.equal(t.customerButtons[1].classList.contains('d-none'), false);
    assert.equal(t.controls.customerFilter.value, '1');
    assert.equal(t.visible().length, 1);
    t.customerButtons[1].listeners.click();
    assert.equal(t.controls.customerFilter.value, '2');
    assert.equal(t.controls.customerDropdownBtn.textContent, 'Globex Inc');
    assert.equal(t.visible()[0].dataset.customerId, '2');
    assert.equal(t.saved().customer, '2');
    assert.equal(t.closes(), 1);
    assert.equal(t.controls.customerDropdownBtn.focused, true);
});

test('customer dropdown reports no matches and Enter selects the first matching customer', () => {
    const t = tracker();
    t.controls.customerSearch.value = 'not a customer';
    t.controls.customerSearch.listeners.input();
    assert.equal(t.controls.customerNoMatches.classList.contains('d-none'), false);
    t.controls.customerSearch.listeners.keydown({key: 'Enter', preventDefault() {}});
    assert.equal(t.controls.customerFilter.value, '');
    t.controls.customerSearch.value = 'acme';
    t.controls.customerSearch.listeners.keydown({key: 'ArrowDown', preventDefault() {}});
    assert.equal(t.customerButtons[0].focused, true);
    t.controls.customerSearch.listeners.keydown({key: 'Enter', preventDefault() {}});
    assert.equal(t.controls.customerFilter.value, '1');
    assert.equal(t.controls.customerNoMatches.classList.contains('d-none'), true);
});

test('typing previews the first Enter target without committing a customer selection', () => {
    const t = tracker({customer: '2'});
    const highlighted = () => t.customerButtons.filter(
        button => button.classList.contains('enter-target')
    );
    t.controls.customerSearch.value = 'c';
    t.controls.customerSearch.listeners.input();
    assert.equal(highlighted().length, 1);
    assert.equal(highlighted()[0].dataset.customerOption, '1');
    assert.equal(t.controls.customerFilter.value, '2');
    t.controls.customerSearch.value = 'glob';
    t.controls.customerSearch.listeners.input();
    assert.equal(highlighted()[0].dataset.customerOption, '2');
    t.controls.customerSearch.value = 'no match';
    t.controls.customerSearch.listeners.input();
    assert.equal(highlighted().length, 0);
    t.controls.customerSearch.value = '';
    t.controls.customerSearch.listeners.input();
    assert.equal(highlighted().length, 0);
    t.controls.customerSearch.value = 'c';
    t.controls.customerSearch.listeners.input();
    t.customerButtons[1].listeners.focus();
    assert.equal(highlighted()[0].dataset.customerOption, '2');
    t.controls.customerSearch.listeners.focus();
    assert.equal(highlighted()[0].dataset.customerOption, '1');
    t.controls.customerSearch.listeners.keydown({key: 'Enter', preventDefault() {}});
    assert.equal(t.controls.customerFilter.value, '1');
});

test('All Customers and Reset clear selection and reopening restores the full options list', () => {
    const t = tracker({customer: '2'});
    t.controls.customerAll.listeners.click();
    assert.equal(t.visible().length, 2);
    assert.equal(t.controls.customerDropdownBtn.textContent, 'All Customers');
    t.controls.customerSearch.value = 'glob';
    t.controls.customerSearch.listeners.input();
    t.controls.customerDropdownBtn.listeners['shown.bs.dropdown']();
    assert.equal(t.controls.customerSearch.value, '');
    assert.equal(t.customerButtons.every(button => !button.classList.contains('d-none')), true);
    t.customerButtons[0].listeners.click();
    t.controls.resetFiltersBtn.listeners.click();
    assert.equal(t.controls.customerFilter.value, '');
    assert.equal(t.controls.customerDropdownBtn.textContent, 'All Customers');
    assert.equal(t.saved().customer, '');
});

test('calendar query forwards encoded search and the customer ID', () => {
    const template = fs.readFileSync(
        path.join(__dirname, '../../templates/milestone_tracker.html'), 'utf8'
    );
    const functionSource = template.slice(
        template.indexOf('function getFilterParams()'),
        template.indexOf('function load()')
    );
    const controls = {
        sellerFilter: element(), customerFilter: element('42'),
        searchFilter: element('  POC & "pilot"  '), urgencyFilter: element(),
        myTeamFilter: element(),
    };
    const context = {
        instance: {
            querySelector: selector => controls[selector.match(/"([^"]+)"/)[1]],
            querySelectorAll: () => [],
        },
        encodeURIComponent,
    };
    vm.runInNewContext(functionSource + '\nparams = getFilterParams();', context);
    const params = new URLSearchParams(context.params);
    assert.equal(params.get('customer_id'), '42');
    assert.equal(params.get('search'), 'POC & "pilot"');
});

test('older calendar responses cannot overwrite the latest filter results', async () => {
    const template = fs.readFileSync(
        path.join(__dirname, '../../templates/milestone_tracker.html'), 'utf8'
    );
    const functionSource = template.slice(
        template.indexOf('function load()'),
        template.indexOf('function render(data)')
    );
    const pending = [];
    const rendered = [];
    const body = element();
    const title = element();
    const context = {
        calendarIsActive: true, calendarRequestId: 0, year: 2026, month: 10,
        getFilterParams: () => '', render: data => rendered.push(data),
        document: {getElementById: id => id === 'msCalendarBody' ? body : title},
        fetch: () => new Promise(resolve => pending.push(resolve)),
        console,
    };
    vm.runInNewContext(functionSource + '\nload(); load();', context);
    pending[1]({ok: true, json: async () => ({month_name: 'New results', year: 2026})});
    await new Promise(resolve => setImmediate(resolve));
    pending[0]({ok: true, json: async () => ({month_name: 'Stale results', year: 2026})});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(rendered.length, 1);
    assert.equal(title.textContent, 'New results 2026');
});
