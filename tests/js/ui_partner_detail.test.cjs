const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const api = require('../../static/js/ui-partner-detail.js');
const source = fs.readFileSync(require.resolve('../../static/js/ui-partner-detail.js'), 'utf8');

function harness({website = '', failure = null, response, saved} = {}) {
    const nodes = new Map(), events = {}, requests = [], destinations = [], errors = [];
    const previews = [], previewSelections = [], noteMounts = [];
    function node(tag = 'div', props = {}) {
        const n = {tag, events: {}, children: [], dataset: {}, attrs: {}, hidden: false,
            disabled: false, checked: false, open: false, value: '', textContent: '',
            scrollTop: 0, scrollLeft: 0, className: '',
            addEventListener(type, fn) { (this.events[type] ??= []).push(fn); },
            dispatchEvent(e) { e.target ??= this; (this.events[e.type] ??= []).forEach(fn => fn(e)); },
            setAttribute(k, v) { this.attrs[k] = v; },
            getAttribute(k) { return k === 'action' ? this.action : this.attrs[k]; },
            hasAttribute(k) { return k in this.attrs; },
            append(...items) { this.children.push(...items); items.forEach(c => { c.parent = this; }); },
            replaceChildren() { this.children = []; },
            focus() { this.focused = true; },
            checkValidity() { return true; },
            matches(selector) {
                const attr = selector.match(/^\[data-(.+)\]$/)?.[1];
                if (attr) return attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase()) in this.dataset;
                return selector.split(',').some(part => part.trim() === this.tag
                    || part.trim().startsWith('.') && this.className.split(' ').includes(part.trim().slice(1)));
            },
            closest(selector) {
                if (selector === 'a[href]' && this.tag === 'a') return this;
                if (selector === 'dialog' && this.tag === 'dialog') return this;
                const attr = selector.match(/^\[data-(.+)\]$/)?.[1];
                const key = attr?.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                if (key && key in this.dataset) return this;
                if (this.matches(selector)) return this;
                return this.parent?.closest(selector) || null;
            },
            querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
            querySelectorAll(selector) {
                return this.children.flatMap(c => {
                    const match = selector.startsWith('.scrape-')
                        ? c.className === selector.split(':')[0].slice(1) && c.checked && !c.disabled
                        : selector === '[data-partner-write]' ? c.dataset.partnerWrite
                        : c.matches(selector);
                    return [...(match ? [c] : []), ...c.querySelectorAll(selector)];
                });
            }, ...props};
        Object.defineProperty(n, 'id', {get() { return this._id; },
            set(value) { this._id = value; nodes.set(value, this); }});
        return n;
    }
    const ids = ['contact_name', 'contact_title', 'contact_email', 'is_primary',
        'contactFailure', 'partnerNotice', 'partnerDetailState', 'contactModalTitle',
        'contactSubmitBtn', 'partnerDiscardConfirm', 'deleteContactText', 'contactPhotoFull',
        'contactPhotoPaste', 'contactPhotoRemoveBtn', 'contactPhotoStatus', 'scrapeReviewBody',
        'scrapeReviewError', 'scrapeDomainInput', 'scrapeDomainHint', 'scrapeDomainError',
        'scrapeDomainConfirmBtn', 'scrapeRetryBtn', 'scrapeApplyBtn', 'addContactBtn', 'scrapeBtn'];
    ids.forEach(id => {
        const tag = ['contact_name', 'contact_title', 'contact_email', 'is_primary', 'scrapeDomainInput']
            .includes(id) ? 'input' : /Btn$|Confirm$/.test(id) ? 'button' : 'div';
        nodes.set(id, node(tag));
    });
    ['addContactModal', 'deletePartnerDialog', 'deleteContactDialog', 'contactPhotoModal',
        'scrapeDomainModal', 'scrapeReviewModal', 'partnerDiscardDialog']
        .forEach(id => nodes.set(id, node('dialog')));
    const contactForm = node('form', {action: '/partners/6/contacts/new',
        dataset: {partnerWrite: true}});
    ['contact_name', 'contact_title', 'contact_email', 'is_primary', 'contactSubmitBtn']
        .forEach(id => contactForm.append(nodes.get(id)));
    nodes.set('contactForm', contactForm);
    nodes.get('addContactModal').append(contactForm);
    const deleteForm = node('form', {dataset: {partnerWrite: true}});
    nodes.get('deleteContactDialog').append(deleteForm);
    nodes.set('scrapeDomainForm', node('form'));
    const root = node('section');
    [...nodes.values()].filter(n => !n.parent).forEach(n => root.append(n));
    const panes = [node(), node(), node()];
    const photo = node('button', {className: 'contact-photo-wrap', dataset: {contactId: '1'}});
    root.append(photo);
    const share = node('button', {dataset: {partnerShare: true}});
    root.append(share);
    const originalQuery = root.querySelector.bind(root);
    root.querySelector = s => ['.part-overview', '.part-contacts', '.part-notes'].includes(s)
        ? panes[['.part-overview', '.part-contacts', '.part-notes'].indexOf(s)] : originalQuery(s);
    const partner = {id: 6, name: 'Partner', website, overview: 'Original', rating: 0,
        specialty_ids: [4], contacts: [
            {id: 1, name: "O'Brien <tag>", title: 'Role', email: 'p@example.test', is_primary: true}],
        failure};
    nodes.set('partnerDetailData', node('script', {textContent: JSON.stringify(partner)}));
    const rootWindow = {document: {readyState: 'complete',
        querySelector: () => root, getElementById: id => nodes.get(id),
        createElement: tag => node(tag),
        addEventListener(type, fn) { (events['doc:' + type] ??= []).push(fn); }},
        addEventListener(type, fn) { (events[type] ??= []).push(fn); },
        SBTableSort: {mount() {}},
        SBRecordDialog: {create(id, options) {
            previews.push({id, options});
            return {open(selection) { previewSelections.push(selection); }};
        }},
        SBNoteActions: {mount(content) { noteMounts.push(content); }},
        SBDialog: {open(value) { (typeof value === 'string' ? nodes.get(value) : value).open = true; },
            close(value, reason = 'button') {
                const dialog = typeof value === 'string' ? nodes.get(value) : value;
                const e = fire(dialog, 'sb:dialog-before-close', {detail: {reason}});
                if (!e.defaultPrevented) { dialog.open = false; fire(dialog, 'sb:dialog-closed'); }
            }},
        Share: {openShareModal(...args) { rootWindow.shared = args; }},
        history: {state: {unrelated: 3, ...(saved ? {sbPartnerDetail: saved} : {})},
            replaceState(state) { this.state = state; }},
        console: {error(error) { errors.push(error); }},
        location: {assign(url) { destinations.push(url); }, reload() { rootWindow.reloaded = true; }},
        fetch(url, options) {
            requests.push({url, options});
            return response ? response(url, options) : Promise.resolve({
                ok: true, json: async () => ({success: true, contacts: [], specialties: [],
                    overview: '', meetings_found: 0})});
        }};
    function fire(target, type, extra = {}) {
        const e = {type, target, button: 0, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; }, ...extra};
        target.dispatchEvent(e); return e;
    }
    vm.runInNewContext(source, {window: rootWindow, Share: rootWindow.Share});
    const emit = (type, extra = {}) => {
        const e = {preventDefault() { this.defaultPrevented = true; }, ...extra};
        (events[type] || []).forEach(fn => fn(e)); return e;
    };
    return {nodes, root, window: rootWindow, requests, destinations, errors, fire, emit,
        panes, photo, contactForm, deleteForm, share, node,
        previews, previewSelections, noteMounts};
}
const settle = async () => { for (let n = 0; n < 20; n++) await Promise.resolve(); };

