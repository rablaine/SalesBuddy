const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/js/ui-note-form-native.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../../templates/note_form.html'), 'utf8')
    .replace(/\r\n/g, '\n');

function fixture({submit = null, modal = false} = {}) {
    const handlers = {};
    class Dialog {
        constructor() {
            this.open = false;
            this.listeners = {};
            this.classes = new Set();
            this.classList = {add: value => this.classes.add(value),
                remove: value => this.classes.delete(value)};
        }
        addEventListener(type, handler) { this.listeners[type] = handler; }
    }
    const notice = {hidden: true, dataset: {}, setAttribute(key, value) { this[key] = value; }};
    const calls = [];
    const window = {};
    vm.runInNewContext(source, {window, queueMicrotask,
        HTMLDialogElement: Dialog,
        document: {addEventListener: (type, handler) => { handlers[type] = handler; },
            getElementById: () => notice,
            querySelector: selector => selector === '#noteForm button[type="submit"]' ?
                submit : modal,
            querySelectorAll: () => []},
        SBDialog: {open: dialog => { dialog.open = true; dialog.listeners['sb:dialog-opening'](); calls.push('open'); },
            close: dialog => { dialog.open = false; dialog.listeners['sb:dialog-closed'](); calls.push('close'); }},
    });
    return {api: window.SBNoteForm, Dialog, notice, handlers, calls};
}

test('rich note save binding clicks the existing button and suppresses Quill newline handling', () => {
    let clicks = 0;
    const submit = {disabled: false, click() { clicks++; }};
    const {api} = fixture({submit});
    const binding = template.match(/saveNote:\s*(\{[\s\S]*?\n\s{16}\}),/)[1];
    const config = vm.runInNewContext(`(${binding})`, {SBNoteForm: api});
    assert.equal(config.key, 13);
    assert.equal(config.shortKey, true);
    assert.equal(config.handler(), false);
    assert.equal(clicks, 1);
    submit.disabled = true;
    assert.equal(config.handler(), false);
    assert.equal(clicks, 1);
});

test('rich note shortcut cannot save underneath an open modal or without a save control', () => {
    let clicks = 0;
    fixture({submit: {click() { clicks++; }}, modal: true}).api.save();
    fixture().api.save();
    assert.equal(clicks, 0);
});

test('dialog handles reuse SBDialog and preserve queued focus-listener registration', async () => {
    const {api, Dialog, calls} = fixture();
    const element = new Dialog();
    const handle = api.dialog(element);
    assert.equal(api.dialog(element), handle);
    handle.show();
    assert.deepEqual(calls, []);
    await Promise.resolve();
    assert.equal(element.open, true);
    assert.equal(element.classes.has('show'), true);
    handle.toggle();
    assert.equal(element.open, false);
    assert.equal(element.classes.has('show'), false);
    handle.toggle();
    await Promise.resolve();
    handle.hide();
    assert.deepEqual(calls, ['open', 'close', 'open', 'close']);
    assert.throws(() => api.dialog({}), /Invalid note dialog/);
});

test('pending and failed-save feedback is explicit and does not discard the draft', () => {
    const {api, notice} = fixture();
    api.report('Saving note...');
    assert.equal(notice.role, 'status');
    assert.equal(notice.hidden, false);
    api.report('Your draft has been preserved.', 'error');
    assert.equal(notice.role, 'alert');
    assert.equal(notice.dataset.sbState, 'error');
    assert.match(notice.textContent, /preserved/);
    assert.match(template, /if \(submitButton\.disabled\) return;/);
    const failure = template.slice(template.indexOf("console.error('Failed to save engagement:'"),
        template.indexOf('// --- Template selector logic ---'));
    assert.match(failure, /SBNoteForm\.report/);
    assert.doesNotMatch(failure, /requestSubmit/);
});

