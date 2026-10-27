(function (global) {
    'use strict';

    function displayName(customer) {
        return customer.name + (customer.nickname ? ' (' + customer.nickname + ')' : '');
    }

    function formatRevenue(amount) {
        return '$' + (amount >= 1000 ? (amount / 1000).toFixed(1) + 'K' : Math.round(amount));
    }

    function visibleBuckets(buckets, focus = '') {
        return buckets.filter(bucket => bucket !== focus);
    }

    function visibleCustomers(customers, buckets, showAll, focus = '', qualifyingBuckets = null) {
        return customers.filter(customer => {
            const coverage = Object.keys(customer.buckets).length;
            if (focus) return !Object.hasOwn(customer.buckets, focus) && (qualifyingBuckets
                ? qualifyingBuckets.some(bucket => Object.hasOwn(customer.buckets, bucket))
                : coverage > 0);
            return showAll || buckets.length === 1 || (coverage > 0 && coverage < buckets.length);
        }).sort((a, b) => Object.keys(a.buckets).length - Object.keys(b.buckets).length
            || a.name.localeCompare(b.name));
    }

    function matchingCustomers(customers, query) {
        const term = query.trim().toLowerCase();
        if (term.length < 2) return [];
        return customers.filter(customer => [customer.name, customer.nickname, customer.tpid]
            .some(value => value != null && String(value).toLowerCase().includes(term))).slice(0, 15);
    }

    function sellerGroups(items) {
        const groups = new Map();
        items.forEach(item => {
            const key = item.seller?.id ?? null;
            if (!groups.has(key)) groups.set(key, {seller: item.seller, rows: []});
            groups.get(key).rows.push(item);
        });
        return [...groups.values()].sort((a, b) =>
            (a.seller == null) - (b.seller == null)
            || (a.seller?.name ?? '').localeCompare(b.seller?.name ?? '')
            || (a.seller?.id ?? 0) - (b.seller?.id ?? 0));
    }

    function createLatestReader(fetcher) {
        const requests = new Map();
        return {
            cancel(key) {
                requests.get(key)?.abort();
                requests.delete(key);
            },
            async read(key, url) {
                this.cancel(key);
                const controller = new AbortController();
                requests.set(key, controller);
                try {
                    const response = await fetcher(url, {signal: controller.signal});
                    if (!response.ok) throw new Error('Request failed (' + response.status + ')');
                    const data = await response.json();
                    return requests.get(key) === controller ? {data} : {stale: true};
                } catch (error) {
                    if (requests.get(key) !== controller || controller.signal.aborted) {
                        return {stale: true};
                    }
                    throw error;
                }
            },
        };
    }

    function requireCustomers(data) {
        if (!Array.isArray(data) || data.some(customer => !Number.isInteger(customer.id)
            || customer.id <= 0 || typeof customer.name !== 'string')) {
            throw new Error('Invalid customer response');
        }
        return data;
    }

    function requireStrings(data) {
        if (!Array.isArray(data) || data.some(value => typeof value !== 'string')) {
            throw new Error('Invalid pillar or product response');
        }
        return data;
    }

    function requireGrid(data, buckets) {
        requireCustomers(data.customers);
        requireStrings(data.buckets);
        if (JSON.stringify(data.buckets) !== JSON.stringify(buckets)
            || data.customers.some(customer => !customer.buckets
                || Array.isArray(customer.buckets)
                || (customer.seller != null && (!Number.isInteger(customer.seller.id)
                    || customer.seller.id <= 0 || typeof customer.seller.name !== 'string'))
                || Object.entries(customer.buckets).some(([bucket, amount]) =>
                    !buckets.includes(bucket) || !Number.isFinite(amount)))) {
            throw new Error('Invalid whitespace grid response');
        }
        return data;
    }

    const PREFERENCES_KEY = 'salesbuddy_whitespace_preferences';

    function entryPreset(search) {
        if (new URLSearchParams(search).get('view') !== 'database-whitespace') return null;
        return {
            key: 'database-whitespace-v2',
            pillars: ['AI', 'Analytics', 'Apps', 'Databases'],
            sortPillar: 'Databases',
            focus: 'Databases',
        };
    }

    function parsePreferences(raw) {
        if (raw === null) return {version: 1, sorts: {}};
        const value = JSON.parse(raw);
        if (!value || Array.isArray(value) || value.version !== 1 ||
            !value.sorts || typeof value.sorts !== 'object' || Array.isArray(value.sorts)) {
            throw new Error('Invalid whitespace preferences');
        }
        if (value.pillars !== undefined) requireStrings(value.pillars);
        if (value.focus !== undefined && typeof value.focus !== 'string') {
            throw new Error('Invalid whitespace focus preference');
        }
        for (const [key, state] of Object.entries(value.sorts)) {
            const match = /^whitespace:(gridTable|penetrationTable):(.+)$/.exec(key);
            const columns = match ? JSON.parse(match[2]) : null;
            if (!Array.isArray(columns) || !columns.length ||
                columns.some(column => !Array.isArray(column) || column.length !== 2 ||
                    typeof column[0] !== 'string' || !['text', 'number'].includes(column[1])) ||
                (state !== null && (!state || !Number.isInteger(state.column) ||
                    state.column < 0 || state.column >= columns.length ||
                    !['ascending', 'descending'].includes(state.direction)))) {
                throw new Error('Invalid saved whitespace sort');
            }
        }
        return value;
    }

    const helpers = {displayName, formatRevenue, visibleBuckets, visibleCustomers, matchingCustomers,
        createLatestReader, requireGrid, parsePreferences, sellerGroups, entryPreset};
    if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
    if (!global.document) return;
    const root = document.querySelector('[data-sb-whitespace]');
    if (!root?.querySelector('[data-mode]')) return;

    const find = selector => root.querySelector(selector);
    const reader = createLatestReader(global.fetch.bind(global));
    const status = document.querySelector('[data-sb-whitespace-status]');
    const search = find('#reverseCustomerSearch');
    const searchResults = find('#reverseSearchResults');
    const dialog = find('#bucketDetailModal');
    const frame = find('[data-bucket-frame]');
    const retries = {};
    const sorts = {};
    let acquisitionTemplate = find('#fabricAcquisitionTable')?.cloneNode(true);
    let preferences = parsePreferences(null);
    const restoredReadings = new Set();
    let availableBuckets = [];
    let grid = null;
    let gridRequest = [];
    let customers = null;
    let matches = [];
    let activeMatch = -1;
    let selectedCustomer = null;
    let penetrationLoaded = false;
    let mode = 'grid';
    let detail = null;
    let searchVersion = 0;
    let frameTimer = null;
    let defaultsVersion = 0;
    const saved = history.state?.sbWhitespace?.dimension === 'SuperStrategicPillar'
        ? history.state.sbWhitespace : {};
    const preset = entryPreset(global.location.search);
    const restoringPreset = preset && saved.entry === preset.key;

    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function selectedBuckets() {
        return Array.from(find('#bucketCheckboxes').querySelectorAll('input:checked'),
            input => input.value);
    }

    function updateStatus(message) {
        if (status) status.textContent = message;
    }

    function setLoading(key, loading) {
        const node = find('[data-loading="' + key + '"]');
        if (node) node.hidden = !loading;
        const panel = find('#' + key + 'Panel');
        if (panel) panel.setAttribute('aria-busy', String(loading));
    }

    function showError(key, error) {
        console.error('Whitespace ' + key + ' failed:', error);
        const notice = find('[data-error="' + key + '"]');
        notice.querySelector('[role="alert"]').textContent =
            'Could not load ' + (key === 'defaults' ? 'strategic pillars' : key) + '. '
            + (key === 'defaults' ? 'Retry loading the pillars.' : 'Please retry.');
        notice.hidden = false;
        if (key === mode) updateStatus('Could not load ' + key + ' | Retry available');
        if (key === 'grid') find('#gridSummary').textContent =
            'Grid unavailable | ' + gridRequest.join(', ');
        if (key === 'defaults' && !availableBuckets.length) {
            find('#bucketCheckboxes').textContent = 'Pillars unavailable.';
        }
    }

    async function read(key, url, accept) {
        find('[data-error="' + key + '"]').hidden = true;
        setLoading(key, true);
        try {
            const result = await reader.read(key, url);
            if (result.stale) return false;
            await accept(result.data);
            setLoading(key, false);
            return true;
        } catch (error) {
            setLoading(key, false);
            showError(key, error);
            return false;
        }
    }

    function saveView() {
        try {
            const scrolls = {};
            root.querySelectorAll('.sb-whitespace-panel').forEach(panel => {
                scrolls[panel.id] = panel.querySelector('.sb-whitespace-reading').scrollTop;
            });
            history.replaceState({...history.state, sbWhitespace: {
                dimension: 'SuperStrategicPillar',
                entry: preset?.key ?? null,
                mode, buckets: selectedBuckets(),
                focus: find('#whitespaceFocus').value,
                showAll: find('#showAllCustomers').checked,
                groupBySeller: {
                    grid: find('#gridGroupBySeller').checked,
                    acquisition: find('#acquisitionGroupBySeller')?.checked ?? false,
                },
                customer: selectedCustomer, query: search.value, scrolls,
                sorts: Object.fromEntries(Object.entries(sorts).map(([key, sort]) =>
                    [key, sort.state])),
            }}, '');
        } catch (error) {
            showError('defaults', error);
            find('[data-error="defaults"] [role="alert"]').textContent =
                'Could not preserve this view for Back navigation.';
        }
    }

    function preferenceError(action, error) {
        console.error('Whitespace preferences ' + action + ' failed:', error);
        const notice = find('[data-error="preferences"]');
        notice.querySelector('[role="alert"]').textContent = action === 'restore'
            ? 'Saved pillar selections and sorting could not be restored. Current settings are shown.'
            : 'Pillar selections and sorting could not be saved on this device.';
        notice.querySelector('button').textContent = action === 'restore'
            ? 'Retry loading preferences' : 'Retry saving preferences';
        retries.preferences = action === 'restore' ? () => {
            if (restorePreferences()) {
                loadDefaults();
                if (penetrationLoaded) loadPenetration();
            }
        } : savePreferences;
        notice.hidden = false;
    }

    function restorePreferences() {
        try {
            preferences = parsePreferences(global.localStorage.getItem(PREFERENCES_KEY));
            find('[data-error="preferences"]').hidden = true;
            return true;
        } catch (error) {
            preferenceError('restore', error);
            return false;
        }
    }

    function savePreferences() {
        if (preset) return;
        const pillars = availableBuckets.length ? selectedBuckets() : preferences.pillars;
        preferences = {
            version: 1,
            ...(pillars === undefined ? {} : {pillars}),
            focus: find('#whitespaceFocus').value,
            sorts: {...preferences.sorts, ...Object.fromEntries(
                Object.entries(sorts).map(([key, sort]) => [key, sort.state]))},
        };
        try {
            global.localStorage.setItem(PREFERENCES_KEY, JSON.stringify(preferences));
            find('[data-error="preferences"]').hidden = true;
        } catch (error) {
            preferenceError('save', error);
        }
    }

    function restoreReading(key) {
        const panel = find('#' + key + 'Panel');
        if (panel && !panel.hidden && !restoredReadings.has(key)) {
            panel.querySelector('.sb-whitespace-reading').scrollTop =
                saved.scrolls?.[panel.id] || 0;
            restoredReadings.add(key);
        }
    }

    function setTableBodies(table, rows, grouped) {
        const items = rows.map(row => ({
            row,
            seller: row.dataset.sellerId ? {
                id: Number(row.dataset.sellerId), name: row.dataset.sellerName,
            } : null,
        }));
        const groups = grouped ? sellerGroups(items) : [{rows: items}];
        for (const group of groups) {
            const body = table.createTBody();
            if (grouped) {
                const heading = element('tr', undefined, 'sb-whitespace-seller-heading');
                heading.setAttribute('data-sb-sort-fixed', '');
                const label = element('th');
                label.colSpan = table.tHead.rows[0].cells.length;
                label.scope = 'rowgroup';
                if (group.seller) {
                    const link = element('a', undefined,
                        'badge bg-primary text-decoration-none');
                    link.href = '/seller/' + group.seller.id;
                    const icon = element('i', undefined, 'bi bi-person');
                    icon.setAttribute('aria-hidden', 'true');
                    link.append(icon, document.createTextNode(' ' + group.seller.name));
                    label.append(link);
                } else label.textContent = 'No seller assigned';
                heading.append(label);
                body.append(heading);
            }
            group.rows.forEach(item => body.append(item.row));
        }
        if (!table.tBodies.length) table.createTBody();
    }

    function makeTable(id, columns, host, rows, grouped = false) {
        const key = 'whitespace:' + id + ':' + JSON.stringify(columns);
        const presetGrid = id === 'gridTable' && preset;
        const previousSort = presetGrid
            ? Object.hasOwn(sorts, key) ? sorts[key].state :
                restoringPreset ? saved.sorts?.[key] : undefined
            : Object.hasOwn(preferences.sorts, key) ? preferences.sorts[key] :
                Object.hasOwn(sorts, key) ? sorts[key].state : saved.sorts?.[key];
        const table = element('table');
        table.id = id;
        table.setAttribute('data-sb-sortable', '');
        table.dataset.sbSortKey = key;
        const head = table.createTHead().insertRow();
        columns.forEach(([label, type]) => {
            const th = element('th', label);
            th.scope = 'col';
            th.dataset.sbSortType = type;
            th.dataset.exportLabel = label;
            if (presetGrid && label === preset.sortPillar) th.dataset.sbSortEmptyFirst = '';
            head.append(th);
        });
        setTableBodies(table, rows, grouped);
        host.replaceChildren(table);
        sorts[key] = global.SBTableSort.create(table);
        if (previousSort !== undefined) sorts[key].restore(previousSort);
        else if (presetGrid) {
            const column = columns.findIndex(([label]) => label === preset.sortPillar);
            if (column >= 0) sorts[key].restore({column, direction: 'ascending'});
        }
        table.addEventListener('click', event => {
            if (event.target.closest('[data-sb-table-sort]')) savePreferences();
        });
        return table;
    }

    function renderAcquisition() {
        if (!acquisitionTemplate) return;
        const previous = find('#fabricAcquisitionTable');
        const state = previous ? global.SBTableSort.create(previous).state : null;
        const table = acquisitionTemplate.cloneNode(true);
        const rows = [...table.querySelectorAll('tr[data-row-key]')];
        table.querySelectorAll('thead th').forEach(header => {
            header.textContent = header.textContent.trim();
        });
        [...table.tBodies].forEach(body => body.remove());
        setTableBodies(table, rows, find('#acquisitionGroupBySeller').checked);
        table.dataset.exportVisible = String(mode === 'acquisition');
        const reading = find('#acquisitionPanel .sb-whitespace-reading');
        const scrollTop = reading.scrollTop;
        reading.replaceChildren(table);
        global.SBTableSort.create(table).restore(state);
        reading.scrollTop = scrollTop;
        if (!rows.length) {
            reading.replaceChildren(element('p', 'No customers match the acquisition criteria.',
                'sb-whitespace-empty'));
        }
    }

    function loadAcquisition() {
        const reading = find('#acquisitionPanel .sb-whitespace-reading');
        reading.hidden = true;
        const include = find('#acquisitionIncludeNoOtherSpend').checked;
        return read('acquisition', '/api/reports/whitespace/fabric-acquisition?'
            + 'include_no_other_spend=' + include, data => {
            const fragment = document.createElement('template');
            fragment.innerHTML = data.table_html;
            acquisitionTemplate = fragment.content.querySelector('#fabricAcquisitionTable');
            if (!acquisitionTemplate) throw new Error('Invalid acquisition table response');
            renderAcquisition();
            reading.hidden = false;
            const summary = data.count + ' customers | Avg monthly ACR, ' + data.period_label;
            find('#acquisitionSummary').textContent = summary;
            if (mode === 'acquisition') updateStatus(summary);
        });
    }

    function cell(row, text, value) {
        const td = element('td', text);
        td.dataset.sbSortValue = value === undefined ? text : String(value);
        row.append(td);
        return td;
    }

    function customerLink(customer) {
        const link = element('a', displayName(customer));
        link.href = '/customer/' + customer.id + '/revenue';
        return link;
    }

    function renderGrid() {
        if (!grid) return;
        const focus = find('#whitespaceFocus').value;
        const buckets = visibleBuckets(selectedBuckets(), focus);
        const rows = visibleCustomers(grid.customers, buckets,
            find('#showAllCustomers').checked, focus,
            preset && focus === preset.focus ? preset.pillars : null);
        const host = find('#gridResults');
        const table = makeTable('gridTable', [['Customer', 'text'],
            ...buckets.map(bucket => [bucket, 'number']), ['Gaps', 'number']], host,
        rows.map(customer => {
            const row = element('tr');
            row.dataset.customerId = customer.id;
            if (customer.seller) {
                row.dataset.sellerId = customer.seller.id;
                row.dataset.sellerName = customer.seller.name;
            }
            cell(row, '', customer.name).append(customerLink(customer));
            buckets.forEach(bucket => {
                const amount = customer.buckets[bucket];
                const td = cell(row, '', amount);
                if (amount === undefined) {
                    td.textContent = 'No spend';
                    td.dataset.sbSortValue = '';
                    td.className = 'sb-whitespace-gap';
                } else {
                    const button = element('button', formatRevenue(amount), 'sb-whitespace-value');
                    td.dataset.exportLabel = formatRevenue(amount);
                    button.type = 'button';
                    button.dataset.sbRevenueBreakdown = JSON.stringify(
                        customer.product_breakdown[bucket]);
                    button.dataset.revenueLabel = bucket;
                    button.dataset.revenuePeriod = grid.period_label;
                    button.setAttribute('aria-label', bucket + ': ' + formatRevenue(amount)
                        + ' average monthly ACR. Click for full product history.');
                    button.addEventListener('click', () => openBucket(customer, bucket));
                    td.append(button);
                }
            });
            cell(row, buckets.filter(bucket => !Object.hasOwn(customer.buckets, bucket)).length);
            return row;
        }), find('#gridGroupBySeller').checked);
        table.dataset.exportVisible = String(rows.length > 0 && mode !== 'acquisition');
        host.hidden = rows.length === 0;
        find('#gridEmpty').hidden = rows.length > 0;
        find('#gridEmpty').textContent = 'No customers match this pillar scope.';
        const scope = rows.length + ' customers'
            + (focus ? ' | ' + focus + ' whitespace' : '')
            + (buckets.length ? ' | ' + buckets.join(', ') : '')
            + ' | Avg monthly ACR, ' + grid.period_label + '; total divided by 3';
        find('#gridSummary').textContent = scope;
        if (mode === 'grid') updateStatus(scope);
        restoreReading('grid');
    }

    function loadGrid(buckets = selectedBuckets()) {
        const focused = Boolean(find('#whitespaceFocus').value);
        find('#showAllCustomers').disabled = focused;
        if (focused) buckets = availableBuckets;
        if (!buckets.length) {
            reader.cancel('grid');
            grid = null;
            gridRequest = [];
            find('#gridResults').replaceChildren();
            find('#gridResults').hidden = true;
            find('#gridEmpty').hidden = false;
            find('#gridEmpty').textContent = 'Choose at least one pillar to see whitespace.';
            find('#gridSummary').textContent = 'No pillar scope loaded.';
            find('[data-error="grid"]').hidden = true;
            setLoading('grid', false);
            if (mode === 'grid') updateStatus('Choose pillars to begin');
            return;
        }
        gridRequest = [...buckets];
        grid = null;
        find('#gridResults').hidden = true;
        find('#gridResults table')?.setAttribute('data-export-visible', 'false');
        find('#gridEmpty').hidden = true;
        find('#gridSummary').textContent = 'Loading | ' + buckets.join(', ');
        if (mode === 'grid') updateStatus('Loading whitespace grid');
        retries.grid = () => loadGrid(gridRequest);
        return read('grid', '/api/reports/whitespace?buckets='
            + encodeURIComponent(buckets.join(',')) + '&show_all=1', data => {
            grid = requireGrid(data, buckets);
            renderGrid();
        });
    }

    async function loadDefaults() {
        const version = ++defaultsVersion;
        const loaded = await read('defaults', '/api/reports/whitespace/pillars', data => {
            availableBuckets = requireStrings(data);
            const host = find('#bucketCheckboxes');
            const focus = find('#whitespaceFocus');
            const any = element('option', 'Any selected pillar');
            any.value = '';
            focus.replaceChildren(any);
            host.replaceChildren();
            availableBuckets.forEach(bucket => {
                const option = element('option', bucket);
                option.value = bucket;
                focus.append(option);
                const label = element('label');
                const input = element('input');
                input.type = 'checkbox';
                input.value = bucket;
                label.append(input, document.createTextNode(bucket));
                host.append(label);
            });
            if (!availableBuckets.length) {
                loadGrid([]);
                host.append(element('span', 'No strategic pillars available.'));
                find('#gridEmpty').textContent = 'No pillars available in imported revenue.';
            }
        });
        if (!loaded || version !== defaultsVersion || !availableBuckets.length) return;
        try {
            const defaults = preset
                ? restoringPreset && Array.isArray(saved.buckets)
                    ? requireStrings(saved.buckets) : preset.pillars
                : preferences.pillars ?? (Array.isArray(saved.buckets)
                    ? requireStrings(saved.buckets) : availableBuckets);
            find('#bucketCheckboxes').querySelectorAll('input').forEach(input => {
                input.checked = defaults.includes(input.value);
            });
            const focus = preset
                ? restoringPreset ? saved.focus ?? preset.focus : preset.focus
                : preferences.focus ?? saved.focus ?? '';
            if (focus && !availableBuckets.includes(focus)) {
                throw new Error('Saved whitespace focus is not an available pillar');
            }
            find('#whitespaceFocus').value = focus;
            find('#showAllCustomers').checked = preset && !restoringPreset
                ? true : Boolean(saved.showAll);
            await loadGrid();
        } catch (error) {
            showError('defaults', error);
        }
    }

    function hideMatches() {
        searchResults.hidden = true;
        search.setAttribute('aria-expanded', 'false');
        search.removeAttribute('aria-activedescendant');
        activeMatch = -1;
    }

    async function searchCustomers() {
        const version = ++searchVersion;
        hideMatches();
        if (search.value.trim().length < 2) return;
        if (!customers) {
            await read('search', '/api/customers', data => { customers = requireCustomers(data); });
        }
        if (version !== searchVersion || !customers || mode !== 'reverse') return;
        matches = matchingCustomers(customers, search.value);
        searchResults.replaceChildren();
        matches.forEach((customer, index) => {
            const button = element('button', displayName(customer)
                + (customer.tpid ? ' | ' + customer.tpid : ''));
            button.type = 'button';
            button.tabIndex = -1;
            button.id = 'whitespaceCustomer' + index;
            button.setAttribute('role', 'option');
            button.setAttribute('aria-selected', 'false');
            button.addEventListener('click', () => chooseCustomer(customer));
            searchResults.append(button);
        });
        if (!matches.length) searchResults.append(element('p', 'No customers found.'));
        searchResults.hidden = false;
        search.setAttribute('aria-expanded', 'true');
    }

    function chooseCustomer(customer) {
        ++searchVersion;
        selectedCustomer = customer;
        search.value = customer.name;
        hideMatches();
        search.focus();
        loadReverse();
    }

    function tags(values, missing) {
        const container = element('div', undefined, 'sb-whitespace-tags');
        container.dataset.missing = String(missing);
        values.forEach(value => container.append(element('span', value)));
        return container;
    }

    function loadReverse() {
        if (!selectedCustomer) return;
        find('#reverseResults').hidden = true;
        retries.reverse = loadReverse;
        return read('reverse', '/api/reports/whitespace/reverse/' + selectedCustomer.id, data => {
            requireCustomers([data.customer]);
            if (data.customer.id !== selectedCustomer.id) {
                throw new Error('Customer analysis does not match the selection');
            }
            const missingBuckets = requireStrings(data.missing_buckets);
            if (!data.missing_products || Array.isArray(data.missing_products)) {
                throw new Error('Invalid product gaps response');
            }
            Object.values(data.missing_products).forEach(requireStrings);
            const host = find('#reverseResults');
            const context = element('section', undefined, 'sb-whitespace-reverse-section');
            context.append(element('h2', data.customer.name), customerLink(data.customer),
                element('p', 'Coverage uses positive spend at any time in imported history.'));
            const bucketSection = element('section', undefined, 'sb-whitespace-reverse-section');
            bucketSection.append(element('h2', 'Missing pillars (' + missingBuckets.length + ')'),
                missingBuckets.length ? tags(missingBuckets, true)
                    : element('p', 'Customer has used every strategic pillar.'));
            const productSection = element('section', undefined, 'sb-whitespace-reverse-section');
            productSection.append(element('h2', 'Missing products within active pillars'));
            const entries = Object.entries(data.missing_products);
            entries.forEach(([bucket, products]) => productSection.append(
                element('h3', bucket + ' (' + products.length + ')'), tags(products, false)));
            if (!entries.length) productSection.append(element('p', 'No product gaps in active pillars.'));
            host.replaceChildren(context, bucketSection, productSection);
            host.hidden = false;
            if (mode === 'reverse') updateStatus(data.customer.name + ' | Historical coverage');
            restoreReading('reverse');
        });
    }

    function loadPenetration() {
        find('#penetrationResults').hidden = true;
        retries.penetration = loadPenetration;
        return read('penetration', '/api/reports/whitespace/penetration', data => {
            if (!Array.isArray(data) || data.some(row => typeof row.bucket !== 'string'
                || !Number.isInteger(row.total_customers)
                || !Number.isInteger(row.customers_with_spend)
                || row.customers_with_spend < 0 || row.customers_with_spend > row.total_customers
                || !Number.isFinite(row.penetration_pct)
                || row.penetration_pct < 0 || row.penetration_pct > 100)) {
                throw new Error('Invalid penetration response');
            }
            data.sort((a, b) => a.penetration_pct - b.penetration_pct);
            const host = find('#penetrationResults');
            if (!data.length) host.replaceChildren(element('p', 'No customer revenue available.',
                'sb-whitespace-empty'));
            else makeTable('penetrationTable', [['Pillar', 'text'], ['Customers with Spend', 'number'],
                ['Adoption Rate', 'number'], ['Gap', 'number']], host, data.map(item => {
                const row = element('tr');
                const button = element('button', item.bucket, 'sb-whitespace-value');
                button.type = 'button';
                button.addEventListener('click', () => openPenetration(item.bucket));
                cell(row, '', item.bucket).append(button);
                cell(row, item.customers_with_spend + ' / ' + item.total_customers,
                    item.customers_with_spend);
                const rate = element('span', undefined, 'sb-whitespace-rate');
                rate.dataset.tone = item.penetration_pct >= 75 ? 'positive'
                    : item.penetration_pct >= 50 ? 'neutral'
                        : item.penetration_pct >= 25 ? 'warning' : 'danger';
                const track = element('span');
                track.setAttribute('aria-hidden', 'true');
                const bar = element('i');
                bar.style.width = item.penetration_pct + '%';
                track.append(bar);
                rate.append(track, document.createTextNode(item.penetration_pct.toFixed(1) + '%'));
                cell(row, '', item.penetration_pct).append(rate);
                cell(row, item.total_customers - item.customers_with_spend);
                return row;
            }));
            host.hidden = false;
            penetrationLoaded = true;
            find('#penetrationSummary').textContent = data.length
                ? data.length + ' pillars | ' + data[0].total_customers
                    + ' customers with imported revenue | Historical positive spend'
                : 'No customer revenue available.';
            if (mode === 'penetration') updateStatus(find('#penetrationSummary').textContent);
            restoreReading('penetration');
        });
    }

    function prepareDetail(title) {
        reader.cancel('detail');
        clearTimeout(frameTimer);
        frame.removeAttribute('src');
        frame.hidden = true;
        find('[data-bucket-full]').hidden = true;
        find('#bucketDetailBody').replaceChildren();
        find('[data-error="detail"]').hidden = true;
        find('#bucketDetailTitle').textContent = title;
        global.SBDialog.open(dialog);
    }

    function openBucket(customer, bucket) {
        prepareDetail(customer.name + ' | ' + bucket);
        const url = '/reports/whitespace/customer/' + customer.id + '/pillar/'
            + encodeURIComponent(bucket);
        detail = {kind: 'bucket', url};
        retries.detail = () => openBucket(customer, bucket);
        find('[data-bucket-full]').href = url;
        find('[data-bucket-full]').hidden = false;
        setLoading('detail', true);
        frame.src = url + '?embed=1';
        frameTimer = setTimeout(() => {
            frame.removeAttribute('src');
            setLoading('detail', false);
            showError('detail', new Error('Pillar preview timed out'));
        }, 30000);
    }

    frame.addEventListener('load', () => {
        if (detail?.kind !== 'bucket' || !dialog.open || !frame.hasAttribute('src')) return;
        try {
            if (frame.contentWindow.location.href !== frame.src) return;
            clearTimeout(frameTimer);
            if (!frame.contentDocument.querySelector('[data-sb-pillar-products][data-embedded="true"]')) {
                throw new Error('Pillar preview could not be loaded');
            }
            frame.contentDocument.querySelectorAll('a[href]').forEach(link => {
                if (!link.getAttribute('href').startsWith('#')) link.target = '_top';
            });
            frame.hidden = false;
            setLoading('detail', false);
        } catch (error) {
            setLoading('detail', false);
            showError('detail', error);
        }
    });

    function openPenetration(bucket) {
        prepareDetail(bucket + ' | Customer adoption');
        detail = {kind: 'penetration', bucket};
        retries.detail = () => openPenetration(bucket);
        return read('detail', '/api/reports/whitespace/penetration/customers?bucket='
            + encodeURIComponent(bucket), data => {
            const withSpend = requireCustomers(data.with_spend);
            const withoutSpend = requireCustomers(data.without_spend);
            const host = find('#bucketDetailBody');
            [[withSpend, 'With spend', true], [withoutSpend, 'Without spend', false]]
                .forEach(([list, label, open]) => {
                    const section = element('details');
                    section.open = open;
                    section.append(element('summary', label + ' (' + list.length + ')'));
                    const ul = element('ul');
                    list.forEach(customer => {
                        const li = element('li');
                        li.append(customerLink(customer));
                        ul.append(li);
                    });
                    if (!list.length) section.append(element('p', 'No customers.'));
                    section.append(ul);
                    host.append(section);
                });
        });
    }

    function setMode(next, focus = false) {
        const modes = Array.from(root.querySelectorAll('[data-mode]'), button => button.dataset.mode);
        if (!modes.includes(next)) next = 'grid';
        mode = next;
        root.querySelectorAll('[data-mode]').forEach(button => {
            const active = button.dataset.mode === mode;
            button.setAttribute('aria-selected', String(active));
            button.tabIndex = active ? 0 : -1;
            find('#' + button.dataset.mode + 'Panel').hidden = !active;
            if (active && focus) button.focus();
        });
        if (mode === 'penetration' && !penetrationLoaded) loadPenetration();
        find('#fabricAcquisitionTable')?.setAttribute(
            'data-export-visible', String(mode === 'acquisition'));
        if (mode === 'acquisition') {
            find('#gridTable')?.setAttribute('data-export-visible', 'false');
            updateStatus(find('#acquisitionSummary').textContent);
            restoreReading(mode);
            return;
        }
        if (grid) renderGrid();
        if ((mode === 'grid' && grid) || (mode === 'penetration' && penetrationLoaded)
            || (mode === 'reverse' && !find('#reverseResults').hidden
                && find('#reverseResults .sb-whitespace-reverse-section'))) {
            restoreReading(mode);
        }
        updateStatus(mode === 'grid' ? find('#gridSummary').textContent
            : mode === 'penetration' ? find('#penetrationSummary').textContent
                : selectedCustomer ? selectedCustomer.name + ' | Historical coverage'
                    : 'Choose a customer');
    }

    root.querySelectorAll('[data-mode]').forEach(button => {
        button.addEventListener('click', () => setMode(button.dataset.mode));
        button.addEventListener('keydown', event => {
            const modes = Array.from(root.querySelectorAll('[data-mode]'),
                item => item.dataset.mode);
            const index = modes.indexOf(mode);
            let next;
            if (event.key === 'ArrowRight') next = modes[(index + 1) % modes.length];
            if (event.key === 'ArrowLeft') next = modes[(index + modes.length - 1) % modes.length];
            if (event.key === 'Home') next = modes[0];
            if (event.key === 'End') next = modes[modes.length - 1];
            if (next) { event.preventDefault(); setMode(next, true); }
        });
    });
    function cancelDefaults() {
        ++defaultsVersion;
        reader.cancel('defaults');
    }
    find('#bucketCheckboxes').addEventListener('change', () => {
        cancelDefaults();
        savePreferences();
        loadGrid();
    });
    find('#whitespaceFocus').addEventListener('change', () => {
        cancelDefaults();
        savePreferences();
        loadGrid();
    });
    find('#showAllCustomers').addEventListener('change', renderGrid);
    find('#gridGroupBySeller').addEventListener('change', () => {
        renderGrid();
        saveView();
    });
    find('#acquisitionGroupBySeller')?.addEventListener('change', () => {
        renderAcquisition();
        saveView();
    });
    find('#acquisitionIncludeNoOtherSpend')?.addEventListener('change', loadAcquisition);
    retries.acquisition = loadAcquisition;
    document.addEventListener('DOMContentLoaded', renderAcquisition);
    search.addEventListener('input', searchCustomers);
    search.addEventListener('keydown', event => {
        if (event.key === 'Escape') { ++searchVersion; hideMatches(); }
        if (searchResults.hidden) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (!matches.length) return;
            activeMatch = activeMatch < 0
                ? event.key === 'ArrowDown' ? 0 : matches.length - 1
                : (activeMatch + (event.key === 'ArrowDown' ? 1 : -1)
                    + matches.length) % matches.length;
            searchResults.querySelectorAll('[role="option"]').forEach((option, index) => {
                option.setAttribute('aria-selected', String(index === activeMatch));
                if (index === activeMatch) {
                    search.setAttribute('aria-activedescendant', option.id);
                    option.scrollIntoView({block: 'nearest'});
                }
            });
        } else if (event.key === 'Enter') {
            event.preventDefault();
            if (matches.length) chooseCustomer(matches[Math.max(0, activeMatch)]);
        }
    });
    document.addEventListener('click', event => {
        if (!event.target.closest('.sb-whitespace-search')) { ++searchVersion; hideMatches(); }
    });
    dialog.addEventListener('sb:dialog-closed', () => {
        reader.cancel('detail');
        clearTimeout(frameTimer);
        detail = null;
        frame.removeAttribute('src');
    });
    retries.defaults = loadDefaults;
    retries.search = searchCustomers;
    root.querySelectorAll('[data-retry]').forEach(button => {
        button.addEventListener('click', () => retries[button.dataset.retry]?.());
    });
    global.addEventListener('pagehide', saveView);
    document.addEventListener('click', event => {
        if (event.target.closest('a[href]')) saveView();
    }, true);
    search.value = typeof saved.query === 'string' ? saved.query : '';
    if (saved.customer && Number.isInteger(saved.customer.id)) {
        selectedCustomer = saved.customer;
        loadReverse();
    }
    find('#gridGroupBySeller').checked = saved.groupBySeller?.grid === true;
    if (find('#acquisitionGroupBySeller')) {
        find('#acquisitionGroupBySeller').checked = saved.groupBySeller?.acquisition === true;
    }
    setMode(preset && !restoringPreset ? 'grid' : saved.mode || root.dataset.initialMode || 'grid');
    restorePreferences();
    loadDefaults();
})(typeof window === 'undefined' ? globalThis : window);
