const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/js/ui-home-calendar.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../../templates/index.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../../static/css/ui-home-calendar.css'), 'utf8');

for (const firstRun of [true, false, null]) {
    test(`meeting refresh observation respects first-run setup (${firstRun})`, async () => {
        let reads = 0;
        const context = {
            document: {getElementById: id => {
                assert.equal(id, 'welcomeModal');
                return firstRun === null ? null : {dataset: {sbAutoOpen: String(firstRun)}};
            }},
            meetingRefresh: {check: async () => { reads++; }},
        };
        const start = template.indexOf('    function checkAuraSync()');
        const end = template.indexOf('    // Reload drafts', start);
        vm.runInNewContext(template.slice(start, end), context);
        await context.checkAuraSync();
        assert.equal(reads, firstRun ? 0 : 1);
    });
}

function fixture() {
    const all = [];
    function element(tag = 'div') {
        const attrs = {}, listeners = {}, classes = new Set();
        const e = {tagName: tag, dataset: {}, style: {}, children: [], textContent: '',
            className: '', classList: {add: name => classes.add(name),
                remove: name => e.classList.toggle(name, false),
                contains: name => classes.has(name) || e.className.split(' ').includes(name),
                toggle: (name, state) => {
                    if (state) classes.add(name);
                    else {
                        classes.delete(name);
                        e.className = e.className.split(' ').filter(c => c !== name).join(' ');
                    }
                }},
            setAttribute: (key, value) => {
                attrs[key] = String(value);
                if (key.startsWith('data-')) e.dataset[key.slice(5).replace(/-([a-z])/g,
                    (_, letter) => letter.toUpperCase())] = String(value);
            },
            getAttribute: key => attrs[key],
            appendChild: child => { e.children.push(child); child.parent = e; return child; },
            replaceChildren: (...children) => {
                e.children.forEach(child => { child.parent = null; });
                e.children = [];
                children.forEach(child => e.appendChild(child));
            },
            focus: () => { document.activeElement = e; },
            insertBefore: (child, before) => {
                e.children.splice(before ? e.children.indexOf(before) : e.children.length, 0, child);
                child.parent = e;
            },
            remove: () => {
                e.parent.children = e.parent.children.filter(child => child !== e);
                e.parent = null;
            },
            replaceWith: next => {
                e.parent.children[e.parent.children.indexOf(e)] = next; next.parent = e.parent;
            },
            addEventListener: (name, handler) => { (listeners[name] ||= []).push(handler); },
            click: () => {
                const result = e.dispatch('click');
                if (e.getAttribute('data-sb-notice-dismiss') !== undefined) e.parent.remove();
                return result;
            },
            dispatch: (name, extra = {}) => {
                const event = {target: e, preventDefault() {}, stopPropagation() {}, ...extra};
                let result;
                for (const handler of listeners[name] || []) result = handler(event);
                return result;
            },
            querySelector: selector => e.children.find(child =>
                selector === '[data-role="recur"]' && child.dataset.role === 'recur' ||
                selector === '[data-sb-notice-dismiss]' &&
                    child.getAttribute('data-sb-notice-dismiss') !== undefined ||
                selector.startsWith('.') && child.className.split(' ').includes(selector.slice(1))) || null,
            closest: selector => selector.startsWith('td') && tag === 'td' ? e :
                selector.includes('data-customer-id') && e.dataset.customerId ? e :
                    e.parent?.closest(selector) || null};
        let text = '';
        Object.defineProperty(e, 'textContent', {
            get: () => text,
            set: value => {
                text = value;
                e.children.forEach(child => { child.parent = null; });
                e.children = [];
            },
        });
        all.push(e);
        return e;
    }
    const body = element(), flash = element(), notices = element(), document = {activeElement: null,
        createElement: element,
        getElementById: id => id === 'calendarBody' ? body : id === 'homeMeetingNotices' ?
            notices : all.find(e => e.id === id && e.parent),
        querySelector: selector => selector === '.sb-flash-stack' ? flash : null};
    let hovered = null;
    body.querySelector = () => hovered;
    body.querySelectorAll = () => all.filter(e => e.dataset.customerId && e.inBody);
    body.contains = e => !!e.inBody;
    const window = {};
    const context = {window, document, Date};
    vm.runInNewContext(source, context);
    return {api: window.SBHomeCalendar, element, document, body, flash, notices, context,
        hover: e => { hovered = e; }};
}

