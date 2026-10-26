const assert = require('node:assert/strict');
const test = require('node:test');
const {selections, liveSelections, configuration} = require('../../static/js/ui-dashboard-filters.js');
const {validate} = require('../../static/js/ui-dashboard-drafts.js');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('filter state is canonical and separate from live selections', () => {
    assert.deepEqual(selections({customer_ids: [3, 1, 3]}),
        {customer_ids: [1, 3], seller_ids: []});
    assert.deepEqual(configuration({scope: {seller_ids: [7]},
        live_filters: ['seller_ids', 'customer_ids', 'seller_ids']}),
    {scope: {customer_ids: [], seller_ids: [7]}, live_filters: ['customer_ids', 'seller_ids']});
    const original = {customer_ids: [4]};
    const normalized = selections(original);
    normalized.customer_ids.push(6);
    assert.deepEqual(original, {customer_ids: [4]});
});

test('invalid filters cannot become unrestricted selections', () => {
    for (const value of [null, [], {extra: []}, {customer_ids: '1'},
        {seller_ids: [true]}, {customer_ids: [0]}, {seller_ids: ['1']},
        {customer_ids: [2 ** 63]}, JSON.parse('{"__proto__": []}')]) {
        assert.throws(() => selections(value), /Invalid/);
    }
    for (const value of [[], {other: []}, {live_filters: ['other']},
        {live_filters: {}}, {live_filters: [['customer_ids']]}, {scope: []}, {scope: null}]) {
        assert.throws(() => configuration(value), /Invalid/);
    }
});

test('seller grouping is a strict live choice, not a customer scope', () => {
    assert.deepEqual(liveSelections({seller_ids: [7], group_by_seller: true}),
        {customer_ids: [], seller_ids: [7], group_by_seller: true});
    assert.deepEqual(liveSelections({group_by_seller: false}), selections());
    for (const value of [null, 0, 1, 'true', [], {}]) {
        assert.throws(() => liveSelections({group_by_seller: value}), /boolean/);
    }
    assert.throws(() => selections({group_by_seller: true}), /Invalid/);
    assert.throws(() => configuration({scope: {group_by_seller: true}}), /Invalid/);
    assert.deepEqual(configuration({live_filters: ['group_by_seller']}).live_filters,
        ['group_by_seller']);
});

function draft() {
    return {
        version: 1, revision: 0, name: 'Saved', subtitle: '',
        base_name: 'Saved', base_subtitle: '', content_width: 'wide', display_style: 'cards',
        filter_config: {scope: {customer_ids: [4]}, live_filters: ['customer_ids']},
        widgets: [{id: 1, key: 'hygiene.gaps', variant_key: 'metric',
            layout_column: 0, layout_row: 0, column_span: 6, row_span: 4,
            filters: {seller_ids: [7]}}],
    };
}

test('draft validation preserves filter scope and accepts old drafts', () => {
    const current = draft();
    assert.equal(validate(current), current);
    delete current.filter_config;
    delete current.widgets[0].filters;
    assert.equal(validate(current), current);
});

test('corrupted draft filters produce errors instead of clearing saved boundaries', () => {
    for (const filters of [null, [], {unsupported: []}, {customer_ids: [true]}]) {
        const current = draft();
        current.widgets[0].filters = filters;
        assert.throws(() => validate(current), /filter/);
    }
    const current = draft();
    current.filter_config.live_filters = ['unknown'];
    assert.throws(() => validate(current), /filter/);
});

test('drafts preserve grouping availability, not the current viewer grouping selection', () => {
    const current = draft();
    current.filter_config.live_filters.push('group_by_seller');
    assert.equal(validate(current), current);
    current.widgets[0].filters.group_by_seller = true;
    assert.throws(() => validate(current), /filter/);
    delete current.widgets[0].filters.group_by_seller;
    current.filter_config.scope.group_by_seller = true;
    assert.throws(() => validate(current), /filter/);
});

