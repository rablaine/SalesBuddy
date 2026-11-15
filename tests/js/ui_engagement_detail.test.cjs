const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const {validateHistory, fetch: request} = require('../../static/js/ui-engagement-detail.js');
const partial = fs.readFileSync('templates/partials/_engagement_view_content.html', 'utf8');

test('native reading history validates identity, both positions and completed disclosure', () => {
    const good = {id: 7, story: 310, work: 90, completedOpen: true};
    assert.equal(validateHistory(good, 7), good);
    for (const bad of [null, {}, {...good, id: 8}, {...good, story: -1},
        {...good, work: NaN}, {...good, completedOpen: 'true'}]) {
        assert.throws(() => validateHistory(bad, 7));
    }
});

test('shared request gates writes through complete JSON and retains explicit failures for retry', async () => {
    const original = global.fetch, reports = [];
    const root = {_engagement: {report: text => reports.push(text)}};
    let finish;
    global.fetch = () => new Promise(resolve => { finish = resolve; });
    try {
        const pending = request(root, '/engagement/7/action-items', {method: 'POST'});
        assert.equal(root._engagement.pending, true);
        await assert.rejects(request(root, '/engagement/7/action-items', {method: 'POST'}),
            /already in progress/);
        finish({ok: true, status: 200, json: async () => ({success: true, task: {id: 3}})});
        const response = await pending;
        assert.equal(root._engagement.pending, false);
        assert.equal((await response.json()).task.id, 3);
        root._actionOrder = {isBusy: () => true};
        await assert.rejects(request(root, '/engagement/7/action-items', {method: 'POST'}),
            /already in progress/);
        delete root._actionOrder;
        for (const failure of [
            {ok: false, status: 500, json: async () => ({error: 'Save refused'})},
            {ok: true, status: 200, json: async () => ({success: false, error: 'Save refused'})},
            {ok: true, status: 200, json: async () => { throw new Error('Broken JSON'); }},
        ]) {
            global.fetch = async () => failure;
            await assert.rejects(request(root, '/save', {method: 'POST'}));
            assert.equal(root._engagement.pending, false);
            assert.match(reports.at(-1), /input is retained.*Retry/);
        }
    } finally { global.fetch = original; }
});

function tasks() {
    const fields = new Map(), calls = [], writes = [], dialogs = [];
    const ordering = {saves: 0};
    let reloads = 0;
    const element = id => {
        if (!fields.has(id)) fields.set(id, {id, value: '', style: {}, handlers: {},
            innerHTML: '', focus() {}, addEventListener(name, handler) {
                this.handlers[name] = handler;
            }, querySelectorAll() { return []; }, querySelector() { return null; }});
        return fields.get(id);
    };
    const root = {isConnected: true, _engagement: {reload: () => reloads++}};
    const doc = {currentScript: {closest: () => root}, getElementById: element};
    let quill;
    class Quill {
        constructor() {
            quill = this;
            this.root = {innerHTML: '', setAttribute() {},
                querySelector: selector => /<(img|table|video)/.test(this.root.innerHTML) ? {} : null};
        }
        getText() { return this.root.innerHTML.replace(/<[^>]*>/g, ''); }
    }
    let sortable;
    class Sortable { constructor(el, options) { sortable = options; } }
    const fetch = (url, options = {}) => new Promise((resolve, reject) =>
        writes.push({url, options, resolve, reject}));
    const context = {window: {document: doc, SBEngagementDetail: {
        document: () => doc, fetch: (_root, url, options) => fetch(url, options), mount() {},
    }, SBActionItemOrder: {mount(_root, options) {
        ordering.options = options;
        return {save() { ordering.saves++; }};
    }}}, Quill, Sortable, SBDialog: {open(dialog) { dialog.open = true; dialogs.push(dialog.id); }},
    fetch, console: {error() {}}, alert: text => calls.push(text), confirm: () => true,
    setTimeout: fn => fn(), viewActionItem: (...args) => calls.push(args)};
    const script = [...partial.matchAll(/<script>([\s\S]*?)<\/script>/g)]
        .map(match => match[1]).find(script => script.includes('const ENGAGEMENT_ID'));
    vm.runInNewContext(script.replaceAll('{{ engagement.id }}', '7'), context);
    const api = root._engagement;
    const reply = (index, task) => writes[index].resolve({json: async () =>
        ({success: true, task})});
    return {api, fields, element, writes, reply, dialogs, calls, root, ordering,
        quill: () => quill, sortable: () => sortable, reloads: () => reloads};
}
async function settle() { for (let i = 0; i < 15; i++) await Promise.resolve(); }