function linkedNote(h) {
    const row = h.node('tr', {dataset: {noteId: '9'}});
    const cell = h.node('td'), excerpt = h.node('p'), date = h.node('td');
    const link = h.node('a', {href: '/note/9',
        dataset: {partnerNotePreview: '', recordTitle: 'Customer - Oct 23, 2026'}});
    link.click = () => {
        const click = h.fire(h.root, 'click', {target: link});
        const event = h.emit('doc:click', click);
        if (!event.defaultPrevented) h.destinations.push(link.href);
    };
    cell.append(link, excerpt);
    row.append(cell, date);
    h.root.append(row);
    return {row, cell, excerpt, date, link};
}

test('note rows and links reuse the standard preview and retain the full-page destination', () => {
    const h = harness(), note = linkedNote(h);
    for (const target of [note.excerpt, note.date, note.cell]) {
        h.fire(h.root, 'click', {target});
    }
    note.link.click();
    assert.deepEqual(h.destinations, []);
    assert.equal(h.previewSelections.length, 4);
    assert.equal(h.previewSelections[0].id, 9);
    assert.equal(h.previewSelections[0].title, 'Customer - Oct 23, 2026');
    assert.equal(h.previews.length, 1);
    assert.equal(h.previews[0].id, 'partnerNotePreview');
    const options = h.previews[0].options;
    assert.equal(options.loadUrl(9), '/api/note/9/detail');
    assert.equal(options.fullUrl(9), note.link.href);
    assert.equal(options.fragmentSelector, '[data-sb-note-detail]');
    assert.equal(options.identityAttribute, 'data-sb-note-id');
    options.mount('note content');
    assert.deepEqual(h.noteMounts, ['note content']);
    assert.equal(h.window.history.state.sbPartnerDetail.id, 6);
    assert.equal(h.requests.length, 0);
});