function filterUI(editing = true, liveFilters = ['customer_ids', 'seller_ids'], options = {}) {
    const all = [];
    const document = {
        readyState: 'complete',
        addEventListener(type, handler) { this[type] = handler; },
        createTextNode(text) { return {textContent: text}; },
        querySelectorAll(selector) {
            if (selector === '[data-live-filter]') return inputs;
            if (selector === '#dashboardCanvas [data-widget-id]') return tiles;
            if (selector === '.db-filter-picker[open]') {
                return all.filter(node => node.className === 'db-filter-picker' && node.open);
            }
            return [];
        },
        querySelector(selector) {
            if (selector === '.sb-dashboards') {
                return {dataset: {dashboardId: options.dashboardId || '5'}};
            }
            return inputs.find(input => selector.includes(input.dataset.liveFilter));
        },
    };
    function node() {
        const value = {
            hidden: false, disabled: false, children: [], dataset: {}, attributes: {},
            textContent: '',
            classList: {values: new Set(), toggle(key, enabled) {
                if (enabled) this.values.add(key);
                else this.values.delete(key);
            }},
            append(...children) {
                children.forEach(child => { child.parentElement = this; });
                this.children.push(...children);
            },
            replaceChildren() { this.children = []; },
            get childElementCount() { return this.children.length; },
            setAttribute(key, value) { this.attributes[key] = value; },
            getAttribute(key) { return this.attributes[key]; },
            addEventListener(type, handler) { this[type] = handler; },
            contains(target) { return target === this || this.children.some(c => c.contains?.(target)); },
            closest(selector) {
                return this.className?.split(' ').includes(selector.slice(1))
                    ? this : this.parentElement?.closest(selector) || null;
            },
            querySelectorAll(selector) {
                return all.filter(child => this.contains(child)
                    && child.className === 'db-filter-picker' && child.open);
            },
            focus() { document.activeElement = this; },
        };
        all.push(value);
        return value;
    }
    document.createElement = node;
    const nodes = Object.fromEntries([
        'dashboardFilterConfig', 'dashboardLiveFilters', 'dashboardFilterStatus',
        'dashboardFilterCoverage', 'dashboardLiveControls', 'dashboardSavedScope',
        'dashboardScopeHint', 'dashboardViewerFilterHint', 'tryDashboardFilters',
        'dashboardReading', 'resetDashboardFilters', 'previewDashboardDraft',
        'dashboardFiltersPanel', 'openDashboardFilters', 'closeDashboardFilters',
        'dashboardFiltersTitle', 'dashboardFiltersSubtitle', 'dashboardFilterCount',
        'dashboardViewerActions', 'dashboardTemporaryFilterHint',
        'dashboardViewerScope', 'dashboardViewerScopeText',
        ...(editing ? ['openWidgetFilters', 'dashboardFilterTabs', 'dashboardFilterDraftHint',
            'dashboardFilterTab', 'widgetFilterTab', 'widgetFilterSettings',
            'dashboardScopeControls', 'widgetFilterControls', 'dashboardFilterSettings',
            'widgetFilterTitle', 'widgetFilterInheritance', 'widgetFilterSupport'] : []),
    ].map(id => [id, node()]));
    nodes.dashboardFilterConfig.textContent = JSON.stringify({
        editing, config: {scope: {}, live_filters: liveFilters},
        live: {}, choices: {customer_ids: [{id: 1, name: 'First account'},
            {id: 2, name: 'Second account'}], seller_ids: [{id: 7, name: 'First owner'}]},
        widgets: [{supported: ['customer_ids', 'seller_ids',
            ...(liveFilters.includes('group_by_seller') ? ['group_by_seller'] : [])]}],
        catalog: [{key: 'test.widget', title: 'Test widget'}],
    });
    const inputs = editing ? ['customer_ids', 'seller_ids', 'group_by_seller'].map(key => {
        const input = node();
        input.dataset.liveFilter = key;
        return input;
    }) : [];
    nodes.dashboardFiltersPanel.className = 'db-filters-panel';
    nodes.dashboardFiltersPanel.hidden = true;
    nodes.dashboardLiveFilters.append(nodes.dashboardLiveControls);
    nodes.dashboardFiltersPanel.append(nodes.dashboardFiltersTitle, nodes.dashboardLiveFilters,
        nodes.dashboardViewerActions);
    if (editing) {
        nodes.widgetFilterSettings.hidden = true;
        nodes.widgetFilterTab.disabled = true;
        nodes.dashboardFilterTab.setAttribute('role', 'tab');
        nodes.widgetFilterTab.setAttribute('role', 'tab');
        nodes.dashboardFiltersPanel.append(nodes.widgetFilterControls, nodes.dashboardScopeControls);
    }
    document.getElementById = id => nodes[id] || null;
    let refreshes = 0;
    const storage = options.storage || new Map();
    const events = {};
    const requests = [];
    const rendered = [];
    const tiles = options.tiles ? [1, 2].map(id => ({
        dataset: {widgetKey: 'test.widget', variant: 'detailed', columnSpan: '6',
            widgetId: String(id), widgetFilters: JSON.stringify({customer_ids: [id]})},
        querySelector() { return {remove() {}}; },
        insertAdjacentHTML(position, html) { rendered.push(html); },
    })) : [];
    const global = {document, console, location: {search: options.search || ''},
        SBConnectGoals: {destroy() {}, init() {}},
        async fetch(url) {
            requests.push(new URLSearchParams(url.split('?')[1]));
            return {ok: true, json: async () => ({success: true, html: '<p>Filtered</p>'})};
        },
        addEventListener(type, handler) { events[type] = handler; },
        localStorage: {
            getItem(key) { return storage.get(key) ?? null; },
            setItem(key, value) {
                if (options.storageFailure) throw new Error('Storage unavailable');
                storage.set(key, value);
            },
        },
        SBDashboardEditor: {
        refreshFilters: async () => { refreshes++; },
        setFilterConfig(config) { global.SBDashboardFilters.restore(config); },
        setWidgetFilters(id, filters) {
            global.SBDashboardFilters.select({id, key: 'test.widget', filters},
                ['customer_ids', 'seller_ids']);
        },
    }};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,
        '../../static/js/ui-dashboard-filters.js'), 'utf8'), {window: global, document,
        URLSearchParams});
    const trigger = (element, type, values = {}) => {
        const event = {target: element, preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this.propagationStopped = true; }, ...values};
        element[type](event);
        return event;
    };
    return {api: global.SBDashboardFilters, nodes, document, trigger, inputs,
        refreshes: () => refreshes, editor: global.SBDashboardEditor, storage, events,
        requests, rendered};
}

