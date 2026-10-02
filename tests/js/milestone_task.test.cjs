const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(
    path.join(__dirname, '../../templates/partials/milestone_view_content.html'), 'utf8'
);
const script = template.slice(
    template.indexOf('window.createTask = function()'),
    template.indexOf('// Modal chaining:')
);

async function submit(response, rejection = null) {
    const elements = {
        modal_task_subject: {value: 'Architecture session'},
        modal_task_category: {value: '861980004'},
        modal_task_description: {value: ''},
        modal_task_due_date: {value: '2026-10-03'},
        create_task_btn: {disabled: false, innerHTML: 'Create Task in MSX'},
    };
    let error = '';
    let reloads = 0;
    let request;
    const context = {
        document: {getElementById: id => elements[id]},
        window: {location: {reload: () => reloads++}},
        showTaskError: message => { error = message; },
        hideTaskError: () => { error = ''; },
        fetch: (_url, options) => {
            request = JSON.parse(options.body);
            return rejection ? Promise.reject(rejection) : Promise.resolve(response);
        },
    };
    vm.runInNewContext(script, context);
    context.window.createTask();
    await new Promise(resolve => setImmediate(resolve));
    return {error, reloads, request, button: elements.create_task_btn};
}

function jsonResponse(status, body) {
    return {
        status, ok: status < 400,
        headers: {get: () => 'application/json'},
        json: async () => body,
    };
}

test('HTML error responses show actionable status, not a JSON parser error', async () => {
    for (const status of [404, 500, 502]) {
        const result = await submit({
            status, ok: false,
            headers: {get: () => 'text/html; charset=utf-8'},
            json: () => { throw new Error('Unexpected token <'); },
        });
        assert.match(result.error, new RegExp(`HTTP ${status}`));
        assert.match(result.error, /Check the milestone in MSX before retrying/);
        assert.doesNotMatch(result.error, /Unexpected token|Network error/);
        assert.equal(result.button.disabled, false);
        assert.equal(result.reloads, 0);
    }
});

test('ordinary JSON errors preserve the server message and allow correction', async () => {
    const result = await submit(jsonResponse(400, {
        success: false, error: 'Task title is required',
    }));
    assert.equal(result.error, 'Task title is required');
    assert.equal(result.button.disabled, false);
    assert.equal(result.button.innerHTML, 'Create Task in MSX');
    assert.equal(result.reloads, 0);
});

test('remote-only creation disables repeat submission and displays its warning', async () => {
    const result = await submit(jsonResponse(500, {
        success: false, created_in_msx: true,
        error: 'The task was created in MSX. Do not create it again.',
    }));
    assert.equal(result.button.disabled, true);
    assert.match(result.error, /Do not create it again/);
    assert.equal(result.reloads, 0);
});

test('successful creation preserves the payload and reload behavior', async () => {
    const result = await submit(jsonResponse(200, {success: true}));
    assert.equal(result.error, '');
    assert.equal(result.reloads, 1);
    assert.equal(result.request.subject, 'Architecture session');
    assert.equal(result.request.task_category, 861980004);
    assert.equal(result.request.duration_minutes, 60);
    assert.equal(result.request.due_date, '2026-10-03T23:59:59Z');
});

test('network failures restore the form without a reload', async () => {
    const result = await submit(null, new TypeError('Failed to fetch'));
    assert.equal(result.error, 'Unable to create task: Failed to fetch');
    assert.equal(result.button.disabled, false);
    assert.equal(result.reloads, 0);
});

test('an HTTP failure cannot be mistaken for successful creation', async () => {
    const result = await submit(jsonResponse(500, {success: true}));
    assert.match(result.error, /HTTP 500/);
    assert.equal(result.reloads, 0);
});

test('unexpected JSON shapes show an actionable error', async () => {
    for (const body of [null, {}, [], {success: 'true'}]) {
        const result = await submit(jsonResponse(200, body));
        assert.match(result.error, /invalid response/);
        assert.match(result.error, /Check the milestone in MSX before retrying/);
        assert.equal(result.reloads, 0);
        assert.equal(result.button.disabled, false);
    }
});
