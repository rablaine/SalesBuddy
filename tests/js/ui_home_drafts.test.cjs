const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

const template = fs.readFileSync('templates/index.html', 'utf8');
const management = template.slice(template.indexOf('// ===== DRAFT MANAGEMENT ====='),
    template.indexOf('// Build a YYYY-MM-DD'));
const storageEvents = template.slice(template.indexOf('// Reload drafts when storage changes'),
    template.indexOf('// ===== CALENDAR VIEW ====='));

function homeDrafts(initial = {}) {
    const storage = new Map(Object.entries(initial));
    const elements = new Map(), documentEvents = new Map(), windowEvents = new Map();
    function element() {
        return {hidden: false, textContent: '', style: {}, children: [],
            append(...children) { this.children.push(...children); },
            replaceChildren(...children) { this.children = children; },
            setAttribute() {}, addEventListener() {}};
    }
    const get = id => {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
    };
    const document = {getElementById: get, createElement: element,
        addEventListener: (name, handler) => documentEvents.set(name, handler)};
    const window = {addEventListener: (name, handler) => windowEvents.set(name, handler)};
    const localStorage = {
        get length() { return storage.size; },
        key: index => [...storage.keys()][index],
        getItem: key => storage.get(key) ?? null,
        removeItem: key => storage.delete(key),
    };
    const code = (management + storageEvents)
        .replaceAll('{{ url_for("notes.note_create") }}', '/note/new')
        .replaceAll('{{ url_for("notes.note_edit", id=0) }}', '/note/0/edit');
    vm.runInNewContext(code, {document, window, localStorage, console, URLSearchParams,
        confirm: () => false});
    documentEvents.get('DOMContentLoaded')();
    return {get, storage,
        emit: (name, event = {}) => windowEvents.get(name)?.(event),
        links: () => get('draftsList').children.map(row => row.children[0].href)};
}

test('Home counts local drafts initially and hides the breadcrumb button when none remain', () => {
    const h = homeDrafts({'salesbuddy_draft_107': JSON.stringify({
        customer_name: 'Customer', timestamp: Date.now(),
    })});
    assert.equal(h.get('draftCount').textContent, 1);
    assert.equal(h.get('homeDraftsButton').hidden, false);
    assert.deepEqual(h.links(), ['/note/new?draft=salesbuddy_draft_107&customer_id=107']);
    h.storage.clear();
    h.emit('storage', {key: 'salesbuddy_draft_107'});
    assert.equal(h.get('homeDraftsButton').hidden, true);
});

test('returning to cached Home discovers drafts created after its initial load', () => {
    const h = homeDrafts();
    assert.equal(h.get('homeDraftsButton').hidden, true);
    h.storage.set('salesbuddy_draft_107', JSON.stringify({customer_name: 'Customer'}));
    h.emit('pageshow', {persisted: true});
    assert.equal(h.get('homeDraftsButton').hidden, false);
    assert.equal(h.get('draftCount').textContent, 1);
});

test('Back restoration and a storage clear reconcile the indicator without reloading Home', () => {
    const h = homeDrafts();
    h.storage.set('salesbuddy_draft_general', JSON.stringify({customer_name: ''}));
    h.emit('pageshow', {persisted: true});
    assert.equal(h.get('draftCount').textContent, 1);
    h.storage.clear();
    h.emit('storage', {key: null});
    assert.equal(h.get('homeDraftsButton').hidden, true);
});

test('draft links resume edited notes, legacy edits and general notes at the correct forms', () => {
    const h = homeDrafts({
        'salesbuddy_draft_note_12': JSON.stringify({note_id: 12, customer_name: 'Customer'}),
        'salesbuddy_draft_107': JSON.stringify({note_id: 13, customer_name: 'Customer'}),
        'salesbuddy_draft_general': JSON.stringify({customer_name: ''}),
    });
    assert.deepEqual(h.links(), ['/note/12/edit?draft=salesbuddy_draft_note_12',
        '/note/13/edit?draft=salesbuddy_draft_107', '/note/new?draft=salesbuddy_draft_general']);
});

test('two creation drafts for the same customer resume their own identity and customer', () => {
    const h = homeDrafts({
        'salesbuddy_draft_new_first': JSON.stringify({customer_id: '107', content: 'First'}),
        'salesbuddy_draft_new_second': JSON.stringify({customer_id: '107', content: 'Second'}),
    });
    assert.deepEqual(h.links(), [
        '/note/new?draft=salesbuddy_draft_new_first&customer_id=107',
        '/note/new?draft=salesbuddy_draft_new_second&customer_id=107',
    ]);
});