test('viewer selections restore from localStorage and refresh every widget on return', async () => {
    const storage = new Map();
    const first = filterUI(false, ['customer_ids', 'seller_ids', 'group_by_seller'], {storage});
    const before = JSON.stringify(first.api.context().dashboard);
    for (const picker of first.nodes.dashboardLiveControls.children.slice(0, 2)) {
        const checkbox = picker.children[1].children[2].children[0].children[0];
        checkbox.checked = true;
        first.trigger(checkbox, 'change');
    }
    const grouping = first.nodes.dashboardLiveControls.children[2].children[0];
    grouping.checked = true;
    first.trigger(grouping, 'change');
    assert.deepEqual(JSON.parse(storage.get('sb.dashboard.filters.5')),
        {customer_ids: [1], seller_ids: [7], group_by_seller: true});
    assert.equal(JSON.stringify(first.api.context().dashboard), before);

    const returned = filterUI(false, ['customer_ids', 'seller_ids', 'group_by_seller'],
        {storage, tiles: true});
    await new Promise(setImmediate);
    assert.equal(returned.nodes.dashboardFilterCount.textContent, '3');
    assert.equal(returned.requests.length, 2);
    assert.equal(returned.rendered.length, 2);
    returned.requests.forEach((query, index) => {
        const context = JSON.parse(query.get('filter_context'));
        assert.deepEqual(context.live,
            {customer_ids: [1], seller_ids: [7], group_by_seller: true});
        assert.deepEqual(context.widget.customer_ids, [index + 1]);
    });
    assert.equal(returned.nodes.dashboardLiveControls.children[1].children[0].textContent,
        'Seller: First owner');
    assert.equal(returned.nodes.dashboardLiveControls.children[2].children[0].checked, true);
    returned.events.pageshow({persisted: true});
    assert.equal(returned.requests.length, 2, 'Unchanged bfcache state needs no reload');
    assert.equal(filterUI(false, undefined, {storage, dashboardId: '6'})
        .nodes.dashboardFilterCount.hidden, true);
});

test('reset persists across reloads and disabled controls cannot restore stale restrictions',
    async () => {
        const storage = new Map([['sb.dashboard.filters.5', JSON.stringify({
            customer_ids: [1], seller_ids: [7], group_by_seller: true,
        })]]);
        const ui = filterUI(false, ['seller_ids'], {storage, tiles: true});
        await new Promise(setImmediate);
        assert.deepEqual(JSON.parse(JSON.stringify(ui.api.context().live)),
            {customer_ids: [], seller_ids: [7]});
        ui.trigger(ui.nodes.resetDashboardFilters, 'click');
        await new Promise(setImmediate);
        const reloaded = filterUI(false, ['seller_ids'], {storage, tiles: true});
        assert.equal(reloaded.nodes.dashboardFilterCount.hidden, true);
        assert.equal(reloaded.requests.length, 0);
        assert.deepEqual(JSON.parse(storage.get('sb.dashboard.filters.5')),
            {customer_ids: [], seller_ids: []});
    });

