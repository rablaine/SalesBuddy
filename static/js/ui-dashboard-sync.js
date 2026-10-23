(function (global) {
    'use strict';

    function createCoordinator(fetcher, jobs) {
        const running = new Map();
        return {
            follow(state, key, retry, listener) {
                const operation = state.operation;
                let entry = running.get(operation);
                if (!entry) {
                    entry = {listeners: new Set(), controller: new AbortController()};
                    running.set(operation, entry);
                    entry.promise = (async () => {
                        const response = await fetcher('/api/reports/dashboards/widgets/'
                            + encodeURIComponent(key) + '/sync', {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({retry}), signal: entry.controller.signal,
                        });
                        const data = await response.json();
                        if (!response.ok || data.success !== true
                            || typeof data.ready !== 'boolean') {
                            throw new Error(data.error || 'Could not prepare widget data.');
                        }
                        if (!data.sync) return;
                        if (data.sync.error) throw new Error(data.sync.error);
                        const job = data.sync.job;
                        if (!Number.isInteger(job?.id) || job.id <= 0) {
                            throw new Error('The sync server did not return a job identity.');
                        }
                        const result = await jobs.observe(job.id, {
                            signal: entry.controller.signal,
                            onProgress(progress) {
                                entry.latest = progress;
                                entry.source = data.sync;
                                entry.listeners.forEach(callback => callback(progress, data.sync));
                            },
                        });
                        if (result.status !== 'done') {
                            throw new Error(result.error || 'The sync failed. Please retry.');
                        }
                    })().finally(() => {
                        if (running.get(operation) === entry) running.delete(operation);
                    });
                }
                entry.listeners.add(listener);
                if (entry.latest) listener(entry.latest, entry.source);
                return entry.promise.finally(() => entry.listeners.delete(listener));
            },
            stop() {
                running.forEach(entry => entry.controller.abort());
                running.clear();
            },
        };
    }

    if (typeof module !== 'undefined' && module.exports) module.exports = {createCoordinator};
    if (!global.document) return;

    function init() {
        const root = document.querySelector('.db-dashboard-workspace');
        if (!root) return;
        const coordinator = createCoordinator(global.fetch.bind(global), global.SBJobs);
        const mounted = new WeakSet();
        const cleanups = new WeakMap();

        async function refresh(body) {
            if (!body.isConnected) return;
            if (body.closest('.db-preview-widget')) {
                global.dispatchEvent(new CustomEvent('sb:widget-sync-ready', {detail: {body}}));
                return;
            }
            const tile = body.closest('[data-widget-id]');
            if (tile && global.SBDashboardEditor) {
                await global.SBDashboardEditor.refreshWidget(Number(tile.dataset.widgetId));
                return;
            }
            const parameters = JSON.parse(body.dataset.syncPreview);
            const context = () => global.SBDashboardFilters?.context(
                JSON.parse(tile?.dataset.widgetFilters || '{}'));
            const scope = JSON.stringify(context());
            const query = new URLSearchParams(parameters);
            if (scope) query.set('filter_context', scope);
            const response = await global.fetch('/api/reports/dashboards/widget-preview?' + query);
            const data = await response.json();
            if (!response.ok || data.success !== true || typeof data.html !== 'string') {
                throw new Error(data.error || 'Could not refresh widget data.');
            }
            if (!body.isConnected || scope !== JSON.stringify(context())) return;
            const parent = body.parentElement;
            global.SBConnectGoals?.destroy(parent);
            body.insertAdjacentHTML('beforebegin', data.html);
            body.remove();
            global.SBConnectGoals?.init(parent);
            global.SBPToF?.mountStatuses(parent);
            global.SBTableSort?.mount(parent);
        }

        function mount(body) {
            if (mounted.has(body)) return;
            mounted.add(body);
            const state = JSON.parse(body.dataset.widgetSync);
            const parameters = JSON.parse(body.dataset.syncPreview);
            const message = body.querySelector('[data-sync-message]');
            const progress = body.querySelector('progress');
            const button = body.querySelector('[data-widget-sync-retry]');
            const actions = body.querySelector('[data-widget-sync-actions]');
            const label = body.querySelector('[data-sync-label]');
            let busy = false;
            let refreshOnly = false;
            function failure(error) {
                console.error('Dashboard widget sync:', error);
                message.textContent = error.message;
                message.classList.add('db-error');
                progress.hidden = true;
                button.textContent = refreshOnly ? 'Retry loading widget' : 'Retry sync';
                button.hidden = false;
                actions.hidden = false;
            }
            async function run(retry = false) {
                if (busy || !body.isConnected) return;
                busy = true;
                button.hidden = true;
                actions.hidden = true;
                progress.hidden = false;
                message.classList.remove('db-error');
                message.textContent = refreshOnly ? 'Loading synced data...' : 'Preparing widget data...';
                try {
                    if (!refreshOnly) {
                        await coordinator.follow(state, parameters.key, retry, (job, source) => {
                            if (!body.isConnected) return;
                            if (label.textContent !== source.name) label.textContent = source.name;
                            const text = global.SBJobs.message(job);
                            if (message.textContent !== text) message.textContent = text;
                            const percent = job.progress?.percent;
                            if (typeof percent === 'number' && Number.isFinite(percent)) {
                                progress.value = Math.max(0, Math.min(100, percent));
                            } else progress.removeAttribute('value');
                        });
                    }
                    refreshOnly = true;
                    await refresh(body);
                } catch (error) {
                    if (error.name !== 'AbortError' && body.isConnected) failure(error);
                } finally {
                    busy = false;
                }
            }
            const retry = () => run(true);
            button.addEventListener('click', retry);
            cleanups.set(body, () => button.removeEventListener('click', retry));
            label.textContent = state.name;
            if (state.error) failure(new Error(state.error));
            else run();
        }

        function scan() {
            document.querySelectorAll('[data-widget-sync]').forEach(mount);
        }
        const observer = new MutationObserver(scan);
        observer.observe(document.querySelector('.sb-dashboards') || root, {
            childList: true, subtree: true,
        });
        // The library lives outside the canvas and replaces its preview independently.
        const library = document.getElementById('widgetLibrary');
        if (library) observer.observe(library, {childList: true, subtree: true});
        global.addEventListener('pagehide', () => {
            observer.disconnect();
            coordinator.stop();
            document.querySelectorAll('[data-widget-sync]').forEach(body => cleanups.get(body)?.());
        }, {once: true});
        scan();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
    global.addEventListener('pageshow', event => {
        if (event.persisted) init();
    });
})(typeof window === 'undefined' ? globalThis : window);
