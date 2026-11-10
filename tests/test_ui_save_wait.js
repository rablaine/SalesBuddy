const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(
    path.join(__dirname, '..', 'static', 'js', 'ui-save-wait.js'), 'utf8');
const approved = 'If you run into this, let Alex know how you ran into this (did you manually ' +
    'start syncs, is this start of the day and syncs are running in the background, stuff like ' +
    "that). He clearly knew this could happen but didn't think it actually would so now you gotta wait.";

function harness() {
    const timers = new Map(), calls = [], listeners = new Map(), errors = [];
    let timerId = 0, status = {success: true, writer: null, save: null};
    let resolve, reject;
    const saveResponse = new Promise((yes, no) => { resolve = yes; reject = no; });
    class Element {
        constructor() { this.children = []; this.hidden = false; }
        append(...elements) { this.children.push(...elements); }
        setAttribute() {}
        get lastElementChild() { return this.children.at(-1); }
        querySelector(selector) {
            if (selector === 'p') return this.children[1];
            return this.children.find(child => child.name === '_sb_save_id');
        }
    }
    class Form extends Element {
        method = 'POST';
        action = 'http://localhost/seller/new';
    }
    const body = new Element();
    const window = {
        location: {href: 'http://localhost/', origin: 'http://localhost'},
        addEventListener: (event, fn) => listeners.set(event, fn),
        fetch: async (input, options) => {
            calls.push({input, options});
            if (String(input).startsWith('/api/jobs/write-status')) {
                return new Response(JSON.stringify(status), {
                    headers: {'Content-Type': 'application/json'},
                });
            }
            return saveResponse;
        },
    };
    vm.runInNewContext(source, {
        window, document: {body, createElement: () => new Element(),
            addEventListener: (event, fn) => listeners.set(event, fn)},
        HTMLFormElement: Form, Request, Headers, URL, crypto, queueMicrotask,
        console: {error: (...args) => errors.push(args)},
        setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
        clearTimeout: id => timers.delete(id),
    });
    return {
        window, calls, body, Form, listeners, errors, resolve, reject,
        timerCount: () => timers.size,
        setStatus: value => { status = value; },
        tick: async () => {
            const [id, fn] = timers.entries().next().value;
            timers.delete(id);
            await fn();
        },
    };
}

test('a real save wait uses the exact approved copy and remains until its response', async () => {
    const h = harness();
    const request = h.window.fetch('/seller/new', {method: 'POST', body: 'name=Alex'});
    assert.ok(h.calls[0].options.headers.get('X-SB-Save-Id'));
    assert.equal(h.body.children.length, 0);
    h.setStatus({success: true, writer: {name: 'Fabric'},
        save: {pending: true, waiting: true}});
    await h.tick();
    const notice = h.body.children[0];
    assert.equal(notice.children[0].textContent, 'Waiting to save');
    assert.equal(notice.children[1].textContent, 'Fabric sync is saving data. Your save will ' +
        'continue when it finishes. Keep this page open until saving completes.');
    assert.equal(notice.children[2].textContent, approved);
    h.setStatus({success: true, writer: null, save: {pending: true, waiting: false}});
    await h.tick();
    assert.equal(notice.hidden, false);
    assert.match(notice.children[1].textContent, /Your save is still pending/);
    h.resolve(new Response('saved'));
    await request;
    assert.equal(notice.hidden, true);
});

test('an unrelated active sync does not produce a false blocked-save notice', async () => {
    const h = harness();
    const request = h.window.fetch('/api/query', {method: 'POST'});
    h.setStatus({success: true, writer: {name: 'Fabric'}, save: null});
    await h.tick();
    assert.equal(h.body.children.length, 0);
    h.resolve(new Response('query result'));
    await request;
});

