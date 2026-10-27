/** Delegated, top-layer product evidence for static and injected revenue tables. */
((global) => {
    'use strict';

    function productLines(products) {
        if (!Array.isArray(products) || products.some(product =>
            typeof product.product !== 'string' || !Number.isFinite(product.monthly_revenue))) {
            throw new Error('Invalid revenue product breakdown.');
        }
        return products.map(product => ({
            name: product.product,
            amount: '$' + product.monthly_revenue.toLocaleString('en-US', {
                maximumFractionDigits: 0,
            }),
        }));
    }

    if (typeof module !== 'undefined' && module.exports) module.exports = {productLines};
    if (!global.document) return;
    const document = global.document;
    const selector = '[data-sb-revenue-breakdown]';
    const preview = document.createElement('div');
    preview.id = 'sbRevenueBreakdown';
    preview.className = 'sb-revenue-breakdown';
    preview.setAttribute('popover', 'manual');
    preview.setAttribute('role', 'tooltip');
    document.body.append(preview);
    let active = null;
    let timer = null;
    const observer = new MutationObserver(() => {
        if (active && !active.isConnected) close();
    });

    function close() {
        global.clearTimeout(timer);
        observer.disconnect();
        if (preview.matches(':popover-open')) preview.hidePopover();
        active?.removeAttribute('aria-describedby');
        active = null;
    }

    function show(anchor) {
        global.clearTimeout(timer);
        if (anchor === active) return;
        close();
        const heading = document.createElement('strong');
        heading.textContent = anchor.dataset.revenueLabel;
        const period = document.createElement('small');
        period.textContent = 'Avg monthly ACR, ' + anchor.dataset.revenuePeriod
            + '. Total divided by 3.';
        const list = document.createElement('dl');
        try {
            for (const product of productLines(JSON.parse(anchor.dataset.sbRevenueBreakdown))) {
                const name = document.createElement('dt');
                name.textContent = product.name;
                const amount = document.createElement('dd');
                amount.textContent = product.amount;
                list.append(name, amount);
            }
        } catch (error) {
            console.error('Revenue product breakdown failed', error);
            period.textContent = 'Product breakdown could not be displayed. Reload this view to retry.';
            list.replaceChildren();
        }
        preview.replaceChildren(heading, period, list);
        active = anchor;
        anchor.setAttribute('aria-describedby', preview.id);
        observer.observe(document.body, {childList: true, subtree: true});
        preview.showPopover();
        const box = anchor.getBoundingClientRect();
        const size = preview.getBoundingClientRect();
        preview.style.left = Math.max(12, Math.min(box.right - size.width,
            global.innerWidth - size.width - 12)) + 'px';
        const below = box.bottom + 8;
        preview.style.top = Math.max(12, below + size.height <= global.innerHeight - 12
            ? below : box.top - size.height - 8) + 'px';
    }

    function deferClose() {
        global.clearTimeout(timer);
        timer = global.setTimeout(() => {
            if (!preview.matches(':hover') && !active?.matches(':hover, :focus')) close();
        }, 120);
    }

    document.addEventListener('pointerover', event => {
        const anchor = event.target.closest(selector);
        if (anchor) show(anchor);
    });
    document.addEventListener('focusin', event => {
        const anchor = event.target.closest(selector);
        if (anchor) show(anchor);
        else close();
    });
    document.addEventListener('pointerout', event => {
        if (event.target.closest(selector)) deferClose();
    });
    document.addEventListener('focusout', deferClose);
    document.addEventListener('click', event => {
        const anchor = event.target.closest(selector);
        if (anchor) show(anchor);
        else if (!preview.contains(event.target)) close();
    });
    preview.addEventListener('pointerenter', () => global.clearTimeout(timer));
    preview.addEventListener('pointerleave', deferClose);
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') close();
    });
    document.addEventListener('scroll', event => {
        if (!preview.contains(event.target)) close();
    }, true);
    global.addEventListener('resize', close);
    global.addEventListener('pagehide', close);
})(typeof window === 'undefined' ? {} : window);