test('resource links bypass inline editing, which retains source and renders saved HTML', async () => {
    const source = '[Azure docs](https://learn.microsoft.com)';
    const handlers = {}, writes = [], editors = [];
    const value = {dataset: {storyField: 'solution_resources', storyValue: source},
        hidden: false, style: {removeProperty() {}},
        classList: {toggle() {}, remove() {}},
        addEventListener(name, handler) { handlers[name] = handler; },
        insertAdjacentElement(_position, element) { editors.push(element); },
        closest() { return null; }};
    const root = {_engagement: {report() {}}};
    const doc = {
        currentScript: {closest: () => root},
        querySelectorAll: () => [value],
        getElementById: () => ({textContent: ''}),
        createElement() {
            return {dataset: {}, handlers: {}, setAttribute() {}, focus() {}, select() {},
                addEventListener(name, handler) { this.handlers[name] = handler; },
                remove() { this.removed = true; }};
        },
    };
    const context = {window: {document: doc, SBEngagementDetail: {
        document: () => doc,
        fetch: (_root, url, options) => new Promise(resolve => writes.push({url, options, resolve})),
    }}, console};
    const script = [...partial.matchAll(/<script>([\s\S]*?)<\/script>/g)]
        .map(match => match[1]).find(script => script.includes('function finishStoryEdit'));
    vm.runInNewContext(script.replaceAll('{{ engagement.id }}', '7'), context);
    const link = {closest: () => ({})};
    handlers.click({target: link});
    handlers.keydown({target: link, key: 'Enter', preventDefault() {
        assert.fail('Link activation must retain its default behavior');
    }});
    assert.equal(editors.length, 0);
    handlers.click({target: value});
    assert.equal(editors[0].value, source);
    const editor = editors[0];
    editor.value = '[Updated](https://learn.microsoft.com/azure)';
    editor.handlers.keydown({key: 'Enter', shiftKey: false, preventDefault() {}});
    assert.equal(JSON.parse(writes[0].options.body).value, editor.value);
    const html = '<p><a href="https://learn.microsoft.com/azure" rel="noopener noreferrer">Updated</a></p>';
    writes[0].resolve({ok: true, json: async () => ({
        success: true, value: editor.value, display_value: editor.value,
        display_html: html, is_empty: false, story_completeness: 42,
    })});
    await settle();
    assert.equal(value.innerHTML, html);
    assert.equal(value.dataset.storyValue, editor.value);
    assert.equal(value.hidden, false);
    assert.equal(editor.removed, true);
});

test('native rich task create and edit keep all fields and image-only HTML', async () => {
    const h = tasks();
    h.api.openNewTaskModal();
    assert.equal(h.dialogs.at(-1), 'taskEditModal');
    h.element('editTaskTitle').value = 'Image task';
    h.element('editTaskDue').value = '2027-01-02';
    h.element('editTaskPriority').value = 'high';
    h.element('editTaskContact').value = 'Owner';
    h.quill().root.innerHTML = '<img src="/image.png">';
    h.api.saveEditedTask();
    assert.equal(h.writes[0].url, '/engagement/7/action-items');
    assert.equal(h.writes[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(h.writes[0].options.body), {title: 'Image task',
        description: '<img src="/image.png">', due_date: '2027-01-02',
        priority: 'high', contact: 'Owner'});
    h.writes[0].reject(new Error('Offline'));
    await settle();
    assert.equal(h.element('editTaskTitle').value, 'Image task');
    assert.equal(h.quill().root.innerHTML, '<img src="/image.png">');
    assert.equal(h.reloads(), 0);
    h.api.saveEditedTask();
    h.reply(1, {id: 12});
    await settle();
    assert.equal(h.reloads(), 1);
    h.api.editTask(12);
    h.reply(2, {id: 12, title: 'Existing', description: '<table><tr><td>Cell</td></tr></table>',
        due_date: null, priority: 'normal', contact: null});
    await settle();
    assert.equal(h.quill().root.innerHTML, '<table><tr><td>Cell</td></tr></table>');
    h.api.saveEditedTask();
    assert.equal(h.writes[3].url, '/action-item/12');
    assert.equal(h.writes[3].options.method, 'PUT');
    assert.match(JSON.parse(h.writes[3].options.body).description, /<table>/);
});

test('rapid task replacement ignores late and wrong-record reads rather than opening blank editors', async () => {
    const h = tasks();
    h.api.editTask(1); h.api.editTask(2);
    h.reply(1, {id: 2, title: 'Second'});
    await settle();
    h.reply(0, {id: 1, title: 'Late first'});
    await settle();
    assert.equal(h.element('editTaskTitle').value, 'Second');
    h.api.editTask(3);
    h.reply(2, {id: 99, title: 'Wrong record'});
    await settle();
    assert.equal(h.element('editTaskTitle').value, 'Second');
    h.api.editTask(4);
    h.api.openNewTaskModal();
    h.reply(3, {id: 4, title: 'Old edit'});
    await settle();
    assert.equal(h.element('editTaskTitle').value, '');
});

test('task writes keep their payloads and dragging delegates to shared order persistence', async () => {
    const h = tasks();
    h.element('ghostTaskTitle').value = 'Quick task';
    h.api.quickAddTask();
    assert.deepEqual(JSON.parse(h.writes[0].options.body), {title: 'Quick task', due_date: null});
    h.reply(0, {id: 1}); await settle();
    h.api.toggleTask(9);
    assert.equal(h.writes[1].url, '/action-item/9/toggle');
    assert.equal(h.writes[1].options.method, 'POST');
    h.reply(1, {id: 9}); await settle();
    h.api.deleteTask(9);
    assert.equal(h.writes[2].options.method, 'DELETE');
    h.sortable().onEnd();
    assert.equal(h.ordering.saves, 1);
    assert.equal(h.ordering.options.url, '/engagement/7/action-items/reorder');
    assert.equal(h.ordering.options.key, 'actionItemOrder:engagement:7');
    assert.equal(h.sortable().disabled, true);
    assert.equal(h.sortable().handle, '.drag-handle');
});
