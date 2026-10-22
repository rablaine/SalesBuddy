const assert = require('node:assert/strict');
const test = require('node:test');
const {fingerprint, createStore, PREFIX} = require('../../static/js/ui-note-drafts.js');

function fixture() {
    const records = new Map();
    let next = 0;
    const storage = {getItem: key => records.get(key) ?? null,
        setItem: (key, value) => records.set(key, value),
        removeItem: key => records.delete(key)};
    const store = key => createStore({storage, key, uuid: () => String(++next)});
    return {records, storage, store};
}

test('meaningful changes exclude timestamps, display metadata and unchanged engagement cards', () => {
    const original = {content: '<p>Note</p>', topics: [{id: 1, name: 'Topic'}],
        inlineEngagements: [{id: 7, status: 'Active', dirty: false}]};
    assert.equal(fingerprint(original), fingerprint({...original, timestamp: 12,
        customer_name: 'New display name', topics: [{id: '1', name: 'Renamed topic'}],
        inlineEngagements: [{id: 7, status: 'Changed remotely', dirty: false}]}));
    assert.notEqual(fingerprint(original), fingerprint({...original,
        inlineEngagements: [{id: 7, status: 'Changed locally', dirty: true}]}));
    assert.notEqual(fingerprint(original), fingerprint({...original, content: ''}));
    assert.notEqual(fingerprint(original), fingerprint({...original, attendees: [{type: 'se', id: 1}]}));
    assert.notEqual(fingerprint(original), fingerprint({...original, opportunities: [{id: 'opp'}]}));
    assert.notEqual(fingerprint(original), fingerprint({...original, task: {due_date: '2026-11-01'}}));
});

test('editor HTML normalization is supplied by the actual rich text editor', () => {
    const normalized = html => html.replaceAll('<p><br></p>', '');
    assert.equal(fingerprint({content: 'Note'}, normalized),
        fingerprint({content: 'Note<p><br></p>'}, normalized));
});

test('separate notes and competing windows retain both recovery copies', () => {
    const h = fixture();
    const first = h.store(PREFIX + 'note_1');
    const second = h.store(PREFIX + 'note_1');
    first.write({content: 'First window'});
    second.write({content: 'Second window'});
    assert.notEqual(first.key, second.key);
    assert.equal(first.read().content, 'First window');
    assert.equal(second.read().content, 'Second window');
    assert.equal(h.records.size, 2);
});

test('an unresumed draft is never silently replaced', () => {
    const h = fixture();
    const previous = h.store(PREFIX + 'note_1');
    previous.write({content: 'Earlier draft'});
    const fresh = h.store(previous.key);
    fresh.write({content: 'New work without resuming'});
    assert.notEqual(previous.key, fresh.key);
    assert.equal(previous.read().content, 'Earlier draft');
});

test('save cleanup cannot delete a newer revision or another window recovery', () => {
    const h = fixture(), first = h.store(PREFIX + 'note_1');
    const submitted = first.write({content: 'Submitted'});
    first.write({content: 'Typed during save'});
    assert.equal(first.remove(submitted.revision), false);
    assert.equal(first.read().content, 'Typed during save');
    const other = h.store(first.key);
    other.claim();
    other.write({content: 'Other window'});
    assert.equal(first.remove(), true);
    assert.equal(other.read().content, 'Other window');
});

test('two windows resuming one recovery copy write to independent keys', () => {
    const h = fixture(), original = h.store(PREFIX + 'note_1');
    original.write({content: 'Original recovery'});
    const first = h.store(original.key), second = h.store(original.key);
    first.claim();
    second.claim();
    first.write({content: 'First resumed window'});
    second.write({content: 'Second resumed window'});
    assert.notEqual(first.key, second.key);
    assert.equal(first.read().content, 'First resumed window');
    assert.equal(second.read().content, 'Second resumed window');
});

test('resuming claims an existing draft while quota failures preserve old copies', () => {
    const h = fixture(), first = h.store(PREFIX + 'note_1');
    first.write({content: 'Recover me'});
    const resumed = h.store(first.key);
    resumed.claim();
    h.storage.setItem = () => { throw new Error('Storage full'); };
    assert.throws(() => resumed.write({content: 'Updated'}), /Storage full/);
    assert.equal(first.read().content, 'Recover me');
});

test('retry identity survives reload and changes only with the submitted payload', () => {
    const h = fixture(), first = h.store(PREFIX + 'new_1');
    const token = first.token('same form');
    assert.equal(first.token('same form'), token);
    const resumed = h.store(first.key);
    resumed.restoreToken({saveToken: token, saveSignature: 'same form'});
    assert.equal(resumed.token('same form'), token);
    assert.notEqual(resumed.token('changed form'), token);
});

test('creation identity remains stable even when work changes after a lost response', () => {
    const h = fixture(), first = h.store(PREFIX + 'new_1');
    const token = first.token('first revision', true);
    assert.equal(first.token('later revision', true), token);
    const resumed = h.store(first.key);
    resumed.restoreToken({saveToken: token, saveSignature: 'later revision'});
    assert.equal(resumed.token('resumed revision', true), token);
});
