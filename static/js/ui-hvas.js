/* HVA inventory navigation delegates durable work and sorting controls to shared helpers. */
(() => {
    'use strict';
    const root = document.querySelector('.sb-hvas');
    if (!root) return;
    root.querySelectorAll('[data-hva-action]').forEach(action => {
        const categoryChange = action.dataset.action === 'category';
        const button = action.querySelector(
            categoryChange ? '[data-hva-category]' : '[data-hva-toggle]',
        );
        const message = action.closest('td').querySelector('[data-hva-result]');
        const reopening = action.dataset.action === 'reopen';
        const originalValue = button.value;
        let pending = false;
        let completed = false;
        button.addEventListener(categoryChange ? 'change' : 'click', async () => {
            if (pending || completed) return;
            if (categoryChange && button.value === originalValue) return;
            pending = true;
            action.setAttribute('aria-busy', 'true');
            button.disabled = true;
            message.hidden = false;
            message.setAttribute('role', 'status');
            message.textContent = categoryChange ? 'Updating category in MSX...' :
                (reopening ? 'Reopening in MSX...' : 'Completing in MSX...');
            try {
                const options = {
                    method: 'POST', headers: {'Accept': 'application/json'},
                };
                if (categoryChange) {
                    options.headers['Content-Type'] = 'application/json';
                    options.body = JSON.stringify({category: Number(button.value)});
                }
                const response = await fetch(action.dataset.actionUrl, options);
                const result = await response.json().catch(error => {
                    throw new Error(
                        'Could not read the status response. Check MSX before retrying.',
                        {cause: error},
                    );
                });
                if (!result || typeof result !== 'object') {
                    throw new Error('Could not read the status response. Check MSX.');
                }
                if (!response.ok || result.success !== true) {
                    completed = result.remote_changed === true;
                    throw new Error(result.error || 'MSX could not update this HVA.');
                }
                completed = true;
                window.location.reload();
            } catch (error) {
                console.error('HVA status update failed:', error);
                message.setAttribute('role', 'alert');
                message.textContent = error.message || 'Could not update this HVA. Check MSX.';
                if (!completed) {
                    if (categoryChange) button.value = originalValue;
                    button.disabled = false;
                    button.focus();
                }
            } finally {
                pending = false;
                action.setAttribute('aria-busy', 'false');
                button.disabled = completed;
            }
        });
    });
    const table = root.querySelector('#hvaTable');
    table?.addEventListener('sb:table-sort', event => {
        const header = table.querySelectorAll('thead th')[event.detail.column];
        const url = new URL(window.location.href);
        url.searchParams.set('sort', header.dataset.hvaSort);
        url.searchParams.set('direction', event.detail.direction === 'ascending' ? 'asc' : 'desc');
        url.searchParams.set('page', '1');
        window.location.assign(url);
    });
    root.querySelector('#hvaPageSize').addEventListener('change', event => {
        const url = new URL(window.location.href);
        url.searchParams.set('per_page', event.target.value);
        url.searchParams.set('page', '1');
        window.location.assign(url);
    });
    const progress = window.SBSyncProgress.create(
        root.querySelector('[data-sb-sync-progress]'),
        {onComplete: () => window.location.reload()},
    );
    root.querySelector('#hvaRefresh').addEventListener('click', () => {
        progress.start().catch(error => console.error('HVA task refresh failed:', error));
    });
})();