test('storage and pageshow restore changed preferences without leaking into editor drafts',
    async () => {
        const storage = new Map();
        const ui = filterUI(false, ['seller_ids'], {storage, tiles: true});
        storage.set('sb.dashboard.filters.5', JSON.stringify({seller_ids: [7]}));
        ui.events.storage({key: 'sb.dashboard.filters.6'});
        assert.equal(ui.requests.length, 0);
        ui.events.storage({key: 'sb.dashboard.filters.5'});
        await new Promise(setImmediate);
        assert.equal(ui.requests.length, 2);
        storage.clear();
        ui.events.pageshow({persisted: true});
        await new Promise(setImmediate);
        assert.equal(ui.nodes.dashboardFilterCount.hidden, true);
        assert.equal(ui.requests.length, 4);
        const editor = filterUI(true, ['seller_ids'], {storage});
        const checkbox = editor.nodes.dashboardLiveControls.children[0].children[1]
            .children[2].children[0].children[0];
        checkbox.checked = true;
        editor.trigger(checkbox, 'change');
        await new Promise(setImmediate);
        assert.equal(storage.size, 0);
    });

test('explicit URL filters take precedence over remembered viewer preferences', () => {
    const storage = new Map([['sb.dashboard.filters.5', '{"seller_ids":[7]}']]);
    const ui = filterUI(false, ['seller_ids', 'group_by_seller'],
        {storage, search: '?group_by_seller=false', tiles: true});
    assert.equal(ui.nodes.dashboardFilterCount.hidden, true);
    assert.equal(ui.requests.length, 0);
});

test('invalid or unavailable filter storage reports errors without breaking current filtering',
    async () => {
        for (const value of ['{', 'null', '{"seller_ids":["7"]}', '{"extra":[]}']) {
            const storage = new Map([['sb.dashboard.filters.5', value]]);
            const ui = filterUI(false, ['seller_ids'], {storage});
            assert.match(ui.nodes.dashboardFilterStatus.textContent, /Could not restore/);
            assert.equal(ui.nodes.dashboardFilterStatus.hidden, false);
        }
        const ui = filterUI(false, ['seller_ids'], {storageFailure: true, tiles: true});
        const checkbox = ui.nodes.dashboardLiveControls.children[0].children[1]
            .children[2].children[0].children[0];
        checkbox.checked = true;
        ui.trigger(checkbox, 'change');
        await new Promise(setImmediate);
        assert.equal(ui.requests.length, 2);
        assert.equal(ui.api.context().live.seller_ids[0], 7);
        assert.match(ui.nodes.dashboardFilterStatus.textContent, /Could not remember/);
        assert.equal(ui.nodes.dashboardFilterStatus.hidden, false);
    });

test('filter panel opens without changing draft state and closes with focus restoration', () => {
    const {api, nodes, document, trigger} = filterUI();
    const before = JSON.stringify(api.context());
    trigger(nodes.openDashboardFilters, 'click');
    assert.equal(nodes.dashboardFiltersPanel.hidden, false);
    assert.equal(nodes.openDashboardFilters.hidden, true);
    assert.equal(nodes.openDashboardFilters.attributes['aria-expanded'], 'true');
    assert.equal(document.activeElement, nodes.dashboardFiltersTitle);
    const escape = trigger(nodes.dashboardFiltersPanel, 'keydown', {key: 'Escape'});
    assert.equal(escape.defaultPrevented, true);
    assert.equal(escape.propagationStopped, true);
    assert.equal(nodes.dashboardFiltersPanel.hidden, true);
    assert.equal(nodes.openDashboardFilters.hidden, false);
    assert.equal(document.activeElement, nodes.openDashboardFilters);
    assert.equal(nodes.openDashboardFilters.attributes['aria-expanded'], 'false');
    assert.equal(JSON.stringify(api.context()), before);
});

test('widget filters show inheritance and retain open checkboxes across edits', async () => {
    const {api, nodes, trigger} = filterUI();
    api.select({id: 1, key: 'test.widget', filters: {}}, ['customer_ids', 'seller_ids']);
    assert.equal(nodes.openWidgetFilters.disabled, false);
    trigger(nodes.openWidgetFilters, 'click');
    assert.equal(nodes.dashboardFilterSettings.hidden, true);
    assert.equal(nodes.widgetFilterSettings.hidden, false);
    assert.equal(nodes.widgetFilterTitle.textContent, 'Test widget');
    assert.equal(nodes.widgetFilterInheritance.textContent, 'Inherits dashboard filters');
    const picker = nodes.widgetFilterControls.children[0];
    const checkbox = picker.children[1].children[2].children[0].children[0];
    picker.open = true;
    checkbox.checked = true;
    trigger(checkbox, 'change');
    assert.equal(nodes.widgetFilterControls.children[0], picker);
    assert.equal(picker.open, true);
    assert.equal(nodes.widgetFilterInheritance.textContent,
        'Dashboard filters + widget restrictions');
    api.select(null);
    assert.equal(nodes.dashboardFilterSettings.hidden, false);
    assert.equal(nodes.widgetFilterSettings.hidden, true);
    assert.equal(nodes.widgetFilterTab.disabled, true);
    await new Promise(setImmediate);
});