test('engagement saves reject an unsuccessful result instead of marking it clean', async () => {
    const start = template.indexOf('function _saveOneEngagement(');
    const end = template.indexOf('\nfunction saveAllInlineEngagements()', start);
    const save = template.slice(start, end);
    const values = {textContent: 'Original title', value: 'keep this draft'};
    const card = {querySelector: () => values};
    const context = {_inlineEngCleanStates: {}, _getCardEngState: () => 'clean',
        fetch: async () => ({json: async () => ({success: false, error: 'Save failed'})})};
    vm.runInNewContext(save, context);
    await assert.rejects(context._saveOneEngagement(card, 9), /Save failed/);
    assert.deepEqual(context._inlineEngCleanStates, {});
    context.fetch = async () => ({json: async () => ({success: true})});
    await context._saveOneEngagement(card, 9);
    assert.equal(context._inlineEngCleanStates[9], 'clean');
});

function draftInitialization(noteId, initial = {}, url = 'http://localhost/note/new') {
    const start = template.indexOf('function getDraftKey()');
    const end = template.indexOf('// Get customer name', start);
    const records = new Map(Object.entries(initial)), elements = new Map(), notices = [];
    let restores = 0, saves = 0;
    const context = {CURRENT_NOTE_ID: noteId, DRAFT_KEY_PREFIX: 'salesbuddy_draft_',
        draftStore: null, draftBaseline: null, noteVersion: null, recoveryPending: false,
        draftNeedsSave: false, draftRestored: false, initializingDraft: false,
        restoreDraft: () => { restores++; return true; },
        saveDraft: () => { saves++; return true; },
        crypto: {randomUUID: () => 'unique-creation-id'}, URL,
        SBNoteDrafts: require('../../static/js/ui-note-drafts.js'),
        collectDraftData: () => ({content: 'Original'}), quill: {clipboard: {convert: html => ({ops: html})}},
        localStorage: {getItem: key => records.get(key) ?? null,
            setItem: (key, value) => records.set(key, value), removeItem: key => records.delete(key)},
        window: {location: {href: url}, history: {replaceState() {}}},
        SBNoteForm: {report: (...args) => notices.push(args)}, console: {error() {}},
        document: {getElementById: id => {
            if (!elements.has(id)) elements.set(id, {value: '107'});
            return elements.get(id);
        }}};
    vm.runInNewContext(template.slice(start, end), context);
    context.initializeNoteDraft();
    return {context, records, elements, notices, restores: () => restores, saves: () => saves};
}

for (const noteId of [null, 285]) {
    test(`an explicit ${noteId ? 'edit' : 'new-note'} draft link opens the draft directly`, () => {
        const key = `salesbuddy_draft_${noteId ? 'note_285' : 'new_requested'}`;
        const h = draftInitialization(noteId, {
            [key]: JSON.stringify({note_id: noteId, content: 'My draft'}),
        }, `http://localhost/note/${noteId ? '285/edit' : 'new'}?draft=${key}`);
        assert.equal(h.restores(), 1);
        assert.equal(h.saves(), 1);
        assert.equal(h.context.draftRestored, true);
        assert.equal(h.context.recoveryPending, false);
        assert.equal(h.elements.get('noteDraftRecovery').hidden, true);
    });
}

test('opening a saved note normally still offers rather than applies unfinished edits', () => {
    const h = draftInitialization(285, {
        salesbuddy_draft_note_285: '{"note_id":285,"content":"Unfinished edits"}',
    }, 'http://localhost/note/285/edit');
    assert.equal(h.restores(), 0);
    assert.equal(h.context.recoveryPending, true);
    assert.equal(h.elements.get('noteDraftRecovery').hidden, false);
});

test('a directly opened stale draft retains the saved-note conflict warning', () => {
    const h = draftInitialization(285, {
        salesbuddy_draft_note_285: '{"note_id":285,"base_version":"older","content":"Old edits"}',
    }, 'http://localhost/note/285/edit?draft=salesbuddy_draft_note_285');
    assert.equal(h.restores(), 1);
    assert.match(h.notices.at(-1)[0], /saved note has changed/);
});