test('HTTP failures remain failures and network failures end the pending notice', async () => {
    const h = harness();
    const request = h.window.fetch('/seller/new', {method: 'POST'});
    h.setStatus({success: true, writer: {name: 'ACR'}, save: {pending: true, waiting: true}});
    await h.tick();
    h.resolve(new Response('save failed', {status: 500}));
    assert.equal((await request).status, 500);
    assert.equal(h.body.children[0].hidden, true);

    const other = harness();
    const failed = other.window.fetch('/seller/new', {method: 'POST'});
    other.setStatus({success: true, writer: {name: 'ACR'},
        save: {pending: true, waiting: true}});
    await other.tick();
    other.reject(new Error('Network lost'));
    await assert.rejects(failed, /Network lost/);
    assert.equal(other.body.children[0].hidden, true);
});

test('native form saves get a token and duplicate submissions are blocked while pending', async () => {
    const h = harness();
    const form = new h.Form();
    const event = {target: form, defaultPrevented: false};
    h.listeners.get('submit')(event);
    await h.tick();
    assert.ok(form.children[0].value);
    let prevented = false;
    h.listeners.get('submit')({target: form,
        preventDefault: () => { prevented = true; }, stopImmediatePropagation() {}});
    assert.equal(prevented, true);
    h.setStatus({success: true, writer: {name: 'Fabric'},
        save: {pending: true, waiting: true}});
    await h.tick();
    assert.equal(h.body.children[0].hidden, false);
    h.setStatus({success: true, writer: null, save: {pending: false, response_status: 500}});
    await h.tick();
    assert.equal(h.body.children[0].hidden, true);
});

for (const reason of ['empty note validation', 'asynchronous note save']) {
    test(`${reason} does not leave the native form tracker blocking retries`, async () => {
        const h = harness();
        const form = new h.Form();
        const event = {target: form, defaultPrevented: false};
        h.listeners.get('submit')(event);
        // Native dispatch can run microtasks between document capture and form handlers.
        await Promise.resolve();
        event.defaultPrevented = true;
        const request = reason === 'asynchronous note save' ?
            h.window.fetch('/note/new', {method: 'POST'}) : null;
        if (h.timerCount()) await h.tick();
        if (request) {
            assert.equal(h.timerCount(), 1);
            assert.equal(h.calls.length, 1);
            h.resolve(new Response('saved'));
            await request;
        }
        assert.equal(h.timerCount(), 0);
        assert.equal(h.calls.length, request ? 1 : 0);

        let prevented = false;
        h.listeners.get('submit')({target: form, defaultPrevented: false,
            preventDefault() { prevented = true; }, stopImmediatePropagation() {}});
        assert.equal(prevented, false);
        await h.tick();
        assert.equal(h.timerCount(), 1);
        assert.equal(h.body.children.length, 0);
        h.listeners.get('pagehide')();
        assert.equal(h.timerCount(), 0);
    });
}

test('status failures are explicit and do not claim the user save was successful', async () => {
    const h = harness();
    const request = h.window.fetch('/seller/new', {method: 'POST'});
    h.setStatus({success: true, writer: {name: 'Fabric'},
        save: {pending: true, waiting: true}});
    await h.tick();
    h.setStatus({success: false, error: 'Status unavailable'});
    await h.tick();
    assert.equal(h.errors.length, 1);
    assert.match(h.body.children[0].children[1].textContent, /Save status is unavailable/);
    assert.equal(h.body.children[0].hidden, false);
    h.resolve(new Response('result'));
    await request;
});

test('GET and foreign requests are not modified, and Request headers are preserved', async () => {
    const h = harness();
    h.resolve(new Response('result'));
    await h.window.fetch('https://other.example/write', {method: 'POST'});
    assert.equal(h.calls[0].options.headers, undefined);
    await h.window.fetch('/read');
    assert.equal(h.calls[1].options.headers, undefined);
    await h.window.fetch(new Request('http://localhost/write', {
        method: 'POST', headers: {'X-Existing': 'preserved'},
    }));
    assert.equal(h.calls[2].options.headers.get('X-Existing'), 'preserved');
    assert.ok(h.calls[2].options.headers.get('X-SB-Save-Id'));
});
