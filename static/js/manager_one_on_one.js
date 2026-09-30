/* Persistent initiative tracking, work detail modals, and dated discussion points. */
(() => {
    'use strict';
    const root = document.getElementById('managerReport');
    const api = '/api/reports/manager-one-on-one';
    const errorBox = document.getElementById('reportError');
    const errorMessage = document.getElementById('reportErrorMessage');
    const retryPoints = document.getElementById('retryTalkingPoints');
    const pointsState = document.getElementById('talkingPointsState');
    const searches = new WeakMap();
    const pointsTimers = new WeakMap();
    const pointsRequests = new WeakMap();
    const AUTOSAVE_DELAY = 700;
    let saving = false;
    let pointsBusy = false;
    let detailRequest = null;
    let contextBeforeWork;
    let workContext;

    async function request(url, method = 'GET', data, signal) {
        const response = await fetch(url, {
            method,
            headers: data === undefined ? {} : {'Content-Type': 'application/json'},
            body: data === undefined ? undefined : JSON.stringify(data),
            signal,
        });
        let payload;
        try {
            payload = await response.json();
        } catch {
            throw new Error('The report could not reach the server. Please try again.');
        }
        if (!response.ok || !payload.success) {
            throw new Error(payload.error || 'Your changes could not be saved. Please try again.');
        }
        return payload;
    }

    function showError(error, pointsFailure = false, focus = true) {
        errorMessage.textContent = error.message;
        errorBox.dataset.pointsFailure = String(pointsFailure);
        retryPoints.classList.toggle('d-none', !pointsFailure);
        errorBox.classList.remove('d-none');
        const modalError = document.getElementById('pointsModalError');
        modalError.querySelector('span').textContent = error.message;
        modalError.classList.remove('d-none');
        modalError.querySelector('button').classList.toggle('d-none', !pointsFailure);
        if (focus) errorBox.focus();
    }

    function updatePointsState() {
        const failed = root.querySelector('.talking-points.is-invalid');
        const pending = root.querySelector('.talking-points-field[data-dirty="true"]');
        pointsState.textContent = failed ? 'Points not saved' : pending ? 'Saving...' : '';
        pointsState.classList.toggle('text-danger', Boolean(failed));
        pointsState.classList.toggle('text-muted', !failed);
        const modalState = document.getElementById('pointsModalState');
        modalState.textContent = pointsState.textContent;
        modalState.classList.toggle('text-danger', Boolean(failed));
        document.getElementById('discussPoints').disabled = pointsBusy
            || !document.getElementById('managerPointsText').value.trim();
        if (!failed && !pending && errorBox.dataset.pointsFailure === 'true') {
            errorBox.classList.add('d-none');
            document.getElementById('pointsModalError').classList.add('d-none');
        }
    }

    async function savePoints(field) {
        clearTimeout(pointsTimers.get(field));
        if (pointsRequests.has(field)) return pointsRequests.get(field);
        const input = field.querySelector('textarea');
        const pending = (async () => {
            input.setAttribute('aria-busy', 'true');
            try {
                // Serialize each field so an earlier request cannot overwrite a newer edit.
                while (field.dataset.dirty === 'true') {
                    const value = input.value;
                    const payload = await request(`${api}/items/${field.dataset.itemId}`, 'PATCH', {
                        talking_points: value,
                    });
                    if (payload.item) {
                        updatePointsButton(payload.item);
                        showPointsCreated(payload.item);
                    }
                    field.dataset.dirty = String(input.value !== value);
                }
                input.classList.remove('is-invalid');
                input.removeAttribute('aria-invalid');
            } catch (error) {
                input.classList.add('is-invalid');
                input.setAttribute('aria-invalid', 'true');
                throw new Error(`Points were not saved. ${error.message}`);
            } finally {
                input.removeAttribute('aria-busy');
            }
        })();
        pointsRequests.set(field, pending);
        try {
            await pending;
        } finally {
            pointsRequests.delete(field);
            updatePointsState();
        }
    }

    async function autosavePoints(field) {
        try {
            await savePoints(field);
        } catch (error) {
            showError(error, true, false);
        }
    }

    async function flushTalkingPoints() {
        for (const field of root.querySelectorAll('.talking-points-field[data-dirty="true"]')) {
            await savePoints(field);
        }
    }

    function localTime(value) {
        return value ? new Date(value).toLocaleString(undefined, {
            year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
        }) : 'Before date tracking';
    }

    function showPointsCreated(item) {
        document.getElementById('pointsCreatedAt').textContent = item.talking_points.trim()
            ? `Created: ${localTime(item.points_created_at)}` : '';
    }

    function updatePointsButton(item) {
        const button = root.querySelector(`.points-button[data-item-id="${item.id}"]`);
        const active = Boolean(item.talking_points.trim());
        button.classList.toggle('text-primary', active);
        button.classList.toggle('text-secondary', !active);
        button.querySelector('i').className = active
            ? 'bi bi-chat-left-text-fill' : 'bi bi-chat-left-text';
        button.querySelector('.points-count')?.remove();
        const count = item.discussed_points.length;
        button.title = `${active ? 'Current points' : 'Add points'}${count ? `; ${count} discussed` : ''}`;
        if (count) {
            const badge = document.createElement('span');
            badge.className = 'small points-count ms-1';
            badge.textContent = count;
            button.append(badge);
        }
    }

    function renderPoints(item) {
        const field = document.getElementById('managerPointsField');
        const input = document.getElementById('managerPointsText');
        field.dataset.itemId = String(item.id);
        field.dataset.dirty = 'false';
        input.value = item.talking_points;
        input.classList.remove('is-invalid');
        input.removeAttribute('aria-invalid');
        document.getElementById('managerPointsTitle').textContent = `Points: ${item.title}`;
        document.getElementById('managerPointsContext').textContent = item.customer_name;
        showPointsCreated(item);
        const history = document.getElementById('pointsHistory');
        history.replaceChildren();
        document.getElementById('pointsHistoryCount').textContent = item.discussed_points.length || '';
        for (const point of item.discussed_points) {
            const entry = document.createElement('article');
            entry.className = 'border-top pt-3 mb-3';
            const dates = document.createElement('div');
            dates.className = 'small text-muted mb-2';
            for (const [label, value] of [
                ['Created', point.created_at], ['Discussed', point.discussed_at],
            ]) {
                const part = document.createElement('span');
                part.className = 'me-3';
                part.append(`${label}: `);
                const time = document.createElement('time');
                if (value) time.dateTime = value;
                time.textContent = localTime(value);
                part.append(time);
                dates.append(part);
            }
            const text = document.createElement('p');
            text.className = 'mb-0 text-break';
            text.style.whiteSpace = 'pre-wrap';
            text.textContent = point.text;
            entry.append(dates, text);
            history.append(entry);
        }
        if (!item.discussed_points.length) {
            history.textContent = 'Nothing discussed yet. Mark your current points discussed to keep a dated record here.';
            history.classList.add('text-muted', 'small');
        } else {
            history.classList.remove('text-muted', 'small');
        }
        updatePointsButton(item);
        updatePointsState();
    }

    async function openPoints(button) {
        if (pointsBusy) return;
        pointsBusy = true;
        button.disabled = true;
        try {
            await flushTalkingPoints();
            const payload = await request(`${api}/items/${button.dataset.itemId}`);
            errorBox.classList.add('d-none');
            document.getElementById('pointsModalError').classList.add('d-none');
            renderPoints(payload.item);
            bootstrap.Modal.getOrCreateInstance(document.getElementById('managerPointsModal')).show();
        } catch (error) {
            showError(error, Boolean(root.querySelector('.talking-points.is-invalid')));
        } finally {
            pointsBusy = false;
            button.disabled = false;
            updatePointsState();
        }
    }

    async function discussPoints(button) {
        if (pointsBusy) return;
        const field = document.getElementById('managerPointsField');
        const input = field.querySelector('textarea');
        pointsBusy = true;
        input.disabled = true;
        button.disabled = true;
        document.getElementById('pointsModalError').classList.add('d-none');
        try {
            await flushTalkingPoints();
            const payload = await request(`${api}/items/${field.dataset.itemId}/discuss`, 'POST', {
                talking_points: input.value,
            });
            errorBox.classList.add('d-none');
            renderPoints(payload.item);
        } catch (error) {
            showError(error, Boolean(root.querySelector('.talking-points.is-invalid')), false);
        } finally {
            pointsBusy = false;
            input.disabled = false;
            updatePointsState();
            input.focus();
        }
    }

    async function activateDetailScripts(body, signal) {
        // Match the home detail modal's scoped script activation, retaining inline handlers.
        const handlers = [
            'toggleEngagementFavorite', 'recommendPartners', 'generateStory', 'applySelectedFields',
            'activateGhostRow', 'deactivateGhostRow', 'openNewTaskModal', 'submitGhostTask',
            'toggleTask', 'viewTask', 'editTask', 'saveEditedTask', 'deleteTask',
            'toggleMilestoneEdit', 'saveMilestoneLinks', 'joinMilestoneTeam', 'leaveMilestoneTeam',
            'viewActionItem', 'switchToEditMode', 'switchToViewMode', 'saveActionItemEdits',
            'saveCopilotTask', 'dismissCopilotTask', 'notUsefulCopilotTask',
        ];
        const exports = handlers.map(name =>
            `if (typeof ${name} === 'function') window.${name} = ${name};`,
        ).join('\n');
        for (const old of body.querySelectorAll('script')) {
            if (signal.aborted) return;
            const script = document.createElement('script');
            if (old.src) {
                script.src = old.src;
                await new Promise((resolve, reject) => {
                    script.onload = resolve;
                    script.onerror = () => reject(new Error('A work detail component could not load. Reopen the row to retry.'));
                    old.replaceWith(script);
                });
            } else {
                script.textContent = `(function(){${old.textContent}\n${exports}\n})();`;
                old.replaceWith(script);
            }
        }
    }

    async function openWork(row) {
        await openDetail(
            row.dataset.title,
            row.querySelector('.work-detail-link').href,
            `/api/${row.dataset.itemType}/${row.dataset.entityId}/detail`,
        );
    }

    async function openSellerNotes(link) {
        await openDetail(
            `1:1 notes: ${link.dataset.sellerName}`,
            link.href,
            `/api/seller/${link.dataset.sellerId}/one-on-one/detail`,
        );
    }

    async function openDetail(title, fullUrl, fragmentUrl) {
        detailRequest?.abort();
        for (const modal of document.querySelectorAll('[data-workspace-parent-modal]')) {
            bootstrap.Modal.getInstance(modal)?.dispose();
            modal.remove();
        }
        const controller = new AbortController();
        detailRequest = controller;
        contextBeforeWork = window.copilotContext;
        workContext = undefined;
        const body = document.getElementById('managerWorkBody');
        document.getElementById('managerWorkTitle').textContent = title;
        document.getElementById('managerWorkFullLink').href = fullUrl;
        body.innerHTML = '<div class="text-center py-5"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div></div>';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('managerWorkModal')).show();
        try {
            const response = await fetch(fragmentUrl, {
                signal: controller.signal,
            });
            if (!response.ok) throw new Error('Work details could not be loaded. Reopen the row to retry.');
            const html = await response.text();
            if (controller.signal.aborted) return;
            body.innerHTML = html;
            await activateDetailScripts(body, controller.signal);
            if (!controller.signal.aborted) workContext = window.copilotContext;
        } catch (error) {
            if (error.name === 'AbortError' || controller.signal.aborted) return;
            body.replaceChildren();
            const message = document.createElement('div');
            message.className = 'alert alert-danger';
            message.setAttribute('role', 'alert');
            message.textContent = error.message;
            body.append(message);
        }
    }

    async function mutate(button, action) {
        if (saving) return;
        saving = true;
        const controls = Array.from(root.querySelectorAll(
            'input, textarea, select, button',
        ), control => ({control, disabled: control.disabled}));
        for (const {control} of controls) control.disabled = true;
        button.disabled = true;
        errorBox.classList.add('d-none');
        try {
            await flushTalkingPoints();
            await action();
            const section = button.closest('section');
            if (section) history.replaceState(null, '', `#${section.id}`);
            location.reload();
        } catch (error) {
            showError(error, Boolean(root.querySelector('.talking-points.is-invalid')));
        } finally {
            saving = false;
            for (const {control, disabled} of controls) control.disabled = disabled;
            button.disabled = false;
        }
    }

    function cell(row, text, className = '') {
        const td = row.insertCell();
        td.textContent = text;
        td.className = className;
        return td;
    }

    function updateSelection(picker) {
        const count = picker.querySelectorAll('input[type="checkbox"]:checked:not(:disabled)').length;
        picker.querySelector('.selected-count').textContent = count > 75
            ? `${count} selected. Add up to 75 at a time.` : `${count} selected`;
        picker.querySelector('[data-action="add-items"]').disabled = count === 0 || count > 75;
    }

    function updatePickerSource(picker, type, source) {
        picker.querySelector('.milestone-source').classList.toggle('d-none', type !== 'milestone');
        for (const button of picker.querySelectorAll('[data-action="picker-source"]')) {
            const active = button.dataset.source === source;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', String(active));
        }
    }

    function candidateStatus(payload, source) {
        if (source !== 'u2c') {
            return payload.results.length
                ? `${payload.results.length} available. Search to narrow the list (up to 75 shown).`
                : 'No matching work. Try another search. Work already in this initiative is excluded.';
        }
        if (!payload.snapshot) return payload.message;
        const snapshot = payload.snapshot;
        const date = snapshot.version_date
            ? `Snapshot ${snapshot.version_date}` : `Imported ${localTime(snapshot.snapshot_date)}`;
        const available = payload.results.filter(item => item.selectable).length;
        const added = payload.results.filter(item => item.already_added).length;
        const missing = payload.results.filter(item => !item.available).length;
        return [
            snapshot.fiscal_quarter, date,
            payload.workload_prefix ? `${payload.workload_prefix} workloads (U2C filter)` : 'All workloads',
            'Live details; quarter based on snapshot dates',
            payload.results.length ? `${available} available` : 'No matching uncommitted milestones',
            added ? `${added} already added` : '',
            missing ? `${missing} not synced locally. Sync these milestones before adding them.` : '',
        ].filter(Boolean).join(' · ');
    }

    async function search(picker) {
        searches.get(picker)?.abort();
        const controller = new AbortController();
        searches.set(picker, controller);
        const form = picker.querySelector('.candidate-search');
        const status = picker.querySelector('.picker-status');
        const results = picker.querySelector('.candidate-results');
        const type = form.elements.type.value;
        const source = type === 'milestone' && picker.dataset.source === 'u2c' ? 'u2c' : 'search';
        updatePickerSource(picker, type, source);
        results.replaceChildren();
        updateSelection(picker);
        status.textContent = 'Loading work...';
        status.classList.remove('text-danger');
        try {
            const params = new URLSearchParams({type, source, q: form.elements.q.value.trim()});
            if (source === 'u2c') {
                const workload = localStorage.getItem('u2c_workload_filter');
                if (workload) params.set('workload_prefix', workload);
            }
            const payload = await request(
                `${api}/sections/${picker.dataset.sectionId}/candidates?${params}`,
                'GET', undefined, controller.signal,
            );
            if (controller.signal.aborted) return;
            status.textContent = candidateStatus(payload, source);
            if (!payload.results.length) return;
            const table = document.createElement('table');
            table.className = 'table table-sm align-middle mb-0';
            const caption = table.createCaption();
            caption.className = 'visually-hidden';
            caption.textContent = `Available ${type}s`;
            const head = table.createTHead().insertRow();
            for (const name of ['Select', 'Work', 'Customer', 'Status', 'Due', 'ACR']) {
                const th = document.createElement('th');
                th.scope = 'col';
                th.className = 'text-nowrap';
                th.textContent = name;
                head.append(th);
            }
            const body = table.createTBody();
            for (const item of payload.results) {
                const row = body.insertRow();
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.className = 'form-check-input';
                checkbox.value = item.id;
                checkbox.disabled = item.selectable === false;
                checkbox.setAttribute('aria-label', `Select ${item.title} for ${item.customer_name}`);
                const selection = cell(row, '');
                selection.append(checkbox);
                if (item.reason) {
                    checkbox.setAttribute('title', item.reason);
                    const reason = document.createElement('span');
                    reason.className = 'small text-muted d-block text-nowrap mt-1';
                    reason.textContent = item.reason;
                    selection.append(reason);
                }
                const work = cell(row, '');
                const title = document.createElement('div');
                title.className = 'fw-semibold text-break';
                title.textContent = item.title;
                const context = document.createElement('div');
                context.className = 'small text-muted';
                context.textContent = [item.detail, item.on_my_team ? 'On my team' : '']
                    .filter(Boolean).join(' · ');
                work.append(title, context);
                cell(row, item.customer_name, 'small');
                cell(row, [item.status, item.commitment].filter(Boolean).join(' / '), 'small');
                cell(row, item.due_date || 'Not set', 'small text-nowrap');
                cell(row, item.acr === null ? 'Not set' : new Intl.NumberFormat(
                    'en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0},
                ).format(item.acr), 'text-end text-nowrap');
            }
            results.append(table);
        } catch (error) {
            if (error.name === 'AbortError') return;
            status.textContent = error.message;
            status.classList.add('text-danger');
        }
    }

    root.addEventListener('input', event => {
        const field = event.target.closest('.talking-points-field');
        if (!field) return;
        field.dataset.dirty = 'true';
        updatePointsState();
        clearTimeout(pointsTimers.get(field));
        pointsTimers.set(field, setTimeout(() => autosavePoints(field), AUTOSAVE_DELAY));
    });

    document.getElementById('managerWorkModal').addEventListener('hidden.bs.modal', event => {
        if (event.target.id !== 'managerWorkModal') return;
        detailRequest?.abort();
        window.copilotContext = contextBeforeWork;
    });
    document.getElementById('managerWorkModal').addEventListener('shown.bs.modal', event => {
        if (event.target.id !== 'managerWorkModal') return;
        if (workContext) window.copilotContext = workContext;
    });

    root.addEventListener('focusout', event => {
        const field = event.target.closest('.talking-points-field');
        if (field && field.dataset.dirty === 'true') autosavePoints(field);
    });

    root.addEventListener('change', event => {
        const picker = event.target.closest('.work-picker');
        if (!picker) return;
        if (event.target.name === 'type') {
            picker.dataset.source = 'search';
            search(picker);
        }
        else updateSelection(picker);
    });

    root.addEventListener('submit', event => {
        const form = event.target;
        if (!form.matches('.candidate-search, .section-form')) return;
        event.preventDefault();
        if (form.matches('.candidate-search')) {
            search(form.closest('.work-picker'));
        } else if (form.matches('.section-form')) {
            const id = form.dataset.sectionId;
            mutate(form.querySelector('[type="submit"]'), () => request(
                id ? `${api}/sections/${id}` : `${api}/sections`,
                id ? 'PATCH' : 'POST',
                {name: form.elements.name.value, description: form.elements.description.value},
            ));
        }
    });

    root.addEventListener('click', event => {
        const button = event.target.closest('[data-action]');
        if (button?.dataset.action === 'open-seller-notes'
            && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
        if (!button) {
            const candidate = event.target.closest('.candidate-results tbody tr');
            if (candidate && !event.target.closest('input, a, button')) {
                const checkbox = candidate.querySelector('input[type="checkbox"]');
                if (!checkbox.disabled) {
                    checkbox.checked = !checkbox.checked;
                    updateSelection(candidate.closest('.work-picker'));
                }
                return;
            }
            const row = event.target.closest('.manager-work-row[data-entity-id]');
            if (!row || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            const interactive = event.target.closest('a, button, input, textarea, select');
            if (interactive && !interactive.matches('.work-detail-link')) return;
            if (window.getSelection()?.toString()) return;
            event.preventDefault();
            openWork(row);
            return;
        }
        const picker = button.closest('.work-picker');
        const sectionId = button.dataset.sectionId;
        switch (button.dataset.action) {
        case 'picker-source':
            picker.dataset.source = button.dataset.source;
            search(picker);
            break;
        case 'open-seller-notes':
            event.preventDefault();
            openSellerNotes(button);
            break;
        case 'open-points':
            openPoints(button);
            break;
        case 'discuss-points':
            discussPoints(button);
            break;
        case 'retry-points':
            button.disabled = true;
            flushTalkingPoints().catch(error => showError(error, true, false))
                .finally(() => { button.disabled = false; });
            break;
        case 'open-picker': {
            const target = document.getElementById(`picker-${sectionId}`);
            const opening = target.classList.contains('d-none');
            target.classList.toggle('d-none');
            button.setAttribute('aria-expanded', String(opening));
            if (opening) {
                target.querySelector('input[name="q"]').focus();
                search(target);
            } else {
                searches.get(target)?.abort();
            }
            break;
        }
        case 'close-picker':
            searches.get(picker)?.abort();
            picker.classList.add('d-none');
            root.querySelector(`[data-action="open-picker"][data-section-id="${picker.dataset.sectionId}"]`)
                .setAttribute('aria-expanded', 'false');
            break;
        case 'delete-section':
            if (confirm('Delete this initiative and its report links? The engagements and milestones will not be deleted.')) {
                mutate(button, () => request(`${api}/sections/${sectionId}`, 'DELETE'));
            }
            break;
        case 'remove-item':
            mutate(button, () => request(`${api}/items/${button.dataset.itemId}`, 'DELETE'));
            break;
        case 'add-items': {
            const ids = Array.from(picker.querySelectorAll('input[type="checkbox"]:checked:not(:disabled)'),
                checkbox => Number(checkbox.value));
            mutate(button, () => request(`${api}/sections/${picker.dataset.sectionId}/items`, 'POST', {
                item_type: picker.querySelector('select[name="type"]').value,
                entity_ids: ids,
            }));
            break;
        }
        }
    });

    document.getElementById('managerPointsModal').addEventListener('hide.bs.modal', event => {
        if (pointsBusy) {
            event.preventDefault();
            return;
        }
        if (!root.querySelector('.talking-points-field[data-dirty="true"]')) return;
        event.preventDefault();
        flushTalkingPoints()
            .then(() => bootstrap.Modal.getOrCreateInstance(event.target).hide())
            .catch(error => showError(error, true, false));
    });

    window.addEventListener('beforeunload', event => {
        if (!root.querySelector('.talking-points-field[data-dirty="true"]')) return;
        event.preventDefault();
        event.returnValue = '';
    });

    window.addEventListener('storage', event => {
        if (event.key !== 'u2c_workload_filter' && event.key !== null) return;
        for (const picker of root.querySelectorAll('.work-picker:not(.d-none)')) {
            if (picker.dataset.source === 'u2c') search(picker);
        }
    });
})();