test('one viewer grouping checkbox updates all supported widgets without editing defaults', async () => {
    const {api, nodes, trigger, editor, refreshes} = filterUI(true, ['group_by_seller']);
    const supported = ['customer_ids', 'seller_ids', 'group_by_seller'];
    editor.setWidgetFilters = () => assert.fail('Temporary grouping must not edit widget filters');
    editor.setFilterConfig = () => assert.fail('Temporary grouping must not edit dashboard settings');
    api.configure([{supported}, {supported: ['customer_ids', 'seller_ids']}], false);
    api.select({id: 1, key: 'test.widget', filters: {}}, supported);
    assert.equal(nodes.widgetFilterControls.children.length, 2);
    const before = JSON.stringify(api.context().dashboard);
    api.setPreview(true);
    const label = nodes.dashboardLiveControls.children[0];
    assert.equal(nodes.dashboardLiveControls.children.length, 1);
    assert.equal(label.className, 'db-live-group-filter');
    assert.equal(label.children[1].textContent, 'Group by seller');
    const checkbox = label.children[0];
    assert.equal(checkbox.checked, false);
    checkbox.checked = true;
    trigger(checkbox, 'change');
    assert.equal(api.context().live.group_by_seller, true);
    assert.equal(Object.hasOwn(api.context().widget, 'group_by_seller'), false);
    assert.equal(nodes.dashboardFilterCount.textContent, '1');
    assert.equal(nodes.dashboardFilterCoverage.textContent, 'Applies to 1 of 2 widgets');
    assert.equal(nodes.resetDashboardFilters.disabled, false);
    checkbox.checked = false;
    trigger(checkbox, 'change');
    assert.equal(Object.hasOwn(api.context().live, 'group_by_seller'), false);
    assert.equal(nodes.dashboardFilterCount.hidden, true);
    checkbox.checked = true;
    trigger(checkbox, 'change');
    trigger(nodes.resetDashboardFilters, 'click');
    assert.equal(Object.hasOwn(api.context().live, 'group_by_seller'), false);
    assert.equal(nodes.dashboardLiveControls.children[0].children[0].checked, false);
    assert.equal(nodes.resetDashboardFilters.disabled, true);
    assert.equal(JSON.stringify(api.context().dashboard), before);
    assert.equal(nodes.dashboardScopeControls.children.length, 2);
    await new Promise(setImmediate);
    assert.equal(refreshes(), 4);
});

test('editor can enable and disable grouping as an available viewer filter', async () => {
    const {api, nodes, trigger, inputs, editor} = filterUI();
    api.configure([{supported: ['customer_ids', 'seller_ids', 'group_by_seller']}], false);
    const changes = [];
    editor.setFilterConfig = value => {
        changes.push(JSON.parse(JSON.stringify(value)));
        api.restore(value);
    };
    const checkbox = inputs.find(input => input.dataset.liveFilter === 'group_by_seller');
    assert.equal(checkbox.disabled, false);
    checkbox.checked = true;
    trigger(checkbox, 'change');
    assert.deepEqual(changes[0].live_filters, ['customer_ids', 'seller_ids', 'group_by_seller']);
    assert.equal(checkbox.checked, true);
    assert.equal(nodes.dashboardLiveControls.children.length, 3);
    assert.match(nodes.dashboardViewerFilterHint.textContent, /Group by seller/);
    assert.equal(Object.hasOwn(api.context().live, 'group_by_seller'), false);
    checkbox.checked = false;
    trigger(checkbox, 'change');
    assert.deepEqual(changes[1].live_filters, ['customer_ids', 'seller_ids']);
    assert.equal(nodes.dashboardLiveControls.children.length, 2);
    await new Promise(setImmediate);
});

test('expanded seller list stays open when grouping is clicked, retaining search and scroll', async () => {
    const {api, nodes, document, trigger} = filterUI(false, ['seller_ids', 'group_by_seller']);
    trigger(nodes.openDashboardFilters, 'click');
    const seller = nodes.dashboardLiveControls.children[0];
    const search = seller.children[1].children[0];
    const list = seller.children[1].children[2];
    seller.open = true;
    search.value = 'owner';
    trigger(search, 'input');
    list.scrollTop = 36;
    const checkbox = nodes.dashboardLiveControls.children[1].children[0];
    assert.equal(document.pointerdown, undefined);
    assert.equal(seller.open, true);
    checkbox.checked = true;
    trigger(checkbox, 'change');
    await new Promise(setImmediate);
    assert.equal(api.context().live.group_by_seller, true);
    assert.equal(nodes.dashboardLiveControls.children[0], seller);
    assert.equal(seller.open, true);
    assert.equal(search.value, 'owner');
    assert.equal(list.scrollTop, 36);
    trigger(seller, 'keydown', {key: 'Escape'});
    assert.equal(seller.open, false);
    assert.equal(document.activeElement, seller.children[0]);
});