test('a draft link cannot apply edits belonging to a different saved note', () => {
    const raw = '{"note_id":999,"content":"Other note"}';
    const h = draftInitialization(285, {salesbuddy_draft_note_999: raw},
        'http://localhost/note/285/edit?draft=salesbuddy_draft_note_999');
    assert.equal(h.restores(), 0);
    assert.equal(h.records.get('salesbuddy_draft_note_999'), raw);
    assert.match(h.notices.at(-1)[0], /different note/);
});

test('the default template cannot replace an intentionally empty resumed draft', () => {
    assert.match(template, /if \(!draftRestored && text\.length === 0\)/);
});

test('editing and creating use independent identities, not one draft per customer', () => {
    assert.equal(draftInitialization(285).context.getDraftKey(), 'salesbuddy_draft_note_285');
    assert.equal(draftInitialization(null).context.getDraftKey(),
        'salesbuddy_draft_new_unique-creation-id');
});

test('legacy edit recovery migrates only the same saved note, never a creation draft', () => {
    const fresh = draftInitialization(285, {
        salesbuddy_draft_107: '{"note_id":null,"content":"protected"}',
    });
    assert.equal(fresh.context.recoveryPending, false);
    assert.equal(fresh.records.get('salesbuddy_draft_107'), '{"note_id":null,"content":"protected"}');
    const edit = draftInitialization(285, {
        salesbuddy_draft_107: '{"note_id":285,"content":"legacy edit"}',
    });
    assert.equal(edit.context.recoveryPending, true);
    assert.equal(edit.records.has('salesbuddy_draft_107'), false);
    assert.equal(edit.records.get('salesbuddy_draft_note_285'),
        '{"note_id":285,"content":"legacy edit"}');
});

test('read-only editor navigation never creates or replaces a draft', () => {
    const start = template.indexOf("window.addEventListener('beforeunload', function(e)");
    const end = template.indexOf("\ndocument.addEventListener('visibilitychange'", start);
    let unload, saves = 0;
    const context = {CURRENT_NOTE_ID: 285, draftNeedsSave: false,
        formSubmittedSuccessfully: false, draftDiscarded: false,
        noteSavePending: false, noteFlyoutBlocker: () => '',
        document: {getElementById: () => ({value: '107'})},
        saveDraft: () => { saves++; return true; },
        window: {addEventListener: (type, handler) => { unload = handler; }}};
    vm.runInNewContext(template.slice(start, end), context);
    unload({});
    assert.equal(saves, 0);
    context.draftNeedsSave = true;
    unload({});
    assert.equal(saves, 1);
    context.CURRENT_NOTE_ID = null;
    context.draftNeedsSave = false;
    unload({});
    assert.equal(saves, 1);
    context.document.getElementById = () => null;
    unload({});
    assert.equal(saves, 1);
    context.draftNeedsSave = true;
    unload({});
    assert.equal(saves, 2);
    context.draftDiscarded = true;
    unload({});
    assert.equal(saves, 2);
    context.draftDiscarded = false;
    context.formSubmittedSuccessfully = true;
    unload({});
    assert.equal(saves, 2);
});

