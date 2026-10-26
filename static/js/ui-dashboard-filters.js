(function (global) {
    'use strict';

    const labels = {customer_ids: 'Customer', seller_ids: 'Seller'};
    const liveLabels = {...labels, group_by_seller: 'Group by seller'};
    const empty = () => ({customer_ids: [], seller_ids: []});

    function selections(value = {}) {
        if (!value || typeof value !== 'object' || Array.isArray(value)
            || Object.keys(value).some(key => !Object.hasOwn(labels, key))) {
            throw new Error('Invalid dashboard filter selections.');
        }
        const result = empty();
        for (const key of Object.keys(labels)) {
            const ids = value[key] ?? [];
            if (!Array.isArray(ids) || ids.length > 1000
                || ids.some(id => !Number.isSafeInteger(id) || id <= 0)) {
                throw new Error('Invalid dashboard filter selections.');
            }
            result[key] = [...new Set(ids)].sort((a, b) => a - b);
        }
        return result;
    }

    function configuration(value = {}) {
        if (!value || typeof value !== 'object' || Array.isArray(value)
            || Object.keys(value).some(key => !['scope', 'live_filters'].includes(key))) {
            throw new Error('Invalid dashboard filter configuration.');
        }
        const live = value.live_filters ?? [];
        if (!Array.isArray(live)
            || live.some(key => typeof key !== 'string' || !Object.hasOwn(liveLabels, key))) {
            throw new Error('Invalid dashboard live filters.');
        }
        return {scope: selections(value.scope), live_filters: Object.keys(liveLabels)
            .filter(key => live.includes(key))};
    }

    function liveSelections(value = {}) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
            throw new Error('Invalid live filter selections.');
        }
        const {group_by_seller: grouping = false, ...scope} = value;
        if (typeof grouping !== 'boolean') throw new Error('Group by seller must be a boolean.');
        return {...selections(scope), ...(grouping ? {group_by_seller: true} : {})};
    }

    function activeLiveKeys(value) {
        return Object.keys(liveLabels).filter(key => key === 'group_by_seller'
            ? value[key] === true : value[key].length > 0);
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {selections, liveSelections, configuration};
    }
    if (!global.document) return;

    function init() {
        const node = document.getElementById('dashboardFilterConfig');
        if (!node) return;
        const settings = JSON.parse(node.textContent);
        let config = configuration(settings.config);
        let live = liveSelections(settings.live);
        let widgets = settings.widgets;
        let readOnly = false;
        let selected = null;
        let generation = 0;
        let liveSignature = '';
        let liveKeys = [];
        let previewing = false;
        let panelWasOpen = false;
        let selectedSignature = '';
        let panelOpener = null;
        let storageError = '';
        const storageKey = 'sb.dashboard.filters.'
            + document.querySelector('.sb-dashboards').dataset.dashboardId;
        const explicitLive = Object.keys(liveLabels).some(key =>
            new URLSearchParams(global.location.search).has(key));
        const panel = document.getElementById('dashboardFiltersPanel');
        const openPanelButton = document.getElementById('openDashboardFilters');
        const widgetPanelButton = document.getElementById('openWidgetFilters');
        const dashboardTab = document.getElementById('dashboardFilterTab');
        const widgetTab = document.getElementById('widgetFilterTab');
        const viewerFilters = document.getElementById('dashboardLiveFilters');
        const status = document.getElementById('dashboardFilterStatus');
        const coverage = document.getElementById('dashboardFilterCoverage');
        const scopeHost = document.getElementById('dashboardScopeControls');
        const widgetHost = document.getElementById('widgetFilterControls');
        const widgetSettings = document.getElementById('widgetFilterSettings');
        const scopeControls = [];
        const liveControls = [];
        const widgetControls = [];

        function controls(host, filters, supported, onChange, options = {}) {
            host.replaceChildren();
            const result = Object.keys(labels).filter(key => supported.includes(key)).map(key => {
                const chosen = new Set(filters[key] || []);
                const details = document.createElement('details');
                details.className = 'db-filter-picker';
                const summary = document.createElement('summary');
                const panel = document.createElement('div');
                panel.className = 'db-filter-panel';
                const search = document.createElement('input');
                search.type = 'search';
                search.placeholder = 'Find ' + labels[key].toLowerCase();
                search.setAttribute('aria-label', search.placeholder);
                const actions = document.createElement('div');
                actions.className = 'db-filter-picker-actions';
                const clear = document.createElement('button');
                clear.type = 'button';
                clear.className = 'db-command is-quiet';
                clear.textContent = 'Clear selections';
                actions.append(clear);
                const list = document.createElement('div');
                list.className = 'db-filter-choice-list';
                list.setAttribute('role', 'group');
                list.setAttribute('aria-label', labels[key]);
                const rows = [...settings.choices[key]];
                for (const id of chosen) {
                    if (!rows.some(row => row.id === id)) {
                        rows.push({id, name: 'Unavailable ' + labels[key].toLowerCase() + ' #' + id});
                    }
                }
                const checkboxes = rows.map(row => {
                    const label = document.createElement('label');
                    const input = document.createElement('input');
                    input.type = 'checkbox';
                    input.value = row.id;
                    input.checked = chosen.has(row.id);
                    label.append(input, document.createTextNode(row.name));
                    list.append(label);
                    input.addEventListener('change', () => {
                        if (input.checked) chosen.add(row.id);
                        else chosen.delete(row.id);
                        changed();
                    });
                    return input;
                });
                function updateSummary() {
                    const names = rows.filter(row => chosen.has(row.id)).map(row => row.name);
                    summary.textContent = labels[key] + ': ' + (names.length === 1
                        ? names[0] : names.length ? names.length + ' selected'
                            : options.emptyLabel?.(key) || 'No additional restriction');
                    summary.title = names.join(', ') || summary.textContent;
                }
                function changed() {
                    updateSummary();
                    onChange(key, [...chosen].sort((a, b) => a - b));
                }
                clear.addEventListener('click', () => {
                    chosen.clear();
                    checkboxes.forEach(input => { input.checked = false; });
                    changed();
                });
                const noMatches = document.createElement('p');
                noMatches.className = 'db-muted';
                noMatches.textContent = rows.length ? 'No matching selections.' : 'No saved records.';
                noMatches.hidden = Boolean(rows.length);
                search.addEventListener('input', () => {
                    const query = search.value.trim().toLocaleLowerCase();
                    checkboxes.forEach((input, index) => {
                        input.parentElement.hidden = !rows[index].name.toLocaleLowerCase()
                            .includes(query);
                    });
                    noMatches.hidden = checkboxes.some(input => !input.parentElement.hidden);
                });
                panel.append(search, actions, list, noMatches);
                details.append(summary, panel);
                host.append(details);
                details.addEventListener('keydown', event => {
                    if (event.key !== 'Escape' || !details.open) return;
                    event.preventDefault();
                    event.stopPropagation();
                    details.open = false;
                    summary.focus();
                });
                updateSummary();
                return {
                    disable(disabled) {
                        checkboxes.forEach(input => { input.disabled = disabled; });
                        clear.disabled = disabled;
                    },
                };
            });
            if (options.live && supported.includes('group_by_seller')) {
                const label = document.createElement('label');
                label.className = 'db-live-group-filter';
                label.title = 'Group Details rows in supported widgets. Summary totals stay unchanged.';
                const input = document.createElement('input');
                input.type = 'checkbox';
                input.checked = filters.group_by_seller === true;
                input.dataset.liveGrouping = 'seller';
                input.addEventListener('change', () => onChange('group_by_seller', input.checked));
                label.append(input, document.createTextNode('Group by seller'));
                host.append(label);
                result.push({disable(disabled) { input.disabled = disabled; }});
            }
            return result;
        }

        function showTab(widget = false) {
            if (!dashboardTab || !settings.editing || previewing) return;
            const useWidget = widget && Boolean(selected);
            dashboardTab.setAttribute('aria-selected', String(!useWidget));
            dashboardTab.tabIndex = useWidget ? -1 : 0;
            widgetTab.setAttribute('aria-selected', String(useWidget));
            widgetTab.tabIndex = useWidget ? 0 : -1;
            document.getElementById('dashboardFilterSettings').hidden = useWidget;
            widgetSettings.hidden = !useWidget;
        }

        function setPanelOpen(open, opener = openPanelButton, widget = false) {
            const available = settings.editing && !previewing || liveKeys.length > 0;
            open = open && available;
            openPanelButton.hidden = open || !available;
            openPanelButton.setAttribute('aria-expanded', String(open));
            if (open) {
                panelOpener = opener;
                panel.hidden = false;
                showTab(widget);
                document.getElementById('dashboardFiltersTitle').focus({preventScroll: true});
            } else {
                const restoreFocus = panel.contains(document.activeElement);
                panel.hidden = true;
                panel.querySelectorAll('.db-filter-picker[open]').forEach(picker => {
                    picker.open = false;
                });
                if (restoreFocus && panelOpener && !panelOpener.disabled && !panelOpener.hidden) {
                    panelOpener.focus({preventScroll: true});
                }
            }
        }

        function updatePanel() {
            const viewing = !settings.editing || previewing;
            const count = viewing ? activeLiveKeys(live).length : 0;
            const countLabel = count ? ', ' + count + ' active' : '';
            const badge = document.getElementById('dashboardFilterCount');
            badge.textContent = String(count);
            badge.hidden = !count;
            openPanelButton.classList.toggle('is-active', count > 0);
            openPanelButton.setAttribute('aria-label', 'Filters' + countLabel);
            openPanelButton.title = 'Open filters' + countLabel;
            openPanelButton.hidden = !panel.hidden || viewing && !liveKeys.length;
            viewerFilters.hidden = !viewing;
            document.getElementById('dashboardFiltersSubtitle').textContent =
                viewing ? 'This view only' : 'Dashboard settings';
            document.getElementById('dashboardViewerActions').hidden = !viewing;
            if (settings.editing) {
                document.getElementById('dashboardFilterTabs').hidden = viewing;
                document.getElementById('dashboardFilterDraftHint').hidden = viewing;
                if (viewing) {
                    document.getElementById('dashboardFilterSettings').hidden = true;
                    widgetSettings.hidden = true;
                } else {
                    showTab(widgetTab.getAttribute('aria-selected') === 'true');
                }
            }
            if (viewing && !liveKeys.length && !panel.hidden) setPanelOpen(false);
        }

        function showStatus(message, error = false) {
            if (!message && storageError) {
                message = storageError;
                error = true;
            }
            status.textContent = message;
            status.hidden = !message;
            status.classList.toggle('is-error', error);
        }

        function saveLive() {
            if (settings.editing) return;
            try {
                global.localStorage.setItem(storageKey, JSON.stringify(live));
                storageError = '';
            } catch (error) {
                console.error('Dashboard filter storage:', error);
                storageError = 'Could not remember dashboard filters. '
                    + 'They apply on this page only. ' + error.message;
                showStatus(storageError, true);
            }
        }

        function restoreLive() {
            if (settings.editing || explicitLive) return;
            try {
                const stored = global.localStorage.getItem(storageKey);
                const restored = stored === null ? liveSelections(settings.live)
                    : liveSelections(JSON.parse(stored));
                const before = JSON.stringify(live);
                live = restored;
                renderLive();
                if (before !== JSON.stringify(live)) refresh();
            } catch (error) {
                console.error('Dashboard filter storage:', error);
                storageError = 'Could not restore dashboard filters. '
                    + 'Current selections are unchanged. ' + error.message;
                showStatus(storageError, true);
            }
        }

        function context(filters) {
            return {dashboard: config, widget: selections(filters), live};
        }

        async function refresh() {
            const request = ++generation;
            document.getElementById('dashboardReading').setAttribute('aria-busy', 'true');
            showStatus('Updating filtered widgets...');
            try {
                if (settings.editing) {
                    await global.SBDashboardEditor.refreshFilters();
                } else {
                    const tiles = [...document.querySelectorAll('#dashboardCanvas [data-widget-id]')];
                    const responses = await Promise.all(tiles.map(async tile => {
                        const query = new URLSearchParams({
                            key: tile.dataset.widgetKey, variant: tile.dataset.variant,
                            width: tile.dataset.columnSpan, instance_id: tile.dataset.widgetId,
                            filter_context: JSON.stringify(context(
                                JSON.parse(tile.dataset.widgetFilters || '{}'))),
                        });
                        const response = await global.fetch(
                            '/api/reports/dashboards/widget-preview?' + query);
                        const payload = await response.json();
                        if (!response.ok || !payload.success || typeof payload.html !== 'string') {
                            throw new Error(payload.error || 'Widget content could not be loaded.');
                        }
                        return {tile, html: payload.html};
                    }));
                    if (request !== generation) return;
                    for (const {tile, html} of responses) {
                        global.SBConnectGoals.destroy(tile);
                        tile.querySelector('.db-widget-body').remove();
                        tile.insertAdjacentHTML('afterbegin', html);
                        global.SBConnectGoals.init(tile);
                        global.SBPToF?.mountStatuses(tile);
                        global.SBTableSort?.mount(tile);
                    }
                }
                if (request !== generation) return;
                showStatus('');
            } catch (error) {
                if (request !== generation) return;
                console.error('Dashboard filters:', error);
                showStatus('Could not refresh filters. Displayed values may reflect previous '
                    + 'selections. Change or reset a filter to retry. ' + error.message, true);
            } finally {
                if (request === generation) {
                    document.getElementById('dashboardReading').setAttribute('aria-busy', 'false');
                }
            }
        }

        function renderLive() {
            updateScopeLabel();
            const supported = Object.keys(liveLabels).filter(key =>
                widgets.some(widget => widget.supported.includes(key)));
            const keys = config.live_filters.filter(key => supported.includes(key));
            liveKeys = keys;
            for (const key of Object.keys(labels)) {
                if (!keys.includes(key)) live[key] = [];
            }
            if (!keys.includes('group_by_seller')) delete live.group_by_seller;
            const signature = JSON.stringify([keys, live, config.scope]);
            if (signature !== liveSignature) {
                liveSignature = signature;
                liveControls.splice(0, liveControls.length, ...controls(
                    document.getElementById('dashboardLiveControls'), live, keys, (key, ids) => {
                        live = liveSelections({...live, [key]: ids});
                        liveSignature = JSON.stringify([keys, live, config.scope]);
                        updateCoverage();
                        refresh();
                        saveLive();
                    }, {live: true, emptyLabel: key => Object.values(config.scope).some(ids => ids.length)
                        ? 'All allowed ' + (key === 'customer_ids' ? 'customers' : 'sellers')
                        : 'All ' + (key === 'customer_ids' ? 'customers' : 'sellers')}));
            }
            updateCoverage();
            document.querySelectorAll('[data-live-filter]').forEach(input => {
                input.checked = config.live_filters.includes(input.dataset.liveFilter);
                input.disabled = readOnly || !supported.includes(input.dataset.liveFilter);
            });
            if (settings.editing) {
                const enabled = config.live_filters.filter(key => supported.includes(key));
                document.getElementById('dashboardViewerFilterHint').textContent = enabled.length
                    ? enabled.map(key => liveLabels[key]).join(' and ')
                        + ' filters will be available to viewers. Their choices are temporary.'
                    : 'No viewer controls enabled. Viewers will see the saved dashboard as-is.';
                document.getElementById('tryDashboardFilters').hidden = !enabled.length;
                document.getElementById('tryDashboardFilters').disabled = readOnly;
            }
        }

        function updateCoverage() {
            const active = Object.keys(labels).filter(key => config.scope[key].length || live[key].length);
            if (live.group_by_seller) active.push('group_by_seller');
            const matched = widgets.filter(widget => active.every(key => widget.supported.includes(key)));
            coverage.textContent = active.length && matched.length !== widgets.length
                ? 'Applies to ' + matched.length + ' of ' + widgets.length + ' widgets' : '';
            coverage.hidden = !coverage.textContent;
            document.getElementById('resetDashboardFilters').disabled =
                activeLiveKeys(live).length === 0;
            updatePanel();
        }

        function updateScopeLabel() {
            const savedScope = document.getElementById('dashboardSavedScope');
            const parts = Object.keys(labels).filter(key => config.scope[key].length).map(key => {
                const names = config.scope[key].map(id => settings.choices[key]
                    .find(choice => choice.id === id)?.name || 'Unavailable #' + id);
                return labels[key] + ': ' + names.join(', ');
            });
            savedScope.hidden = !parts.length;
            savedScope.textContent = 'Always includes ' + parts.join(' · ');
            savedScope.title = savedScope.textContent;
            document.getElementById('dashboardViewerScope').hidden = !parts.length;
            document.getElementById('dashboardViewerScopeText').textContent = parts.join(' · ');
            const hint = document.getElementById('dashboardScopeHint');
            if (hint) hint.textContent = parts.length ? parts.join(' · ')
                : 'No restrictions. This dashboard includes all customers.';
        }

        function restore(value) {
            const restored = configuration(value);
            const changed = JSON.stringify(restored) !== JSON.stringify(config);
            config = restored;
            if (scopeHost && (changed || !scopeHost.childElementCount)) {
                scopeControls.splice(0, scopeControls.length, ...controls(scopeHost,
                    config.scope, Object.keys(labels), (key, ids) => {
                        config = {...config, scope: {...config.scope, [key]: ids}};
                        global.SBDashboardEditor.setFilterConfig(config);
                        renderLive();
                        refresh();
                    }, {emptyLabel: key => 'All ' + (key === 'customer_ids' ? 'customers' : 'sellers')}));
            }
            renderLive();
        }

        function select(item, supported = [], disabled = false) {
            selected = item;
            if (!widgetSettings) return;
            widgetTab.disabled = !item;
            widgetPanelButton.disabled = !item || disabled;
            if (!item) {
                showTab(false);
                selectedSignature = '';
                return;
            }
            const title = settings.catalog.find(definition => definition.key === item.key)?.title
                || 'Unavailable widget';
            document.getElementById('widgetFilterTitle').textContent = title;
            const filters = selections(item.filters);
            const restricted = Object.keys(labels).some(key => filters[key].length);
            document.getElementById('widgetFilterInheritance').textContent = restricted
                ? 'Dashboard filters + widget restrictions' : 'Inherits dashboard filters';
            document.getElementById('widgetFilterSupport').textContent = supported.length
                ? restricted ? 'These choices intersect with dashboard rules and viewer selections.'
                    : 'No additional restrictions. This widget follows the dashboard.'
                : 'This widget does not support Customer or Seller filters.';
            const signature = JSON.stringify([item.id, filters, supported]);
            if (signature === selectedSignature) {
                widgetControls.forEach(control => control.disable(disabled));
                return;
            }
            selectedSignature = signature;
            widgetControls.splice(0);
            widgetControls.push(...controls(widgetHost, selections(item.filters), supported,
                (key, ids) => {
                    if (!selected) return;
                    const filters = {...selections(selected.filters), [key]: ids};
                    selectedSignature = JSON.stringify([selected.id, filters, supported]);
                    global.SBDashboardEditor.setWidgetFilters(selected.id, filters);
                    refresh();
                }));
            if (!supported.length) widgetHost.textContent =
                'This widget uses portfolio-wide metrics and does not follow Customer or Seller filters.';
            widgetControls.forEach(control => control.disable(disabled));
        }

        global.SBDashboardFilters = Object.freeze({
            context, restore, select, refresh,
            configuration, selections, liveSelections,
            setPreview(value) {
                if (value) panelWasOpen = !panel.hidden;
                previewing = value;
                renderLive();
                setPanelOpen(panelWasOpen);
            },
            configure(items, disabled) {
                widgets = items;
                readOnly = disabled;
                renderLive();
                scopeControls.forEach(control => control.disable(disabled));
                widgetControls.forEach(control => control.disable(disabled));
            },
        });
        if (panel) {
            openPanelButton.addEventListener('click', () => {
                setPanelOpen(panel.hidden);
            });
            widgetPanelButton?.addEventListener('click', () => {
                setPanelOpen(true, widgetPanelButton, true);
            });
            document.getElementById('closeDashboardFilters').addEventListener('click', () => {
                setPanelOpen(false);
            });
            document.getElementById('tryDashboardFilters')?.addEventListener('click', () => {
                document.getElementById('previewDashboardDraft').click();
            });
            dashboardTab?.addEventListener('click', () => showTab(false));
            widgetTab?.addEventListener('click', () => showTab(true));
            panel.addEventListener('keydown', event => {
                if (event.key === 'Escape' && !event.defaultPrevented) {
                    event.preventDefault();
                    event.stopPropagation();
                    setPanelOpen(false);
                } else if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)
                    && event.target.getAttribute('role') === 'tab') {
                    event.preventDefault();
                    const useWidget = !widgetTab.disabled && (event.key === 'End'
                        || (event.key !== 'Home' && event.target === dashboardTab));
                    showTab(useWidget);
                    (useWidget ? widgetTab : dashboardTab).focus();
                }
            });
        }
        document.querySelectorAll('[data-live-filter]').forEach(input => {
            input.addEventListener('change', () => {
                config = {...config, live_filters: Object.keys(liveLabels).filter(key =>
                    document.querySelector('[data-live-filter="' + key + '"]').checked)};
                global.SBDashboardEditor.setFilterConfig(config);
                renderLive();
                refresh();
            });
        });
        document.getElementById('resetDashboardFilters').addEventListener('click', () => {
            live = empty();
            renderLive();
            refresh();
            saveLive();
        });
        restore(config);
        restoreLive();
        global.addEventListener('pageshow', restoreLive);
        global.addEventListener('storage', event => {
            if (event.key === storageKey || event.key === null) restoreLive();
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})(typeof window === 'undefined' ? globalThis : window);