test('sidebar lists stay open independently until explicitly collapsed', () => {
    const {nodes, document, trigger} = filterUI(false);
    const [customer, seller] = nodes.dashboardLiveControls.children;
    customer.open = true;
    seller.open = true;
    assert.equal(customer.open, true);
    assert.equal(seller.open, true);
    assert.equal(document.pointerdown, undefined);
    assert.equal(customer.open, true);
    assert.equal(seller.open, true);
    trigger(nodes.closeDashboardFilters, 'click');
    assert.equal(customer.open, false);
    assert.equal(seller.open, false);
});

test('published viewer exposes grouping without any editor-only widget controls', () => {
    const {api, nodes, trigger} = filterUI(false, ['group_by_seller']);
    assert.equal(nodes.openDashboardFilters.hidden, false);
    trigger(nodes.openDashboardFilters, 'click');
    const checkbox = nodes.dashboardLiveControls.children[0].children[0];
    checkbox.checked = true;
    trigger(checkbox, 'change');
    assert.equal(api.context().live.group_by_seller, true);
    assert.equal(nodes.widgetFilterControls, undefined);
    api.configure([{supported: ['customer_ids']}], false);
    assert.equal(nodes.dashboardLiveControls.children.length, 0);
    assert.equal(nodes.openDashboardFilters.hidden, true);
    assert.equal(Object.hasOwn(api.context().live, 'group_by_seller'), false);
});

test('preview switches the open panel to temporary filters without exposing saved settings', async () => {
    const {api, nodes, trigger} = filterUI();
    trigger(nodes.openDashboardFilters, 'click');
    assert.equal(nodes.dashboardLiveFilters.hidden, true);
    const before = JSON.stringify(api.context().dashboard);
    api.setPreview(true);
    assert.equal(nodes.dashboardFiltersPanel.hidden, false);
    assert.equal(nodes.openDashboardFilters.hidden, true);
    assert.equal(nodes.dashboardLiveFilters.hidden, false);
    assert.equal(nodes.dashboardFilterTabs.hidden, true);
    assert.equal(nodes.dashboardFilterSettings.hidden, true);
    assert.equal(nodes.widgetFilterSettings.hidden, true);
    assert.equal(nodes.dashboardFilterDraftHint.hidden, true);
    assert.equal(nodes.dashboardFiltersSubtitle.textContent, 'This view only');
    const customer = nodes.dashboardLiveControls.children[0];
    assert.equal(customer.children[0].textContent, 'Customer: All customers');
    const checkbox = customer.children[1].children[2].children[0].children[0];
    checkbox.checked = true;
    trigger(checkbox, 'change');
    assert.equal(nodes.resetDashboardFilters.disabled, false);
    assert.equal(nodes.dashboardFilterCount.textContent, '1');
    assert.equal(JSON.stringify(api.context().dashboard), before);
    api.setPreview(false);
    assert.equal(nodes.dashboardFiltersPanel.hidden, false);
    assert.equal(nodes.openDashboardFilters.hidden, true);
    assert.equal(nodes.dashboardLiveFilters.hidden, true);
    assert.equal(nodes.dashboardFilterSettings.hidden, false);
    assert.equal(nodes.dashboardViewerActions.hidden, true);
    assert.equal(nodes.dashboardFilterCount.hidden, true);
    assert.equal(nodes.openDashboardFilters.classList.values.has('is-active'), false);
    assert.equal(api.context().live.customer_ids[0], 1);
    api.setPreview(true);
    assert.equal(nodes.dashboardFilterCount.textContent, '1');
    assert.equal(nodes.dashboardFilterCount.hidden, false);
    assert.equal(nodes.dashboardViewerActions.hidden, false);
    trigger(nodes.resetDashboardFilters, 'click');
    api.setPreview(false);
    assert.equal(nodes.dashboardLiveFilters.hidden, true);
    assert.equal(nodes.resetDashboardFilters.disabled, true);
    assert.equal(nodes.dashboardViewerActions.hidden, true);
    assert.equal(nodes.dashboardFilterCount.hidden, true);
    assert.equal(JSON.stringify(api.context().dashboard), before);
    await new Promise(setImmediate);
});