test('modified note clicks retain native navigation without creating a preview', () => {
    const h = harness(), note = linkedNote(h);
    for (const extra of [{ctrlKey: true}, {metaKey: true},
        {shiftKey: true}, {altKey: true}, {button: 1}, {defaultPrevented: true}]) {
        for (const target of [note.excerpt, note.link]) {
            const event = h.fire(h.root, 'click', {target, ...extra});
            assert.equal(event.defaultPrevented, extra.defaultPrevented || false);
        }
    }
    assert.deepEqual(h.destinations, []);
    assert.equal(h.previews.length, 0);
});

test('note row respects pending writes and unsaved contact confirmation', () => {
    const h = harness(), note = linkedNote(h);
    h.fire(h.nodes.get('addContactBtn'), 'click');
    h.nodes.get('contact_name').value = 'Unsaved contact';
    h.fire(h.root, 'click', {target: note.excerpt});
    assert.deepEqual(h.destinations, []);
    assert.equal(h.nodes.get('partnerDiscardDialog').open, true);
    h.fire(h.nodes.get('partnerDiscardConfirm'), 'click');
    assert.equal(h.previewSelections.length, 1);
    assert.deepEqual(h.destinations, []);

    const pending = harness(), pendingNote = linkedNote(pending);
    pending.fire(pending.contactForm, 'submit');
    pending.fire(pending.root, 'click', {target: pendingNote.date});
    assert.deepEqual(pending.destinations, []);
    assert.equal(pending.previews.length, 0);
});

test('preview setup errors are visible and clicking again retries', () => {
    const h = harness(), note = linkedNote(h);
    const controller = h.window.SBRecordDialog;
    h.window.SBRecordDialog = {create() { throw new Error('Unavailable'); }};
    note.link.click();
    assert.equal(h.nodes.get('partnerNotice').hidden, false);
    assert.match(h.nodes.get('partnerNotice').textContent, /Could not open the note preview/);
    assert.equal(h.errors.length, 1);
    assert.deepEqual(h.destinations, []);
    h.window.SBRecordDialog = controller;
    note.link.click();
    assert.equal(h.previewSelections.length, 1);
});