test('day refresh errors preserve saved data, hide parser internals and clear per date', () => {
    const {api, flash, notices, document} = fixture();
    api.showRefreshError('2026-10-03', '<img onerror=bad()>');
    api.showRefreshError('2026-10-03', 'WorkIQ lookup failed');
    api.showRefreshError('2026-10-04', 'Network error');
    assert.equal(notices.children.length, 2);
    assert.equal(flash.children.length, 0);
    const notice = document.getElementById('homeMeetingRefreshError-2026-10-03');
    assert.equal(notice.getAttribute('role'), 'alert');
    assert.equal(notice.dataset.sbTone, 'danger');
    assert.equal(notice.children[2].getAttribute('data-sb-notice-dismiss'), '');
    assert.match(notice.children[1].textContent, /2026-10-03.*saved meetings are unchanged/);
    assert.doesNotMatch(notice.children[1].textContent, /WorkIQ lookup failed/);
    api.showRefreshError('2026-10-03', '<literal details>');
    assert.doesNotMatch(notice.children[1].textContent, /<literal details>/);
    api.clearRefreshError('2026-10-03');
    assert.equal(notices.children.length, 1);
    assert.equal(notices.children[0].id, 'homeMeetingRefreshError-2026-10-04');
});

test('per-date notices offer one accessible Retry while status errors have no date action', () => {
    const {api, notices, document} = fixture();
    const retried = [];
    for (let n = 0; n < 2; n++) {
        api.showRefreshError('2026-10-03', 'multiple calendar response payloads',
            () => retried.push('2026-10-03'));
    }
    const body = document.getElementById('homeMeetingRefreshError-2026-10-03').children[1];
    assert.equal(body.children.length, 1);
    const retry = body.children[0];
    assert.equal(retry.type, 'button');
    assert.equal(retry.textContent, 'Retry');
    assert.equal(retry.getAttribute('aria-label'), 'Retry meeting refresh for 2026-10-03');
    retry.click();
    assert.deepEqual(retried, ['2026-10-03']);
    api.showRefreshError(null, 'Reconnecting to refresh status.');
    assert.equal(notices.children[1].children[1].children.length, 0);
    assert.match(notices.children[1].children[1].textContent, /Reconnecting/);
});

test('notice Retry readmits the same date through the durable refresh lifecycle', async () => {
    const f = dayRefreshFixture(new Error('WorkIQ parse failed'));
    await f.context.triggerDaySync('2026-10-03', null);
    const retry = f.notices.children[0].children[1].children[0];
    await retry.click();
    assert.deepEqual(f.requests, [
        {operation: 'meetings', options: {date: '2026-10-03'}},
        {operation: 'meetings', options: {date: '2026-10-03'}},
    ]);
    assert.equal(f.notices.children.length, 1);
    assert.equal(f.events.length, 0);
    assert.equal(f.redraws.length, 0);
});