test('active filter indications are viewer-only while preview selections are retained', async () => {
    for (const key of ['customer_ids', 'seller_ids', 'group_by_seller']) {
        const {api, nodes, trigger} = filterUI(true,
            ['customer_ids', 'seller_ids', 'group_by_seller']);
        const before = JSON.stringify(api.context().dashboard);
        api.setPreview(true);
        const index = ['customer_ids', 'seller_ids', 'group_by_seller'].indexOf(key);
        const control = nodes.dashboardLiveControls.children[index];
        const checkbox = key === 'group_by_seller' ? control.children[0]
            : control.children[1].children[2].children[0].children[0];
        checkbox.checked = true;
        trigger(checkbox, 'change');
        assert.equal(nodes.dashboardFilterCount.textContent, '1');
        assert.equal(nodes.openDashboardFilters.classList.values.has('is-active'), true);
        api.setPreview(false);
        assert.equal(nodes.dashboardFilterCount.hidden, true);
        assert.equal(nodes.openDashboardFilters.classList.values.has('is-active'), false);
        assert.equal(nodes.openDashboardFilters.attributes['aria-label'], 'Filters');
        assert.equal(nodes.openDashboardFilters.title, 'Open filters');
        assert.equal(nodes.dashboardViewerActions.hidden, true);
        assert.equal(nodes.dashboardLiveFilters.hidden, true);
        api.setPreview(true);
        assert.equal(nodes.dashboardFilterCount.hidden, false);
        assert.equal(nodes.dashboardFilterCount.textContent, '1');
        assert.equal(nodes.openDashboardFilters.classList.values.has('is-active'), true);
        assert.equal(nodes.dashboardViewerActions.hidden, false);
        assert.equal(checkbox.checked, true);
        assert.equal(JSON.stringify(api.context().dashboard), before);
        await new Promise(setImmediate);
    }
});

test('published panel contains only temporary filters and read-only scope', () => {
    const {api, nodes, trigger} = filterUI(false);
    assert.equal(nodes.dashboardLiveFilters.hidden, false);
    assert.equal(nodes.openDashboardFilters.hidden, false);
    trigger(nodes.openDashboardFilters, 'click');
    assert.equal(nodes.dashboardFiltersPanel.hidden, false);
    assert.equal(nodes.dashboardFilterSettings, undefined);
    assert.equal(nodes.widgetFilterSettings, undefined);
    assert.equal(nodes.dashboardViewerActions.hidden, false);
    assert.equal(nodes.dashboardViewerScope.hidden, true);
    api.restore({scope: {seller_ids: [7]}, live_filters: ['customer_ids']});
    assert.equal(nodes.dashboardLiveControls.children[0].children[0].textContent,
        'Customer: All allowed customers');
    assert.equal(nodes.dashboardSavedScope.textContent, 'Always includes Seller: First owner');
    assert.equal(nodes.dashboardViewerScope.hidden, false);
    assert.equal(nodes.dashboardViewerScopeText.textContent, 'Seller: First owner');
    assert.equal(nodes.resetDashboardFilters.disabled, true);
});

test('panel tabs support keyboard navigation and respect unavailable widget selection', () => {
    const {api, nodes, document, trigger} = filterUI();
    api.select(null);
    trigger(nodes.dashboardFiltersPanel, 'keydown',
        {target: nodes.dashboardFilterTab, key: 'ArrowRight'});
    assert.equal(nodes.widgetFilterSettings.hidden, true);
    api.select({id: 1, key: 'test.widget', filters: {}}, ['customer_ids']);
    trigger(nodes.dashboardFiltersPanel, 'keydown',
        {target: nodes.dashboardFilterTab, key: 'ArrowRight'});
    assert.equal(nodes.widgetFilterSettings.hidden, false);
    assert.equal(document.activeElement, nodes.widgetFilterTab);
    trigger(nodes.dashboardFiltersPanel, 'keydown',
        {target: nodes.widgetFilterTab, key: 'Home'});
    assert.equal(nodes.widgetFilterSettings.hidden, true);
    assert.equal(document.activeElement, nodes.dashboardFilterTab);
    api.select({id: 1, key: 'test.widget', filters: {}}, ['customer_ids'], true);
    assert.equal(nodes.openWidgetFilters.disabled, true);
});