test('domain normalization, exact selected payload and malformed Back validation', () => {
    assert.equal(api.normalizeDomain(' HTTPS://WWW.Example.Test/path '), 'example.test');
    assert.deepEqual(api.selectedResults({specialties: [{name: 'No'}, {name: 'Selected'}],
        contacts: [{name: 'Chosen'}]}, ' Original\n\n---\n\nNew ', [1], [0]),
    {overview: 'Original\n\n---\n\nNew', specialties: ['Selected'], contacts: [{name: 'Chosen'}]});
    assert.throws(() => api.validateHistory({id: 6, scroll: [[0, 0], [0, -1], [0, 0]]}, 6));
    assert.throws(() => api.validateHistory({id: 6, scroll: [[0, 0], [0, 0], [0, 0]],
        draft: {action: '/partners/7/contacts/new', values: {}}}, 6));
});
test('HTTP, success-false, invalid JSON and network errors surface rather than succeed', async () => {
    for (const fetch of [
        async () => ({ok: false, status: 503, json: async () => ({error: 'Unavailable'})}),
        async () => ({ok: true, json: async () => ({success: false, error: 'Failed'})}),
        async () => ({ok: true, json: async () => { throw new SyntaxError('bad JSON'); }}),
        async () => { throw new Error('offline'); }]) {
        await assert.rejects(api.request(fetch, '/isolated', 'POST', {}));
    }
});
test('Add/Edit contact exact values, paths, local draft and nested discard lifecycle', () => {
    const h = harness(), get = id => h.nodes.get(id);
    const edit = h.node('button', {dataset: {editContact: '1'}});
    h.fire(h.root, 'click', {target: edit});
    assert.equal(h.contactForm.action, '/partners/6/contacts/1/edit');
    assert.equal(get('contact_name').value, "O'Brien <tag>");
    assert.equal(get('is_primary').checked, true);
    get('contact_title').value = 'Draft'; h.fire(h.contactForm, 'input');
    h.window.SBDialog.close('addContactModal');
    assert.equal(get('addContactModal').open, true);
    assert.equal(get('partnerDiscardDialog').open, true);
    h.window.SBDialog.close('partnerDiscardDialog');
    assert.equal(get('contact_title').value, 'Draft');
    h.window.SBDialog.close('addContactModal');
    h.fire(get('partnerDiscardConfirm'), 'click');
    assert.equal(get('addContactModal').open, false);
    h.fire(get('addContactBtn'), 'click');
    assert.equal(h.contactForm.action, '/partners/6/contacts/new');
    assert.equal(get('contact_name').value, '');
    assert.equal(h.requests.length, 0);
});
test('manual submission does not disable successful named inputs or prompt unload', () => {
    const h = harness();
    h.fire(h.nodes.get('addContactBtn'), 'click');
    h.nodes.get('contact_name').value = 'Manual';
    h.fire(h.contactForm, 'submit');
    assert.equal(h.nodes.get('contact_name').disabled, false);
    assert.equal(h.nodes.get('is_primary').disabled, false);
    assert.equal(h.nodes.get('contactSubmitBtn').disabled, true);
    assert.equal(h.emit('beforeunload').defaultPrevented, undefined);
    assert.equal(h.fire(h.contactForm, 'submit').defaultPrevented, true);
    assert.equal(h.requests.length, 0);
    h.emit('pageshow');
    assert.equal(h.nodes.get('contactSubmitBtn').disabled, false);
});
test('entry-local panes/draft restore, unrelated history retained, failed server values win', () => {
    const saved = {id: 6, scroll: [[20, 0], [30, 50], [40, 0]], draft: {
        action: '/partners/6/contacts/1/edit', title: 'Edit Contact', baseline: '{}',
        values: {contact_name: 'Back draft', contact_title: 'Role',
            contact_email: 'back@test', is_primary: 'on'}}};
    const h = harness({saved});
    assert.equal(h.panes[1].scrollLeft, 50);
    assert.equal(h.nodes.get('contact_name').value, 'Back draft');
    h.fire(h.contactForm, 'input');
    assert.equal(h.window.history.state.unrelated, 3);
    assert.equal(h.emit('beforeunload').defaultPrevented, true);
    const failed = harness({saved, failure: {path: '/partners/6/contacts/new',
        values: {contact_name: 'Failed entry', contact_title: '', contact_email: '', is_primary: ''}}});
    assert.equal(failed.nodes.get('contact_name').value, 'Failed entry');
    assert.equal(failed.nodes.get('contactFailure').hidden, false);
});
test('delete contact confirmation uses exact POST path and safe text; sharing delegates once', () => {
    const h = harness();
    h.fire(h.root, 'click', {target: h.node('button', {dataset: {deleteContact: '1'}})});
    assert.equal(h.deleteForm.action, '/partners/6/contacts/1/delete');
    assert.equal(h.nodes.get('deleteContactText').textContent, "Delete O'Brien <tag>?");
    h.window.SBDialog.close('deleteContactDialog');
    h.fire(h.share, 'click');
    assert.deepEqual([...h.window.shared], ['partner', 6]);
    assert.equal(h.requests.length, 0);
});
test('domain suggestion cancel never writes; blank input stays open with explicit error', async () => {
    const h = harness();
    h.fire(h.nodes.get('scrapeBtn'), 'click');
    assert.equal(h.nodes.get('scrapeDomainInput').value, 'example.test');
    h.nodes.get('scrapeDomainInput').value = '';
    h.fire(h.nodes.get('scrapeDomainForm'), 'submit'); await settle();
    assert.equal(h.nodes.get('scrapeDomainError').hidden, false);
    assert.equal(h.nodes.get('scrapeDomainModal').open, true);
    h.window.SBDialog.close('scrapeDomainModal');
    assert.equal(h.requests.length, 0);
});
test('domain PUT preserves original associations/contacts, then exact scrape POST', async () => {
    const h = harness();
    h.fire(h.nodes.get('scrapeBtn'), 'click');
    h.nodes.get('scrapeDomainInput').value = 'HTTPS://WWW.New.Test/path';
    h.fire(h.nodes.get('scrapeDomainForm'), 'submit'); await settle();
    assert.equal(h.requests[0].url, '/api/partners/6');
    assert.equal(h.requests[0].options.method, 'PUT');
    const payload = JSON.parse(h.requests[0].options.body);
    assert.equal(payload.website, 'new.test');
    assert.equal(payload.contacts[0].id, 1);
    assert.deepEqual(payload.specialty_ids, [4]);
    assert.equal(payload.rating, 0);
    assert.equal(payload.overview, 'Original');
    assert.equal(h.requests[1].url, '/api/partners/6/scrape');
    assert.equal(h.requests[1].options.method, 'POST');
    assert.equal(h.nodes.get('scrapeDomainModal').open, false);
    assert.equal(h.nodes.get('scrapeReviewModal').open, true);
});
test('failed domain save retains typed value, unlocks input and avoids AI until retry', async () => {
    let failed = true;
    const h = harness({response: async () => ({ok: !failed, status: 503,
        json: async () => failed ? {error: 'Save failed'} : {success: true}})});
    h.fire(h.nodes.get('scrapeBtn'), 'click');
    h.nodes.get('scrapeDomainInput').value = 'new.test';
    h.fire(h.nodes.get('scrapeDomainForm'), 'submit'); await settle();
    assert.equal(h.requests.length, 1);
    assert.equal(h.nodes.get('scrapeDomainInput').value, 'new.test');
    assert.equal(h.nodes.get('scrapeDomainInput').disabled, false);
    failed = false;
    h.fire(h.nodes.get('scrapeDomainForm'), 'submit'); await settle();
    assert.equal(h.requests.at(-1).url, '/api/partners/6/scrape');
});
test('existing domain skips PUT; pending guards close, paste switching, navigation and duplicate scan', async () => {
    let resolve;
    const h = harness({website: 'example.test', response: () => new Promise(r => { resolve = r; })});
    h.fire(h.nodes.get('scrapeBtn'), 'click');
    h.fire(h.nodes.get('scrapeBtn'), 'click');
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].url, '/api/partners/6/scrape');
    h.nodes.get('scrapeReviewModal').open = true;
    h.window.SBDialog.close('scrapeReviewModal');
    assert.equal(h.nodes.get('scrapeReviewModal').open, true);
    const link = h.node('a', {href: '/partners'});
    assert.equal(h.emit('doc:click', {target: link, button: 0}).defaultPrevented, true);
    assert.equal(h.emit('beforeunload').defaultPrevented, true);
    resolve({ok: true, json: async () => ({success: true})}); await settle();
    assert.equal(h.nodes.get('scrapeBtn').disabled, false);
});
test('review selections and notes survive apply failure and exact payload retries', async () => {
    let failApply = true;
    const data = {success: true, overview: '<tag>suggestion</tag>', meetings_found: 3,
        specialties: [{name: 'Existing', is_new: false}, {name: 'New', is_new: true}],
        contacts: [{name: '<script>safe</script>', is_new: true},
            {name: 'Unchanged', is_new: false, has_updates: false},
            {name: 'Update', is_new: false, has_updates: true, existing_id: 9,
                updates: ['Add email']}]};
    const h = harness({website: 'example.test', response: async url => ({
        ok: !url.endsWith('/apply') || !failApply, status: 503,
        json: async () => url.endsWith('/apply') ? failApply
            ? {error: 'Import failed'} : {success: true} : data})});
    h.fire(h.nodes.get('scrapeBtn'), 'click'); await settle();
    const textarea = h.nodes.get('scrapeOverviewText');
    assert.equal(textarea.value, 'Original\n\n---\n\n<tag>suggestion</tag>');
    const spec = h.nodes.get('scrapeReviewBody').querySelectorAll('.scrape-specialty:checked:not(:disabled)');
    assert.equal(spec.length, 1);
    textarea.value = 'Edited import';
    h.fire(h.nodes.get('scrapeApplyBtn'), 'click'); await settle();
    assert.equal(h.nodes.get('scrapeReviewError').textContent, 'Import failed');
    assert.equal(h.nodes.get('scrapeReviewModal').open, true);
    assert.equal(textarea.value, 'Edited import');
    const first = JSON.parse(h.requests.at(-1).options.body);
    assert.deepEqual(first.specialties, ['New']);
    assert.equal(first.contacts.length, 2);
    assert.equal(first.overview, 'Edited import');
    failApply = false;
    h.fire(h.nodes.get('scrapeApplyBtn'), 'click'); await settle();
    assert.deepEqual(JSON.parse(h.requests.at(-1).options.body), first);
    assert.equal(h.window.reloaded, true);
});
test('empty result retry error remains retryable and successful retry restores review', async () => {
    let fail = false;
    const h = harness({website: 'example.test', response: async () => ({
        ok: !fail, status: 504, json: async () => fail ? {error: 'Timed out'}
            : {success: true, contacts: [], specialties: []}})});
    h.fire(h.nodes.get('scrapeBtn'), 'click'); await settle();
    assert.match(h.nodes.get('scrapeReviewBody').children.at(-1).textContent, /second attempt/);
    fail = true; h.fire(h.nodes.get('scrapeRetryBtn'), 'click'); await settle();
    assert.equal(h.nodes.get('scrapeReviewError').textContent, 'Timed out');
    assert.equal(h.nodes.get('scrapeRetryBtn').disabled, false);
    fail = false; h.fire(h.nodes.get('scrapeRetryBtn'), 'click'); await settle();
    assert.equal(h.nodes.get('scrapeReviewError').hidden, true);
    assert.equal(h.requests.length, 3);
});
test('photo remove failure retains source and dialog, retry uses exact endpoint and clears full source', async () => {
    let fail = true;
    const h = harness({response: async () => ({ok: !fail, status: 503,
        json: async () => fail ? {error: 'Photo failed'} : {success: true, photo_b64: null}})});
    h.photo.dataset.fullSrc = 'data:image/png;base64,original';
    h.fire(h.root, 'click', {target: h.photo});
    h.fire(h.nodes.get('contactPhotoRemoveBtn'), 'click'); await settle();
    assert.equal(h.nodes.get('contactPhotoStatus').textContent, 'Photo failed');
    assert.equal(h.nodes.get('contactPhotoModal').open, true);
    assert.equal(h.photo.dataset.fullSrc, 'data:image/png;base64,original');
    fail = false; h.fire(h.nodes.get('contactPhotoRemoveBtn'), 'click'); await settle();
    assert.equal(h.requests.at(-1).url, '/api/partner/contact/1/photo');
    assert.equal(h.requests.at(-1).options.body, '{"photo_b64":null}');
    assert.equal(h.photo.dataset.fullSrc, undefined);
    assert.equal(h.nodes.get('contactPhotoModal').open, false);
});
test('clipboard photo success, file-read failure and network failure remain isolated and retryable', async () => {
    let readFailure = true, networkFailure = false;
    const h = harness({response: async () => {
        if (networkFailure) throw new Error('Photo offline');
        return {ok: true, json: async () => ({success: true, photo_b64: 'cropped'})};
    }});
    h.window.FileReader = class {
        readAsDataURL() {
            this.result = 'data:image/png;base64,raw';
            if (readFailure) this.onerror();
            else this.onload();
        }
    };
    h.fire(h.root, 'click', {target: h.photo});
    const clipboardData = {items: [{type: 'image/png', getAsFile: () => ({})}]};
    h.emit('doc:paste', {clipboardData}); await settle();
    assert.match(h.nodes.get('contactPhotoStatus').textContent, /Could not read/);
    assert.equal(h.requests.length, 0);
    readFailure = false; networkFailure = true;
    h.emit('doc:paste', {clipboardData}); await settle();
    assert.equal(h.nodes.get('contactPhotoStatus').textContent, 'Photo offline');
    networkFailure = false;
    h.emit('doc:paste', {clipboardData}); await settle();
    assert.equal(h.requests.at(-1).url, '/api/partner/contact/1/photo');
    assert.equal(h.requests.at(-1).options.body, '{"photo_b64":"raw"}');
    assert.equal(h.photo.children[0].src, 'data:image/png;base64,cropped');
    assert.equal(h.photo.dataset.fullSrc, 'data:image/png;base64,raw');
    assert.equal(h.nodes.get('contactPhotoStatus').textContent, 'Saved!');
    assert.equal(h.nodes.get('contactPhotoModal').open, true);
});