test('context changes select native paired/single layout without legacy shell widths', () => {
    const start = template.indexOf('function _updateColumnLayout()');
    const end = template.indexOf('// Related Notes (dynamically loaded)', start);
    const classes = new Set(['sb-directory-frame']);
    const row = {dataset: {}, classList: {toggle: (name, enabled) =>
        enabled ? classes.add(name) : classes.delete(name)}}, left = {};
    let cards = [], notes = {childElementCount: 0};
    const right = {style: {}, querySelectorAll: () => cards, querySelector: () => notes};
    const context = {document: {getElementById: id => ({
        noteFormRow: row, noteFormLeftCol: left, noteFormRightCol: right,
    })[id]}};
    const layout = template.slice(start, end);
    vm.runInNewContext(layout, context);
    context._updateColumnLayout();
    assert.equal(row.dataset.sbNotePaired, 'false');
    assert.equal(right.style.display, 'none');
    assert.equal(classes.has('sb-directory-frame'), true);
    cards = [{}];
    context._updateColumnLayout();
    assert.equal(row.dataset.sbNotePaired, 'true');
    assert.equal(right.style.display, '');
    assert.equal(classes.has('sb-directory-frame'), false);
    cards = [];
    notes.childElementCount = 2;
    context._updateColumnLayout();
    assert.equal(row.dataset.sbNotePaired, 'true');
    assert.equal(classes.has('sb-directory-frame'), false);
    notes.childElementCount = 0;
    context._updateColumnLayout();
    assert.equal(row.dataset.sbNotePaired, 'false');
    assert.equal(classes.has('sb-directory-frame'), true);
    assert.doesNotMatch(layout, /1900|main\.container|maxWidth|paddingLeft|col-md/);
});

test('native drawer stack uses dialog open state and retains earlier context panes', () => {
    const start = template.indexOf("(function() {\n    const ids = ['partnerFlyout'");
    const end = template.indexOf('})();', start) + 5;
    assert.ok(start >= 0 && end > start, 'the actual drawer-stack controller is selected');
    const ids = ['partnerFlyout', 'engagementFlyout', 'contactsFlyout',
        'customerInfoFlyout', 'projectFlyout'];
    const documentListeners = {};
    const drawers = Object.fromEntries(ids.map(id => {
        const classes = new Set(), listeners = {};
        return [id, {open: false, classes, listeners,
            classList: {remove: (...values) => values.forEach(x => classes.delete(x)),
                add: value => classes.add(value)},
            addEventListener: (type, handler) => {
                (listeners[type] ??= []).push(handler);
            }}];
    }));
    vm.runInNewContext(template.slice(start, end), {
        document: {getElementById: id => drawers[id],
            addEventListener: (type, handler) => {
                (documentListeners[type] ??= []).push(handler);
            }},
    });
    function dispatch(id, type) {
        const event = {type, target: drawers[id], bubbles: true};
        (drawers[id].listeners[type] ?? []).forEach(handler => handler(event));
        (documentListeners[type] ?? []).forEach(handler => handler(event));
    }
    function open(id) {
        dispatch(id, 'sb:dialog-opening');
        drawers[id].open = true;
        dispatch(id, 'sb:dialog-opened');
    }
    open('partnerFlyout');
    open('projectFlyout');
    open('engagementFlyout');
    assert.equal(drawers.partnerFlyout.classes.has('sb-note-drawer-shift-2'), true);
    assert.equal(drawers.projectFlyout.classes.has('sb-note-drawer-shift-1'), true);
    assert.equal(drawers.engagementFlyout.classes.size, 0);
    drawers.engagementFlyout.open = false;
    dispatch('engagementFlyout', 'sb:dialog-closed');
    assert.equal(drawers.partnerFlyout.classes.has('sb-note-drawer-shift-1'), true);
    assert.equal(drawers.projectFlyout.classes.size, 0);
    const css = fs.readFileSync(path.join(__dirname, '../../static/css/ui-note-form.css'), 'utf8');
    assert.match(css, /\.sb-nf-drawer\s*\{[^}]*position:fixed;z-index:1045;/);
    assert.match(css, /body\.copilot-chat-open \.sb-nf-drawer\[open\].*--sb-chat-context-inset,0px/);
    assert.doesNotMatch(template.slice(start, end), /offcanvas|flyout-shifted/);
});

