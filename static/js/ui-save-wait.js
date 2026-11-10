/* Observe actual write contention without taking ownership of a user's save. */
(() => {
    'use strict';
    const nativeFetch = window.fetch.bind(window);
    const pending = new Map();
    const forms = new WeakMap();
    let notice;
    function render() {
        const waiting = [...pending.values()].find(save => save.writer || save.shown);
        if (!notice && waiting) {
            notice = document.createElement('aside');
            notice.className = 'sb-save-wait';
            notice.setAttribute('role', 'status');
            notice.setAttribute('aria-live', 'polite');
            const heading = document.createElement('strong');
            heading.textContent = 'Waiting to save';
            notice.append(heading, document.createElement('p'), document.createElement('p'));
            notice.lastElementChild.textContent = 'If you run into this, let Alex know how you ran ' +
                'into this (did you manually start syncs, is this start of the day and syncs are ' +
                "running in the background, stuff like that). He clearly knew this could happen " +
                "but didn't think it actually would so now you gotta wait.";
            document.body.append(notice);
        }
        if (!notice) return;
        notice.hidden = !waiting;
        if (waiting) {
            waiting.shown = true;
            notice.querySelector('p').textContent = waiting.writer ?
                `${waiting.writer.name} sync is saving data. Your save will continue when it ` +
                'finishes. Keep this page open until saving completes.' :
                'The sync finished saving. Your save is still pending. ' +
                'Keep this page open until saving completes.';
        }
    }
    function end(id) {
        const save = pending.get(id);
        if (save) clearTimeout(save.timer);
        if (save?.form) forms.delete(save.form);
        pending.delete(id);
        render();
    }
    function begin(id, form) {
        const save = {form, shown: false};
        pending.set(id, save);
        async function check() {
            try {
                const response = await nativeFetch(`/api/jobs/write-status?save_id=${id}`,
                    {cache: 'no-store'});
                const data = await response.json();
                if (!response.ok || !data.success) throw new Error(data.error ||
                    'Could not check the database write wait.');
                if (!pending.has(id)) return;
                if (data.save?.pending === false) {
                    end(id);
                    if (form) forms.delete(form);
                    return;
                }
                save.writer = data.save?.pending && data.save.waiting ? data.writer : null;
                render();
            } catch (error) {
                console.error('Save wait status unavailable', error);
                if (pending.has(id) && save.shown) {
                    save.writer = null;
                    render();
                    notice.querySelector('p').textContent =
                        'Save status is unavailable. Keep this page open and wait for the save result.';
                }
            }
            if (pending.has(id)) save.timer = setTimeout(check, 1000);
        }
        save.timer = setTimeout(check, 750);
    }
    window.fetch = async (input, options = {}) => {
        const isRequest = input instanceof Request;
        const url = new URL(isRequest ? input.url : String(input), window.location.href);
        const method = (options.method || (isRequest ? input.method : 'GET')).toUpperCase();
        if (url.origin !== window.location.origin || ['GET', 'HEAD', 'OPTIONS'].includes(method) ||
            url.pathname.startsWith('/api/jobs')) return nativeFetch(input, options);
        const id = crypto.randomUUID();
        const headers = new Headers(options.headers || (isRequest ? input.headers : undefined));
        headers.set('X-SB-Save-Id', id);
        begin(id);
        try {
            return await nativeFetch(input, {...options, headers});
        } finally {
            end(id);
        }
    };
    document.addEventListener('submit', event => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || form.method.toUpperCase() !== 'POST' ||
            new URL(form.action, window.location.href).origin !== window.location.origin) return;
        if (forms.has(form)) {
            event.preventDefault();
            event.stopImmediatePropagation();
            return;
        }
        const id = crypto.randomUUID();
        let field = form.querySelector('[name="_sb_save_id"]');
        if (!field) {
            field = document.createElement('input');
            field.type = 'hidden';
            field.name = '_sb_save_id';
            form.append(field);
        }
        field.value = id;
        // Native dispatch may run microtasks before the form's validation cancels submission.
        setTimeout(() => {
            if (event.defaultPrevented) return;
            forms.set(form, id);
            begin(id, form);
        }, 0);
    }, true);
    window.addEventListener('pagehide', () => [...pending.keys()].forEach(end));
    window.addEventListener('pageshow', () => {
        for (const [id, save] of pending) {
            if (save.form) forms.delete(save.form);
            end(id);
        }
    });
})();