test('meeting notifications float outside layout with a bounded scrollable stack', () => {
    const homeCSS = fs.readFileSync(path.join(__dirname, '../../static/css/ui-home.css'), 'utf8');
    const stack = homeCSS.match(/\.sb-home-meeting-notices \{([^}]+)\}/)[1];
    assert.match(stack, /position: fixed/);
    assert.match(stack, /bottom: 32px/);
    assert.match(stack, /max-height: min\(400px, calc\(100dvh/);
    assert.match(stack, /overflow: auto/);
    assert.match(homeCSS, /\.sb-home-meeting-notices:empty \{ display: none; \}/);
    assert.match(homeCSS, /\.sb-home-meeting-notices > \.sb-notice \{ margin: 0; \}/);
});

function dayRefreshFixture(reply, month = 10) {
    const f = fixture(), requests = [], events = [], spins = [], redraws = [], errors = [];
    const busy = new Set(), painted = new Map();
    let snapshots = [];
    f.context.window.dispatchEvent = event => events.push(event);
    f.context.window.addEventListener = () => {};
    f.context.window.SBJobs = {start: async (operation, options) => {
        requests.push({operation, options: {...options}});
        if (reply instanceof Error) throw reply;
        if (!reply.ok) throw new Error((await reply.json()).error || 'Admission failed');
        const result = await reply.json();
        const job = {id: 1, operation, options, status: 'done', state: 'done',
            calendar_dates: [options.date], progress: {}, result};
        snapshots = [job];
        return job;
    }};
    Object.assign(f.context, {CustomEvent, calendarYear: 2026, calendarMonth: month,
        URLSearchParams, setTimeout: () => 1, clearTimeout() {},
        _pendingRedrawDates: new Set(),
        applyMeetingIndicators: states => {
            const dates = new Set([...painted.keys(), ...states.keys()]);
            for (const date of dates) {
                const on = states.has(date);
                if (painted.get(date) !== on) spins.push([date, on]);
                painted.set(date, on);
                if (on) busy.add(date);
                else busy.delete(date);
            }
        },
        refreshDaysInPlace: dates => redraws.push(Array.from(dates)),
        console: {error: (...args) => errors.push(args)},
        fetch: async () => ({ok: true, json: async () => ({jobs: snapshots})})});
    vm.runInNewContext(fs.readFileSync(path.join(
        __dirname, '../../static/js/ui-meeting-refresh.js'), 'utf8'), f.context);
    const start = template.indexOf('    const meetingRefresh = ');
    const end = template.indexOf('    // ===== TODAY-CELL HEALTH', start);
    vm.runInNewContext(template.slice(start, end), f.context);
    return {...f, requests, events, spins, redraws, errors, busy};
}

function monthData(year = 2026, month = 9) {
    return {year, month, month_name: new Date(year, month - 1, 1)
        .toLocaleDateString('en-US', {month: 'long'}),
    days_in_month: new Date(year, month, 0).getDate(), days: {}, ghosts: {}};
}

function monthFixture(response) {
    const f = fixture(), requests = [], rendered = [], effects = [], errors = [], events = {};
    const title = f.element(), tab = f.element('button');
    const getElement = f.document.getElementById;
    f.document.getElementById = id => id === 'calendarTitle' ? title :
        id === 'calendar-tab' ? tab : getElement(id);
    f.body.contains = element => {
        for (let node = element; node; node = node.parent) if (node === f.body) return true;
        return false;
    };
    let nextResponse = response;
    f.context.window.addEventListener = (name, callback) => { events[name] = callback; };
    Object.assign(f.context, {AbortController, calendarYear: 2026, calendarMonth: 9,
        console: {error: (...args) => errors.push(args)},
        fetch: async (url, options) => {
            requests.push({url, options});
            if (typeof nextResponse === 'function') return nextResponse(url);
            if (nextResponse instanceof Error) throw nextResponse;
            return nextResponse || {ok: true, json: async () => monthData()};
        },
        renderCalendar: data => {
            rendered.push(data);
            f.body.replaceChildren(f.element('tr'));
        },
        checkTodayStatus: () => effects.push('today'),
        checkAuraSync: () => effects.push('aura'),
        _restoreInFlightSpinners: () => effects.push('spinners')});
    const start = template.indexOf('    let calendarRead = null;');
    const end = template.indexOf('    function renderCalendar(', start);
    vm.runInNewContext(template.slice(start, end), f.context);
    return {...f, title, tab, requests, rendered, effects, errors, events,
        panel: () => f.body.children[0].children[0].children[0],
        setResponse: value => { nextResponse = value; }};
}

test('month validation accepts confirmed empty/leap months and rejects malformed month/record data', () => {
    const {api} = fixture();
    api.validateMonth(monthData(), 2026, 9);
    api.validateMonth(monthData(2024, 2), 2024, 2);
    for (const value of [
        null, {}, {...monthData(), year: 2025}, {...monthData(), month: 10},
        {...monthData(), month_name: ''}, {...monthData(), days_in_month: 31},
        {...monthData(), days: null}, {...monthData(), ghosts: []},
        {...monthData(), days: {'31': []}}, {...monthData(), ghosts: {'0': []}},
        {...monthData(), days: {'01': []}}, {...monthData(), days: {'1': {}}},
        {...monthData(), ghosts: {'1': [null]}}, {...monthData(), days: {'1': [{id: 0}]}},
    ]) assert.throws(() => api.validateMonth(value, 2026, 9), /Calendar response/);
    assert.throws(() => api.validateMonth(monthData(2026, 0), 2026, 0), /invalid month/);
});

test('native read states expose a full-width status/alert, busy state and keyboard-native Retry', () => {
    const {api, body} = fixture();
    api.showReadState(body, 'loading');
    const cell = body.children[0].children[0];
    assert.equal(cell.colSpan, 7);
    assert.equal(body.getAttribute('aria-busy'), 'true');
    assert.equal(cell.children[0].children[1].getAttribute('role'), 'status');
    assert.equal(cell.children[0].children[1].textContent, 'Loading calendar...');
    let retried = 0;
    api.showReadState(body, 'error', () => { retried++; });
    const content = body.children[0].children[0].children[0];
    assert.equal(body.getAttribute('aria-busy'), 'false');
    assert.equal(body.dataset.sbCalendarState, 'error');
    assert.equal(content.children[1].getAttribute('role'), 'alert');
    assert.equal(content.children[2].tagName, 'button');
    assert.equal(content.children[2].type, 'button');
    content.children[2].click();
    assert.equal(retried, 1);
    assert.doesNotMatch(content.children.map(child => child.className).join(' '),
        /spinner-border|text-danger|py-4/);
    assert.throws(() => api.showReadState(body, 'error'), /Invalid calendar read state/);
});

test('month reads retain the GET contract, announce the requested month and preserve paint hooks', async () => {
    const f = monthFixture();
    const pending = f.context.loadCalendar(2026, 9);
    assert.equal(f.title.textContent, 'September 2026');
    assert.equal(f.body.dataset.sbCalendarState, 'loading');
    await pending;
    assert.equal(f.requests[0].url, '/api/notes/calendar?year=2026&month=9');
    assert.equal(f.requests[0].options.method, undefined);
    assert.equal(f.rendered.length, 1);
    assert.deepEqual(f.effects, ['today', 'aura', 'spinners']);
    assert.equal(f.body.dataset.sbCalendarState, 'ready');
    assert.equal(f.body.getAttribute('aria-busy'), 'false');
});

for (const [description, response] of [
    ['HTTP error', {ok: false, status: 503}],
    ['network error', new Error('Network unavailable')],
    ['JSON error', {ok: true, json: async () => { throw new Error('Bad JSON'); }}],
    ['unconfirmed month', {ok: true, json: async () => ({error: 'Unavailable'})}],
    ['wrong month', {ok: true, json: async () => monthData(2026, 10)}],
]) {
    test('month read ' + description + ' is logged, visible and retryable, not empty success', async () => {
        const f = monthFixture(response);
        await f.context.loadCalendar(2026, 9);
        assert.equal(f.body.dataset.sbCalendarState, 'error');
        assert.equal(f.body.getAttribute('aria-busy'), 'false');
        assert.match(f.panel().children[1].textContent, /Unable to load/);
        assert.equal(f.panel().children[2].textContent, 'Retry');
        assert.equal(f.errors.length, 1);
        assert.equal(f.rendered.length, 0);
        assert.equal(f.effects.length, 0);
    });
}

test('Retry rereads the same month and restores focus before replacing its control', async () => {
    const f = monthFixture({ok: false, status: 500});
    await f.context.loadCalendar(2026, 9);
    f.setResponse({ok: true, json: async () => monthData()});
    const retry = f.panel().children[2];
    retry.focus();
    await retry.click();
    assert.equal(f.document.activeElement, f.tab);
    assert.equal(f.requests.length, 2);
    assert.equal(f.requests[1].url, f.requests[0].url);
    assert.equal(f.body.dataset.sbCalendarState, 'ready');
});

for (const failed of [false, true]) {
    test('superseded month ' + (failed ? 'failures' : 'responses') +
        ' cannot repaint a newer month or clear its busy state', async () => {
        const replies = [];
        const f = monthFixture(() => new Promise((resolve, reject) => {
            replies.push({resolve, reject});
        }));
        const older = f.context.loadCalendar(2026, 9);
        const newer = f.context.loadCalendar(2026, 10);
        assert.equal(f.requests[0].options.signal.aborted, true);
        assert.equal(f.title.textContent, 'October 2026');
        if (failed) replies[0].reject(new Error('Old failed read'));
        else replies[0].resolve({ok: true, json: async () => monthData()});
        await older;
        assert.equal(f.body.getAttribute('aria-busy'), 'true');
        assert.equal(f.rendered.length, 0);
        assert.equal(f.errors.length, 0);
        replies[1].resolve({ok: true, json: async () => monthData(2026, 10)});
        await newer;
        assert.equal(f.rendered.length, 1);
        assert.equal(f.rendered[0].month, 10);
        assert.equal(f.body.getAttribute('aria-busy'), 'false');
    });
}

test('page exit aborts reads, late replies stay ignored and cached-back navigation resumes loading', async () => {
    let finish;
    const f = monthFixture(() => new Promise(resolve => { finish = resolve; }));
    const pending = f.context.loadCalendar(2026, 9);
    f.events.pagehide();
    assert.equal(f.requests[0].options.signal.aborted, true);
    finish({ok: true, json: async () => monthData()});
    await pending;
    assert.equal(f.rendered.length, 0);
    assert.equal(f.effects.length, 0);
    f.setResponse({ok: true, json: async () => monthData()});
    f.events.pageshow({persisted: false});
    assert.equal(f.requests.length, 1);
    await f.events.pageshow({persisted: true});
    assert.equal(f.requests.length, 2);
    assert.equal(f.rendered.length, 1);
    assert.equal(f.body.dataset.sbCalendarState, 'ready');
    f.events.pageshow({persisted: true});
    assert.equal(f.requests.length, 2);
});

test('late replies after the newer month has completed cannot replace its calendar or error state', async () => {
    for (const failed of [false, true]) {
        const replies = [];
        const f = monthFixture(() => new Promise((resolve, reject) => {
            replies.push({resolve, reject});
        }));
        const older = f.context.loadCalendar(2026, 9);
        const newer = f.context.loadCalendar(2026, 10);
        replies[1].resolve({ok: true, json: async () => monthData(2026, 10)});
        await newer;
        if (failed) replies[0].reject(new Error('Late failed read'));
        else replies[0].resolve({ok: true, json: async () => monthData()});
        await older;
        assert.equal(f.title.textContent, 'October 2026');
        assert.equal(f.rendered.length, 1);
        assert.equal(f.body.dataset.sbCalendarState, 'ready');
        assert.equal(f.errors.length, 0);
    }
});

test('unmatched meetings and empty days open the native picker with separate meeting context', () => {
    const stamp = {textContent: ''}, updates = [], opened = [];
    const context = {Date, quickCreateDate: null, encodeURIComponent,
        document: {getElementById: id => {
            assert.equal(id, 'quickCreateDate');
            return stamp;
        }},
        window: {location: {href: ''},
            _quickCreateUpdateDate: (...args) => updates.push(args),
            SBDialog: {open: id => opened.push(id)}}};
    const start = template.indexOf('    function openQuickCreateModal(');
    const end = template.indexOf('    function dismissGhost(', start);
    vm.runInNewContext(template.slice(start, end), context);
    context.openGhostMeeting({id: 42, customer_id: null}, '2026-10-09');
    context.openQuickCreateModal('2026-10-12');
    assert.deepEqual(updates, [['2026-10-09', 42], ['2026-10-12', null]]);
    assert.deepEqual(opened, ['quickCreateModal', 'quickCreateModal']);
    assert.match(stamp.textContent, /Oct 12, 2026/);
    context.openGhostMeeting({id: 7, customer_id: 44}, '2026-10-01');
    assert.equal(context.window.location.href,
        '/note/new?customer_id=44&date=2026-10-01&from_meeting=7');
    assert.equal(opened.length, 2);
});

for (const [description, reply] of [
    ['HTTP error', {ok: false, status: 500,
        json: async () => ({error: 'WorkIQ calendar lookup was not confirmed'})}],
    ['network error', new Error('Connection lost')],
    ['JSON error', {ok: true, json: async () => { throw new Error('Invalid JSON'); }}],
    ['unconfirmed cache', {ok: true, json: async () => ({meetings: []})}],
]) {
    test('day refresh ' + description + ' is visible and cannot announce successful cache refresh', async () => {
        const f = dayRefreshFixture(reply);
        await f.context.triggerDaySync('2026-10-03', null);
        assert.equal(f.notices.children.length, 1);
        assert.match(f.notices.children[0].children[1].textContent, /Calendar refresh failed/);
        assert.equal(f.errors.length, 1);
        assert.equal(f.events.length, 0);
        assert.equal(f.redraws.length, 0);
        assert.equal(f.busy.size, 0);
        assert.deepEqual(f.spins, [['2026-10-03', true], ['2026-10-03', false]]);
    });
}

test('confirmed empty calendar pulls notify freshness, clear their error, and use durable admission', async () => {
    const f = dayRefreshFixture({ok: true, json: async () => ({
        date: '2026-10-03', synced_at: '2026-10-03T22:00:00Z', meetings: [],
    })});
    f.api.showRefreshError('2026-10-03', 'Previous failed attempt');
    await f.context.triggerDaySync('2026-10-03', null);
    assert.equal(f.notices.children.length, 0);
    assert.equal(f.events[0].type, 'sb:meetings-cache-updated');
    assert.deepEqual(f.redraws, [['2026-10-03']]);
    assert.deepEqual(f.requests[0], {operation: 'meetings', options: {date: '2026-10-03'}});
});

test('a pull completed after month navigation still notifies freshness and queues its repaint', async () => {
    const f = dayRefreshFixture({ok: true, json: async () => ({
        date: '2026-10-03', synced_at: '2026-10-03T22:00:00Z', meetings: [],
    })}, 9);
    await f.context.triggerDaySync('2026-10-03', null);
    assert.equal(f.events[0].type, 'sb:meetings-cache-updated');
    assert.equal(f.redraws.length, 0);
    assert.ok(f.context._pendingRedrawDates.has('2026-10-03'));
});

test('work rail follows HVA, task, milestone, plain priority for all flag combinations', () => {
    const {api} = fixture();
    for (const has_hva of [false, true]) for (const has_task of [false, true]) {
        for (const has_milestone of [false, true]) {
            assert.equal(api.workStatus({has_hva, has_task, has_milestone}),
                has_hva ? 'hva' : has_task ? 'task' : has_milestone ? 'milestone' : 'plain');
        }
    }
});

test('full month and day refresh reuse the same note/ghost customer palette', () => {
    const {api} = fixture();
    const data = {days: {1: [{customer_id: 44}, {customer_id: null}, {customer_id: 77}]},
        ghosts: {1: [{customer_id: 44}, {customer_id: 99}, {customer_id: null}]}};
    assert.deepEqual({...api.customerColors(data)}, {44: 1, 77: 2, 99: 3});
    assert.deepEqual({...api.customerColors({})}, {});
    const many = {days: {1: Array.from({length: 16}, (_, i) => ({customer_id: i + 1}))}};
    assert.equal(api.customerColors(many)[16], 1);
    assert.equal((template.match(/SBHomeCalendar\.customerColors\(data\)/g) || []).length, 2);
});

test('note label is literal, primary statuses become rails, engagement stays an independent marker', () => {
    const {api, element} = fixture(), entry = element('button');
    api.note(entry, {customer_name: '<img onerror=bad()>', time: '9:00 AM',
        has_milestone: true, has_task: true, has_hva: true, has_engagement: true}, 2);
    assert.equal(entry.dataset.workStatus, 'hva');
    assert.equal(entry.dataset.customerColor, '2');
    assert.equal(entry.children[1].textContent, '<img onerror=bad()>');
    assert.equal(entry.children[2].className, 'bi bi-diagram-3');
    assert.match(entry.getAttribute('aria-label'), /Milestone linked.*MSX task linked.*HVA task/);
    assert.ok(entry.children.every(e => !/flag-fill|star-fill|check2-square/.test(e.className)));
});

test('general notes keep their marker without inventing a customer color or a time', () => {
    const {api, element} = fixture(), entry = element('button');
    api.note(entry, {customer_name: 'Internal project', is_general: true}, 1);
    assert.equal(entry.dataset.customerColor, undefined);
    assert.equal(entry.children[0].className, 'bi bi-sticky');
    assert.equal(entry.children[1].textContent, 'Internal project');
    assert.equal(entry.dataset.workStatus, 'plain');
    assert.match(entry.title, /General note/);
});

test('invalid customer palette slots fail explicitly', () => {
    const {api, element} = fixture();
    for (const index of [0, 16, 1.5, '3']) {
        assert.throws(() => api.note(element(), {}, index), /Invalid calendar customer color/);
    }
});

test('matched ghosts present a real link and sibling native recurrence control', () => {
    const {api, element} = fixture(), entry = element();
    const opener = api.ghost(entry, {customer_id: 44, customer_name: 'Customer',
        subject: 'Architecture review', is_recurring: true}, 1, '10:00 AM');
    assert.equal(opener.tagName, 'a');
    assert.equal(entry.className, 'sb-home-entry cal-ghost');
    const recur = entry.children.at(-1);
    assert.equal(recur.dataset.role, 'recur');
    assert.equal(recur.tagName, 'button');
    assert.equal(recur.type, 'button');
    assert.equal(opener.children[0].getAttribute('aria-hidden'), 'true');
    assert.match(opener.getAttribute('aria-label'), /Architecture review.*no saved note/);
});

test('unmatched ghost openers are native buttons with literal copy and an accessible unmatched identity', () => {
    const {api, element} = fixture(), entry = element();
    const opener = api.ghost(entry, {subject: '<literal meeting>'}, null, '');
    assert.equal(opener.tagName, 'button');
    assert.equal(opener.type, 'button');
    assert.equal(entry.getAttribute('role'), undefined);
    assert.equal(opener.children[1].textContent, '<literal meeting>');
    assert.match(opener.getAttribute('aria-label'), /Customer not matched/);
});

test('real ghost builder keeps note destinations and dismiss actions on separate native controls', () => {
    const {context} = fixture();
    const opened = [], dismissed = [];
    Object.assign(context, {openGhostMeeting: (...args) => opened.push(args),
        dismissGhost: (...args) => dismissed.push(args)});
    const start = template.indexOf('    function buildGhostsContainer(');
    const end = template.indexOf('    // Surgically refresh', start);
    vm.runInNewContext(template.slice(start, end), context);
    const data = [{id: 7, customer_id: 44, customer_name: 'Customer', subject: 'Review',
        is_recurring: true}, {id: 8, subject: 'Unmatched review'}];
    const rows = context.buildGhostsContainer(data, {44: 2}, '2026-10-01').children;
    const [link, recur, dismiss] = rows[0].children;
    assert.equal(link.href, '/note/new?customer_id=44&date=2026-10-01&from_meeting=7');
    assert.equal(link.tagName, 'a');
    assert.ok(link.children.every(child => child.tagName !== 'button'));
    recur.click();
    dismiss.click();
    rows[1].children[0].click();
    assert.deepEqual(dismissed.map(args => [args[0], args[2]]), [[7, true], [7, false]]);
    assert.deepEqual(opened, [[data[1], '2026-10-01']]);
});

test('shared date spinner disables its native button and announces busy', () => {
    const {element, context, document} = fixture(), wrap = element('button'), cell = element('td');
    let aura = null;
    wrap.closest = () => cell;
    cell.querySelector = () => aura;
    document.querySelector = () => wrap;
    const start = template.indexOf('    function _findCurrentDayWrap(');
    const end = template.indexOf('    const meetingRefresh = ', start);
    vm.runInNewContext(template.slice(start, end), context);
    context._setSpinnerOnDate('2026-10-01', true);
    assert.equal(wrap.disabled, true);
    assert.equal(wrap.getAttribute('aria-busy'), 'true');
    context._setSpinnerOnDate('2026-10-01', false);
    assert.equal(wrap.disabled, false);
    aura = element('button');
    context._setSpinnerOnDate('2026-10-01', false);
    assert.equal(wrap.disabled, false);
});

test('aura dots stay stable in native day actions, gate refresh, and offer independent sync details', () => {
    const {element, context, document} = fixture(), records = new Map();
    for (const date of ['2026-10-01', '2026-10-02']) {
        const cell = element('td'), header = element(), actions = element(), wrap = element('button');
        cell.setAttribute('data-cal-date', date);
        wrap.closest = () => cell;
        actions.className = 'sb-home-day-actions';
        cell.querySelector = selector => selector === '.sb-home-day-header' ? header :
            actions.children.find(child => child.className.includes('cal-aura-dots'));
        header.querySelector = () => actions;
        header.appendChild(actions); cell.appendChild(header);
        records.set(date, {cell, actions, wrap});
    }
    document.querySelectorAll = selector => selector === '.cal-aura-dots' ?
        [...records.values()].flatMap(r => r.actions.children) :
        [...records.values()].map(r => r.wrap);
    document.querySelector = selector => records.get(selector.match(/data-cal-date="([^"]+)"/)[1]).cell;
    let details = 0;
    context.window.SyncStatus = {forceShow: () => { details++; }};
    context.meetingRefresh = {getJob: date => state.get(date).job};
    context._setSpinnerOnDate = (date, on) => { records.get(date).wrap.disabled = on; };
    const start = template.indexOf('    function applyMeetingIndicators(');
    const end = template.indexOf('    function ', start + 20);
    vm.runInNewContext(template.slice(start, end), context);
    const state = new Map([['2026-10-01', {state: 'running', job: {id: 1}}],
        ['2026-10-02', {state: 'queued', job: {id: 1}}]]);
    context.applyMeetingIndicators(state);
    const first = records.get('2026-10-01'), second = records.get('2026-10-02');
    const dots = first.actions.children[0];
    assert.equal(dots.tagName, 'button');
    assert.equal(dots.type, 'button');
    assert.equal(first.wrap.disabled, true);
    assert.equal(second.actions.children[0].classList.contains('cal-aura-queued'), true);
    context.applyMeetingIndicators(state);
    assert.equal(first.actions.children[0], dots);
    dots.click();
    assert.equal(details, 1);
    context.applyMeetingIndicators(new Map([['2026-10-02', {state: 'running'}]]));
    assert.equal(first.actions.children.length, 0);
    assert.equal(first.wrap.disabled, false);
    assert.equal(second.actions.children[0].classList.contains('cal-aura-queued'), false);
    context.applyMeetingIndicators(new Map());
    assert.equal(second.actions.children.length, 0);
    assert.equal(second.wrap.disabled, false);
});

test('Today health targets the new square date cell without relying on Bootstrap badge markup', () => {
    const {element, context, body} = fixture(), today = element('td');
    body.querySelectorAll = selector => {
        assert.equal(selector, '.sb-home-today'); return [today];
    };
    const start = template.indexOf('    function applyTodayStatus(');
    const end = template.indexOf('    // ===== SHARED MEETING REFRESH', start);
    vm.runInNewContext(template.slice(start, end), context);
    context.applyTodayStatus({synced: false});
    assert.equal(today.classList.contains('cal-today-incomplete'), true);
    context.applyTodayStatus({synced: true});
    assert.equal(today.classList.contains('cal-today-incomplete'), false);
});

test('freshness-menu meeting reads feed the existing Today health renderer', () => {
    const {context} = fixture();
    let handler, received;
    context.window.addEventListener = (name, callback) => {
        assert.equal(name, 'sb:meetings-today-status'); handler = callback;
    };
    context.applyTodayStatus = data => { received = data; };
    vm.runInNewContext(template.match(
        /window\.addEventListener\('sb:meetings-today-status',[^\n]+/)[0], context);
    const status = {synced: false, synced_at: null};
    handler({detail: status});
    assert.equal(received, status);
});
test('day refresh inserts and replaces ghosts inside the shared scroll area, preserving saved notes', async () => {
    const {element, context, document} = fixture(), cell = element('td'), entries = element();
    const note = element('button');
    entries.appendChild(note);
    cell.querySelector = selector => selector === '.sb-home-day-entries' ? entries :
        selector === '.cal-ghosts-container' ? entries.children.find(e => e !== note) : null;
    document.querySelector = () => cell;
    let ghosts = [{id: 7, customer_id: 44}];
    let healthChecks = 0;
    Object.assign(context, {calendarYear: 2026, calendarMonth: 10,
        meetingRefresh: {render() {}},
        fetch: async () => ({ok: true, json: async () => ({
            ...monthData(2026, 10), days: {1: [{id: 44, customer_id: 44}]},
            ghosts: {1: ghosts}})}),
        buildGhostsContainer: (rows, colors) => {
            assert.equal(colors[44], 1);
            const container = element(); container.rows = rows; return container;
        },
        requestAnimationFrame: callback => callback(), checkTodayStatus: () => { healthChecks++; }});
    const start = template.indexOf('    function refreshDaysInPlace(');
    const end = template.indexOf('    {% if has_engagements %}', start);
    vm.runInNewContext(template.slice(start, end), context);
    for (const next of [[{id: 7, customer_id: 44}], [{id: 8, customer_id: 44}], []]) {
        ghosts = next;
        context.refreshDaysInPlace(['2026-10-01']);
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(entries.children[0], note);
        assert.equal(entries.children.length, next.length ? 2 : 1);
        if (next.length) assert.deepEqual(entries.children[1].rows, next);
    }
    assert.equal(healthChecks, 3);
});

test('failed day repaint preserves entries and retries the read without another WorkIQ call', async () => {
    const {element, context, document} = fixture(), cell = element('td'), entries = element();
    const ghost = element(), note = element('button');
    entries.appendChild(note); entries.appendChild(ghost);
    document.querySelector = () => cell;
    cell.querySelector = selector => selector === '.sb-home-day-entries' ? entries : ghost;
    let reads = 0;
    Object.assign(context, {calendarYear: 2026, calendarMonth: 10,
        fetch: async () => { reads++; return {ok: false, status: 503}; },
        console: {error() {}}});
    const start = template.indexOf('    function refreshDaysInPlace(');
    const end = template.indexOf('    {% if has_engagements %}', start);
    vm.runInNewContext(template.slice(start, end), context);
    await context.refreshDaysInPlace(['2026-10-01']);
    assert.deepEqual(entries.children, [note, ghost]);
    const body = document.getElementById('homeMeetingRefreshError-2026-10-01').children[1];
    assert.match(body.textContent, /could not redraw.*HTTP 503/);
    assert.doesNotMatch(body.textContent, /saved meetings are unchanged/);
    await body.children[0].click();
    assert.equal(reads, 2);
});

test('customer highlighting spans notes and ghosts, survives mouse departure while focused, and stays scoped', () => {
    const {api, element, document, hover} = fixture();
    const entries = [44, 44, 77, 44].map((id, i) => {
        const e = element(); e.dataset.customerId = String(id); e.inBody = i !== 3;
        api.bindHighlight(e); return e;
    });
    hover(entries[0]);
    entries[0].dispatch('mouseenter');
    assert.deepEqual(entries.map(e => e.classList.contains('cal-highlight')), [true, true, false, false]);
    document.activeElement = entries[0];
    hover(null);
    entries[0].dispatch('mouseleave');
    assert.equal(entries[1].classList.contains('cal-highlight'), true);
    document.activeElement = null;
    entries[0].dispatch('focusout');
    assert.equal(entries[1].classList.contains('cal-highlight'), false);
});

test('calendar rendering retains every day in four-, five-, six-week and leap months', () => {
    for (const [year, month, days, weeks] of [
        [2026, 2, 28, 4], [2026, 10, 31, 5], [2026, 8, 31, 6], [2028, 2, 29, 5],
    ]) {
        const {api, body, context} = fixture();
        let html;
        Object.defineProperty(body, 'innerHTML', {set: value => { html = value; body.children = []; }});
        Object.assign(context, {triggerDaySync() {}, openQuickCreateModal() {},
            openHomeNoteDetail() {}, buildGhostsContainer() {},});
        const start = template.indexOf('    function renderCalendar(data)');
        const end = template.indexOf('    // Build a calendar-cell ghost container', start);
        vm.runInNewContext(template.slice(start, end), context);
        context.renderCalendar({year, month, days_in_month: days, today_day: 3});
        assert.equal(html, '');
        assert.equal(body.children.length, weeks);
        const cells = body.children.flatMap(row => row.children);
        assert.equal(cells.length, weeks * 7);
        assert.equal(cells.filter(cell => cell.getAttribute('data-cal-date')).length, days);
        assert.equal(cells.filter(cell => cell.classList.contains('sb-home-today')).length, 1);
        assert.ok(api);
    }
});

test('calendar styles use opaque theme-aware customer fills, striped ghost rails and inset focus', () => {
    assert.match(css, /oklch\(37% \.075 var\(--sb-home-customer-hue\)\)/);
    assert.match(css, /oklch\(90% \.04 var\(--sb-home-customer-hue\)\)/);
    assert.match(css, /repeating-linear-gradient\(135deg/);
    assert.match(css, /border: 1px dashed var\(--sb-home-event-rail\)/);
    assert.match(css, /outline-offset: -2px/);
    assert.match(css, /prefers-reduced-motion/);
    assert.match(css, /\.sb-home-page \.cal-day-num-wrap\.cal-day-syncing \.cal-day-sync-overlay/);
    assert.doesNotMatch(css, /--bs-|rgba\(|transition: all/);
    assert.doesNotMatch(template, /cal-customer-\d|cal-ghost-customer-\d/);
});