function saveLifecycle() {
    const start = template.indexOf("document.getElementById('noteForm').addEventListener('submit', async");
    const end = template.indexOf('// Discard draft function', start);
    let submit, respond, clears = 0, requests = 0, state = 'submitted';
    const button = {disabled: false, innerHTML: ''};
    const form = {action: '/note/new', querySelector: () => button};
    const notices = [];
    const context = {
        document: {getElementById: id => id === 'noteForm' ?
            {addEventListener: (type, handler) => { submit = handler; }} : null},
        selectedMilestones: [], taskCategories: [], formSubmittedSuccessfully: false,
        CURRENT_NOTE_ID: null,
        autoSaveTimeout: null, noteSavePending: false, recoveryPending: false, taskOutcome: '',
        noteVersion: null, activeSaveSignature: null, activeSaveToken: null, URL,
        draftStore: {token: () => 'retry-token', read: () => ({revision: 'submitted'}),
            write: value => { context.laterDraft = value; }},
        collectDraftData: () => ({content: state}),
        draftFingerprint: value => value.content,
        getDraftKey: () => 'salesbuddy_draft_new_1',
        saveDraft: () => true,
        SBNoteForm: {report: (...args) => notices.push(args)},
        FormData: class {
            constructor() { this.values = new Map(); }
            set(key, value) { this.values.set(key, value); }
            entries() { return this.values.entries(); }
        },
        fetch: () => { requests++; return new Promise(resolve => { respond = resolve; }); },
        clearDraft: () => { clears++; },
        window: {location: {href: '', origin: 'http://localhost'}},
        console: {log() {}, error() {}, warn() {}}, clearTimeout() {},
    };
    vm.runInNewContext(template.slice(start, end), context);
    const event = {target: form, preventDefault() {}};
    return {context, button, notices, event, submit: event => submit(event),
        respond: value => respond(value), requests: () => requests, clears: () => clears,
        changeState: value => { state = value; }};
}

test('a pending save blocks duplicates and failed acknowledgements keep draft recovery', async () => {
    const h = saveLifecycle();
    const {submit, event, button, context, notices} = h;
    await submit({...event, defaultPrevented: true});
    assert.equal(h.requests(), 0);
    assert.equal(button.disabled, false);
    const pending = submit(event);
    assert.equal(button.disabled, true);
    await submit(event);
    assert.equal(h.requests(), 1);
    h.respond({ok: false, json: async () => ({success: false, error: 'Save failed'})});
    await pending;
    assert.equal(h.clears(), 0);
    assert.equal(context.formSubmittedSuccessfully, false);
    assert.equal(button.disabled, false);
    assert.match(notices.at(-1)[0], /draft has been preserved/);
    const retry = submit(event);
    h.respond({ok: true, json: async () => ({
        success: true, note_id: 9, version: 'new-version', redirect_url: '/note/9',
    })});
    await retry;
    assert.equal(h.clears(), 1);
    assert.equal(context.formSubmittedSuccessfully, true);
    assert.equal(context.window.location.href, 'http://localhost/note/9');
});

test('HTTP 200 without an explicit saved-note acknowledgement never clears the draft', async () => {
    const h = saveLifecycle();
    const pending = h.submit(h.event);
    h.respond({ok: true, json: async () => ({})});
    await pending;
    assert.equal(h.clears(), 0);
    assert.equal(h.context.formSubmittedSuccessfully, false);
});

test('typing during a save is preserved as unfinished edits to the acknowledged note', async () => {
    const h = saveLifecycle();
    const pending = h.submit(h.event);
    h.changeState('later keystrokes');
    h.respond({ok: true, json: async () => ({
        success: true, note_id: 9, version: 'new-version', redirect_url: '/note/9',
    })});
    await pending;
    assert.equal(h.clears(), 0);
    assert.equal(h.context.laterDraft.content, 'later keystrokes');
    assert.equal(h.context.laterDraft.note_id, 9);
    assert.match(h.context.window.location.href, /^\/note\/9\/edit\?draft=/);
});

