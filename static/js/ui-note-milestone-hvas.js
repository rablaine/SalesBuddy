/* Cached milestone HVA context, independent of note saving and MSX refreshes. */
(() => {
    'use strict';

    const container = document.getElementById('selected_milestone');
    if (!container) return;
    const cache = new Map();
    const painted = new WeakMap();
    const statuses = new Set(['Open', 'Completed', 'Canceled', 'Unknown']);
    const tooltip = document.createElement('div');
    tooltip.id = 'noteMilestoneHvaDetails';
    tooltip.className = 'sb-note-hva-details';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.setAttribute('popover', 'manual');
    document.body.append(tooltip);
    let owner = null;
    let hideTimer;

    function hideDetails() {
        clearTimeout(hideTimer);
        if (tooltip.matches(':popover-open')) tooltip.hidePopover();
        owner?.removeAttribute('aria-describedby');
        owner = null;
    }

    function deferHide() {
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => {
            if (!owner?.matches(':hover, :focus') && !tooltip.matches(':hover')) hideDetails();
        }, 120);
    }

    function text(tag, className, value) {
        const element = document.createElement(tag);
        element.className = className;
        element.textContent = value;
        return element;
    }

    function showDetails(button, row) {
        hideDetails();
        owner = button;
        button.setAttribute('aria-describedby', tooltip.id);
        const details = document.createElement('dl');
        for (const [label, value] of [
            ['Category', row.category], ['Status', row.status],
            ['Duration', row.duration_minutes + ' minutes'],
            ['Created', row.created], ['Due', row.due], ['Completed', row.completed],
        ]) {
            const content = ['Created', 'Due', 'Completed'].includes(label) && value
                ? new Date(value).toLocaleString() : value || 'Not recorded';
            details.append(text('dt', '', label), text('dd', '', content));
        }
        tooltip.replaceChildren(
            text('strong', 'sb-note-hva-title', row.subject),
            text('p', 'sb-note-hva-description', row.description || 'No description recorded.'),
            details,
        );
        tooltip.showPopover();
        const anchor = button.getBoundingClientRect();
        const bounds = tooltip.getBoundingClientRect();
        tooltip.style.left = Math.max(12,
            Math.min(anchor.left, window.innerWidth - bounds.width - 12)) + 'px';
        const below = anchor.bottom + 6;
        tooltip.style.top = Math.max(12, below + bounds.height <= window.innerHeight - 12
            ? below : anchor.top - bounds.height - 6) + 'px';
    }

    function tag(row) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'sb-note-hva-tag';
        button.dataset.status = row.status.toLowerCase();
        button.setAttribute('aria-label', row.category + ': ' + row.subject + ', ' + row.status);
        const icon = document.createElement('i');
        icon.className = 'bi ' + ({
            Open: 'bi-clock', Completed: 'bi-check-circle',
            Canceled: 'bi-x-circle', Unknown: 'bi-question-circle',
        })[row.status];
        icon.setAttribute('aria-hidden', 'true');
        button.append(icon, text('span', '', row.category));
        button.addEventListener('mouseenter', () => showDetails(button, row));
        button.addEventListener('focus', () => showDetails(button, row));
        button.addEventListener('mouseleave', deferHide);
        button.addEventListener('blur', deferHide);
        return button;
    }

    function cards() {
        return [...container.querySelectorAll('[data-msx-milestone-id]')].filter(
            card => card.dataset.msxMilestoneId && card.dataset.msxMilestoneId !== 'None',
        );
    }

    function paint() {
        if (owner && !owner.isConnected) hideDetails();
        for (const card of cards()) {
            const id = card.dataset.msxMilestoneId.toLowerCase();
            let slot = card.querySelector('.sb-note-hva-history');
            if (!slot) {
                slot = document.createElement('div');
                slot.className = 'sb-note-hva-history';
                slot.setAttribute('aria-label', 'Logged HVAs');
                card.querySelector('.sb-nf-card-body').append(slot);
            }
            const entry = cache.get(id);
            if (entry && painted.get(slot) === entry) continue;
            painted.set(slot, entry);
            slot.setAttribute('aria-busy', String(entry?.state === 'loading'));
            if (entry?.state === 'ready' && entry.rows.length) {
                slot.replaceChildren(
                    text('span', 'sb-note-hva-label', 'Logged HVAs'),
                    ...entry.rows.map(tag),
                );
            } else if (entry?.state === 'error') {
                const retry = text('button', 'sb-nf-btn sb-nf-btn-sm sb-nf-btn-outline-secondary',
                    'Retry HVA history');
                retry.type = 'button';
                retry.addEventListener('click', () => {
                    cache.delete(id);
                    render();
                });
                slot.replaceChildren(text('span', 'sb-note-hva-label',
                    'Could not load logged HVAs.'), retry);
            } else {
                slot.replaceChildren(text('span', 'sb-note-hva-label',
                    entry?.state === 'ready' ? 'No HVAs in local history.' : 'Loading logged HVAs...'));
            }
        }
    }

    async function load(ids) {
        const entry = {state: 'loading'};
        for (const id of ids) cache.set(id, entry);
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        try {
            const query = new URLSearchParams();
            for (const id of ids) query.append('milestone_id', id);
            const response = await fetch('/api/msx/milestone-hvas?' + query, {
                signal: controller.signal,
            });
            if (!response.ok) throw new Error('HVA history HTTP ' + response.status);
            const data = await response.json();
            if (data.success !== true || !data.milestones || ids.some(id =>
                !Array.isArray(data.milestones[id]) || data.milestones[id].some(row =>
                    !row || typeof row.subject !== 'string' || typeof row.category !== 'string'
                    || !statuses.has(row.status)))) {
                throw new Error('Invalid milestone HVA history response.');
            }
            for (const id of ids) {
                if (cache.get(id) === entry) {
                    cache.set(id, {state: 'ready', rows: data.milestones[id]});
                }
            }
        } catch (error) {
            console.error('Could not load logged milestone HVAs.', error);
            for (const id of ids) {
                if (cache.get(id) === entry) cache.set(id, {state: 'error'});
            }
        } finally {
            clearTimeout(timeout);
            paint();
        }
    }

    function render() {
        const ids = [...new Set(cards().map(card => card.dataset.msxMilestoneId.toLowerCase()))]
            .filter(id => !cache.has(id));
        for (let start = 0; start < ids.length; start += 100) load(ids.slice(start, start + 100));
        paint();
    }

    function refresh() {
        hideDetails();
        cache.clear();
        render();
    }

    tooltip.addEventListener('mouseenter', () => clearTimeout(hideTimer));
    tooltip.addEventListener('mouseleave', deferHide);
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape' || !owner) return;
        event.preventDefault();
        event.stopPropagation();
        hideDetails();
    }, true);
    window.addEventListener('scroll', event => {
        if (!tooltip.contains(event.target)) hideDetails();
    }, true);
    window.addEventListener('resize', hideDetails);
    window.addEventListener('pagehide', hideDetails);
    window.SBMilestoneHvas = {render, refresh};
    render();
})();