test('collapsed rail works in preview without opening settings', () => {
    const {api, nodes, trigger} = filterUI();
    assert.equal(nodes.dashboardFiltersPanel.hidden, true);
    assert.equal(nodes.openDashboardFilters.hidden, false);
    api.setPreview(true);
    assert.equal(nodes.dashboardFiltersPanel.hidden, true);
    assert.equal(nodes.openDashboardFilters.hidden, false);
    trigger(nodes.openDashboardFilters, 'click');
    assert.equal(nodes.dashboardFiltersPanel.hidden, false);
    assert.equal(nodes.dashboardLiveFilters.hidden, false);
    api.setPreview(false);
    assert.equal(nodes.dashboardFiltersPanel.hidden, true);
    assert.equal(nodes.openDashboardFilters.hidden, false);
    trigger(nodes.openDashboardFilters, 'click');
    trigger(nodes.closeDashboardFilters, 'click');
    assert.equal(nodes.dashboardFiltersPanel.hidden, true);
    assert.equal(nodes.openDashboardFilters.hidden, false);
});

test('viewer modes hide filters when no controls are enabled or supported', () => {
    const viewer = filterUI(false, []);
    assert.equal(viewer.nodes.openDashboardFilters.hidden, true);
    viewer.trigger(viewer.nodes.openDashboardFilters, 'click');
    assert.equal(viewer.nodes.dashboardFiltersPanel.hidden, true);
    const editor = filterUI(true, []);
    editor.trigger(editor.nodes.openDashboardFilters, 'click');
    editor.api.setPreview(true);
    assert.equal(editor.nodes.openDashboardFilters.hidden, true);
    assert.equal(editor.nodes.dashboardFiltersPanel.hidden, true);
    editor.api.setPreview(false);
    assert.equal(editor.nodes.dashboardFiltersPanel.hidden, false);
    const unsupported = filterUI(false);
    unsupported.api.configure([{supported: []}], false);
    assert.equal(unsupported.nodes.openDashboardFilters.hidden, true);
});

test('collapsed tab counts active dimensions, not selected records, and reset clears it', async () => {
    const {api, nodes, trigger} = filterUI(false);
    api.restore({scope: {seller_ids: [7]}, live_filters: ['customer_ids', 'seller_ids']});
    const before = JSON.stringify(api.context().dashboard);
    trigger(nodes.openDashboardFilters, 'click');
    const customer = nodes.dashboardLiveControls.children[0];
    for (const row of customer.children[1].children[2].children) {
        const checkbox = row.children[0];
        checkbox.checked = true;
        trigger(checkbox, 'change');
    }
    assert.equal(nodes.dashboardFilterCount.textContent, '1');
    const seller = nodes.dashboardLiveControls.children[1].children[1]
        .children[2].children[0].children[0];
    seller.checked = true;
    trigger(seller, 'change');
    trigger(nodes.closeDashboardFilters, 'click');
    assert.equal(nodes.openDashboardFilters.hidden, false);
    assert.equal(nodes.dashboardFilterCount.textContent, '2');
    assert.equal(nodes.dashboardFilterCount.hidden, false);
    assert.equal(nodes.openDashboardFilters.attributes['aria-label'], 'Filters, 2 active');
    assert.equal(nodes.openDashboardFilters.classList.values.has('is-active'), true);
    assert.equal(JSON.stringify(api.context().dashboard), before);
    trigger(nodes.openDashboardFilters, 'click');
    trigger(nodes.resetDashboardFilters, 'click');
    assert.equal(nodes.dashboardFilterCount.hidden, true);
    assert.equal(nodes.openDashboardFilters.attributes['aria-label'], 'Filters');
    assert.equal(nodes.openDashboardFilters.classList.values.has('is-active'), false);
    assert.equal(nodes.resetDashboardFilters.disabled, true);
    assert.equal(nodes.dashboardViewerScopeText.textContent, 'Seller: First owner');
    assert.equal(JSON.stringify(api.context().dashboard), before);
    await new Promise(setImmediate);
});

test('filter refresh failure keeps active indication and explicit retry guidance', async () => {
    const {api, nodes, trigger, editor} = filterUI();
    api.setPreview(true);
    editor.refreshFilters = async () => { throw new Error('Refresh unavailable'); };
    const customer = nodes.dashboardLiveControls.children[0].children[1]
        .children[2].children[0].children[0];
    customer.checked = true;
    trigger(customer, 'change');
    await new Promise(setImmediate);
    assert.equal(nodes.dashboardFilterCount.textContent, '1');
    assert.equal(nodes.dashboardFilterCount.hidden, false);
    assert.match(nodes.dashboardFilterStatus.textContent, /previous selections/);
    assert.equal(nodes.dashboardFilterStatus.hidden, false);
    editor.refreshFilters = async () => {};
    trigger(nodes.resetDashboardFilters, 'click');
    await new Promise(setImmediate);
    assert.equal(nodes.dashboardFilterCount.hidden, true);
    assert.equal(nodes.dashboardFilterStatus.hidden, true);
});
