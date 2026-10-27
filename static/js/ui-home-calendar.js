/* Approved calendar presentation; data loading and actions stay with Home. */
(() => {
    'use strict';

    function customerColors(data) {
        const ids = new Set();
        for (const entries of [...Object.values(data.days || {}),
            ...Object.values(data.ghosts || {})]) {
            for (const entry of entries) if (entry.customer_id) ids.add(entry.customer_id);
        }
        return Object.fromEntries([...ids].map((id, index) => [id, index % 15 + 1]));
    }

    function workStatus(note) {
        return note.has_hva ? 'hva' : note.has_task ? 'task' :
            note.has_milestone ? 'milestone' : 'plain';
    }

    function color(entry, index) {
        if (index == null) return;
        if (!Number.isInteger(index) || index < 1 || index > 15) {
            throw new Error('Invalid calendar customer color: ' + index);
        }
        entry.dataset.customerColor = String(index);
    }

    function marker(name, title) {
        const icon = document.createElement('i');
        icon.className = 'bi bi-' + name;
        icon.title = title;
        icon.setAttribute('aria-hidden', 'true');
        return icon;
    }

    function copy(entry, time, label) {
        if (time) {
            const stamp = document.createElement('span');
            stamp.className = 'sb-home-entry-time';
            stamp.textContent = time;
            entry.appendChild(stamp);
        }
        const title = document.createElement('span');
        title.className = 'sb-home-entry-title';
        title.textContent = label;
        entry.appendChild(title);
    }

    function note(entry, data, index) {
        entry.className = 'sb-home-entry sb-home-note';
        entry.dataset.workStatus = workStatus(data);
        color(entry, data.is_general ? null : index);
        if (data.is_general) entry.appendChild(marker('sticky', 'General note'));
        copy(entry, data.time, data.customer_name);
        if (data.has_engagement) {
            entry.appendChild(marker('diagram-3', 'Linked engagement'));
        }
        const status = [
            data.is_general ? 'General note' : 'Saved note',
            data.has_milestone ? 'Milestone linked' : '',
            data.has_task ? 'MSX task linked' : '',
            data.has_hva ? 'HVA task' : '',
            data.has_engagement ? 'Engagement linked' : '',
        ].filter(Boolean).join(' / ');
        entry.title = [data.time, data.customer_name, status].filter(Boolean).join(' / ');
        entry.setAttribute('aria-label', entry.title);
    }

    function ghost(entry, data, index, time) {
        entry.className = 'sb-home-entry cal-ghost' +
            (data.customer_id ? '' : ' cal-ghost-unmatched');
        color(entry, index);
        const opener = document.createElement(data.customer_id ? 'a' : 'button');
        opener.className = 'sb-home-ghost-open';
        if (!data.customer_id) opener.type = 'button';
        const label = data.customer_id ? data.customer_name || data.subject : data.subject;
        opener.appendChild(marker('calendar-event', 'Cached meeting; no note yet'));
        copy(opener, time, label);
        opener.setAttribute('aria-label', [time, label, data.subject !== label ? data.subject : '',
            'Cached meeting; no saved note', !data.customer_id ? 'Customer not matched' : '']
            .filter(Boolean).join(' / '));
        if (!data.customer_id) {
            opener.appendChild(marker('question-circle', 'Customer not matched'));
        }
        entry.appendChild(opener);
        if (data.is_recurring) {
            const recur = document.createElement('button');
            recur.type = 'button';
            recur.className = 'cal-ghost-recur';
            recur.dataset.role = 'recur';
            recur.setAttribute('aria-label', 'Hide recurring meeting series: ' + data.subject);
            recur.title = 'Recurring meeting - hover to dismiss series';
            recur.appendChild(marker('arrow-repeat', 'Recurring meeting'));
            entry.appendChild(recur);
        }
        return opener;
    }

    function bindHighlight(entry) {
        function update() {
            const body = document.getElementById('calendarBody');
            const hovered = body.querySelector('.sb-home-entry[data-customer-id]:hover');
            const focused = document.activeElement?.closest('.sb-home-entry[data-customer-id]');
            const active = hovered || (focused && body.contains(focused) ? focused : null);
            for (const item of body.querySelectorAll('.sb-home-entry[data-customer-id]')) {
                item.classList.toggle('cal-highlight',
                    !!active && item.dataset.customerId === active.dataset.customerId);
            }
        }
        for (const name of ['mouseenter', 'mouseleave', 'focusin', 'focusout']) {
            entry.addEventListener(name, update);
        }
    }

    function clearRefreshError(date) {
        document.getElementById('homeMeetingRefreshError-' + (date || 'status'))
            ?.querySelector('[data-sb-notice-dismiss]').click();
    }

    function validateMonth(data, year, month) {
        const dayCount = new Date(year, month, 0).getDate();
        if (!Number.isInteger(year) || year < 1 || year > 9999 ||
            !Number.isInteger(month) || month < 1 || month > 12 ||
            !data || data.year !== year || data.month !== month ||
            typeof data.month_name !== 'string' || !data.month_name.trim() ||
            data.days_in_month !== dayCount) {
            throw new Error('Calendar response has invalid month data.');
        }
        for (const name of ['days', 'ghosts']) {
            const entries = data[name];
            if (!entries || typeof entries !== 'object' || Array.isArray(entries)) {
                throw new Error('Calendar response has invalid ' + name + '.');
            }
            for (const [key, records] of Object.entries(entries)) {
                const day = Number(key);
                if (!Number.isInteger(day) || day < 1 || day > dayCount ||
                    String(day) !== key || !Array.isArray(records) ||
                    records.some(record => !record || !Number.isInteger(record.id) ||
                        record.id < 1)) {
                    throw new Error('Calendar response has invalid ' + name + ' for day ' + key);
                }
            }
        }
    }

    function showReadState(body, state, retry) {
        if (!['loading', 'error'].includes(state) ||
            (state === 'error' && typeof retry !== 'function')) {
            throw new Error('Invalid calendar read state: ' + state);
        }
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 7;
        cell.className = 'sb-home-calendar-state';
        const content = document.createElement('div');
        content.className = 'sb-home-calendar-state-content';
        content.appendChild(marker(state === 'loading' ? 'hourglass-split' :
            'exclamation-circle', state === 'loading' ? 'Loading' : 'Error'));
        const message = document.createElement('span');
        message.className = 'sb-home-calendar-state-message';
        message.setAttribute('role', state === 'loading' ? 'status' : 'alert');
        message.textContent = state === 'loading' ? 'Loading calendar...' :
            'Unable to load the calendar.';
        content.appendChild(message);
        if (state === 'error') {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'sb-home-button sb-home-calendar-retry';
            button.textContent = 'Retry';
            button.addEventListener('click', retry);
            content.appendChild(button);
        }
        cell.appendChild(content);
        row.appendChild(cell);
        body.dataset.sbCalendarState = state;
        body.setAttribute('aria-busy', String(state === 'loading'));
        body.replaceChildren(row);
    }

    function showRefreshError(date, message, retry, refreshed = false) {
        const id = 'homeMeetingRefreshError-' + (date || 'status');
        let notice = document.getElementById(id);
        if (!notice) {
            notice = document.createElement('div');
            notice.id = id;
            notice.className = 'sb-notice';
            notice.setAttribute('data-sb-tone', 'danger');
            notice.setAttribute('data-sb-category', 'error');
            notice.setAttribute('role', 'alert');
            notice.setAttribute('aria-label', 'Meeting refresh error');
            const icon = marker('exclamation-circle', 'Error');
            icon.className += ' sb-notice-icon';
            notice.appendChild(icon);
            const body = document.createElement('div');
            body.className = 'sb-notice-body';
            notice.appendChild(body);
            const close = document.createElement('button');
            close.type = 'button';
            close.className = 'sb-notice-dismiss';
            close.setAttribute('data-sb-notice-dismiss', '');
            close.setAttribute('aria-label', 'Dismiss notification');
            close.appendChild(marker('x-lg', 'Close'));
            notice.appendChild(close);
            document.getElementById('homeMeetingNotices').appendChild(notice);
        }
        const body = notice.querySelector('.sb-notice-body');
        body.textContent = date && !refreshed ?
            'Calendar refresh failed for ' + date + '. Your saved meetings are unchanged.' :
            message;
        if (date && typeof retry === 'function') {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'sb-home-button';
            button.textContent = 'Retry';
            button.setAttribute('aria-label', 'Retry meeting refresh for ' + date);
            button.addEventListener('click', retry);
            body.appendChild(button);
        }
    }

    window.SBHomeCalendar = Object.freeze({
        customerColors, workStatus, note, ghost, bindHighlight, showRefreshError, clearRefreshError,
        validateMonth, showReadState,
    });
})();