test('a changed creation retry becomes recoverable edits instead of creating another note', async () => {
    const h = saveLifecycle();
    const pending = h.submit(h.event);
    h.respond({ok: false, json: async () => ({
        success: false, existing_note_id: 9, version: 'saved-version',
        error: 'An earlier save created this note.',
    })});
    await pending;
    assert.equal(h.clears(), 0);
    assert.equal(h.context.laterDraft.note_id, 9);
    assert.equal(h.context.laterDraft.base_version, 'saved-version');
    assert.equal(h.context.laterDraft.content, 'submitted');
    assert.match(h.context.window.location.href, /^\/note\/9\/edit\?draft=/);
});

test('snapshotting cannot apply pending meeting imports or open a meeting picker', () => {
    const start = template.indexOf('function collectDraftData()');
    const end = template.indexOf('function saveDraft()', start);
    assert.doesNotMatch(template.slice(start, end),
        /applyMeetingImport|showMeetingImportModalReady|hideMeetingStatusCard/);
});

for (const restored of [false, true]) {
    test(`delayed engagement data preserves ${restored ? 'resumed' : 'newly typed'} inline edits`, async () => {
        const start = template.indexOf('function _loadEngCard(card, engId)');
        const end = template.indexOf('function _updateColumnLayout()', start);
        let respond;
        const card = {state: 'Initial', dataset: {}, cloneNode() { return {state: this.state}; }};
        const context = {
            fetch: () => new Promise(resolve => { respond = resolve; }),
            _getCardEngState: card => card.state,
            _populateEngCard: (card, data) => { card.state = data.state; },
            _inlineEngCleanStates: {}, _wireInlineCardContacts() {}, _loadEngCardContacts() {},
            console,
        };
        vm.runInNewContext(template.slice(start, end), context);
        if (restored) {
            card.state = 'Recovered edits';
            card.dataset.noteDraftRestored = 'true';
        }
        context._loadEngCard(card, 7);
        assert.equal(context._inlineEngCleanStates[7], card.state);
        if (!restored) card.state = 'Typed while loading';
        respond({json: async () => ({state: 'Saved engagement'})});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(card.state, restored ? 'Recovered edits' : 'Typed while loading');
        assert.equal(context._inlineEngCleanStates[7], 'Saved engagement');
    });
}

for (const scenario of [
    {name: 'Close flushes before the debounce timer', protected: true, calls: 1, blocked: false},
    {name: 'storage failure keeps the editor open on Stay', protected: false, calls: 1, blocked: true},
    {name: 'dirty flyouts block parent navigation', blocker: 'engagement', calls: 0, blocked: true},
    {name: 'a pending note save blocks parent navigation', pending: true, calls: 0, blocked: true},
    {name: 'inline JavaScript links are not navigation', href: 'javascript:void(0)', calls: 0, blocked: false},
]) {
    test(scenario.name, async () => {
        const start = template.indexOf("document.addEventListener('click', async event =>");
        const end = template.indexOf('// Clear draft only after', start);
        let handler, saves = 0, prevented = false;
        const context = {
            document: {addEventListener: (type, fn) => { handler = fn; }},
            window: {location: {href: 'http://localhost/note/1/edit'}},
            location: {pathname: '/note/1/edit', search: ''}, URL,
            noteSavePending: Boolean(scenario.pending), formSubmittedSuccessfully: false,
            draftDiscarded: false, draftNeedsSave: true,
            noteFlyoutBlocker: () => scenario.blocker || '',
            saveDraft: () => { saves++; return scenario.protected; },
            askNoteDecision: async () => false, SBNoteForm: {report() {}},
        };
        vm.runInNewContext(template.slice(start, end), context);
        const link = {href: scenario.href || 'http://localhost/note/1',
            hasAttribute: name => name === 'data-note-close'};
        await handler({target: {closest: () => link}, button: 0,
            preventDefault: () => { prevented = true; }, stopImmediatePropagation() {}});
        assert.equal(saves, scenario.calls);
        assert.equal(prevented, scenario.blocked);
        assert.equal(context.window.location.href, 'http://localhost/note/1/edit');
    });
}
