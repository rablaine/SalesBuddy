const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const test = require('node:test');

const source = fs.readFileSync('static/js/ui-note-milestone-hvas.js', 'utf8');
const firstId = '12345678-1234-1234-1234-123456789abc';
const secondId = '87654321-1234-1234-1234-123456789abc';
const row = {
    id: 1, subject: '<img src=x onerror=alert(1)>',
    category: 'Architecture session', description: '<script>not markup</script>',
    status: 'Completed', duration_minutes: 45,
    created: '2026-10-01T12:00:00+00:00', due: null, completed: null,
};

function harness(ids = [firstId], hasContainer = true) {
    const document = new EventEmitter();
    document.addEventListener = document.on.bind(document);
    document.activeElement = null;
    class Element extends EventEmitter {
        constructor(tag) {
            super();
            this.tagName = tag.toUpperCase();
            this.children = [];
            this.dataset = {};
            this.style = {};
            this.attributes = {};
            this.className = '';
            this.bounds = {left: 40, top: 100, bottom: 130, width: 400, height: 230};
        }
        addEventListener(name, handler) { this.on(name, handler); }
        setAttribute(name, value) { this.attributes[name] = value; }
        removeAttribute(name) { delete this.attributes[name]; }
        append(...children) {
            for (const child of children) {
                child.parentElement = this;
                this.children.push(child);
            }
        }
        replaceChildren(...children) {
            for (const child of this.children) child.parentElement = null;
            this.children = [];
            this.append(...children);
        }
        set textContent(value) { this._text = value; this.replaceChildren(); }
        get textContent() { return this._text || this.children.map(child => child.textContent).join(''); }
        get isConnected() { return this === document.body || !!this.parentElement?.isConnected; }
        contains(element) {
            return this === element || this.children.some(child => child.contains(element));
        }
        matches(selector) {
            if (selector === ':popover-open') return !!this.open;
            if (selector === ':hover') return !!this.hover;
            if (selector === ':hover, :focus') return !!this.hover || document.activeElement === this;
            if (selector === '[data-msx-milestone-id]') return !!this.dataset.msxMilestoneId;
            if (selector.startsWith('.')) return this.className.split(' ').includes(selector.slice(1));
            return this.tagName.toLowerCase() === selector;
        }
        querySelectorAll(selector) {
            return this.children.flatMap(child => [
                ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
            ]);
        }
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
        showPopover() { this.open = true; }
        hidePopover() { this.open = false; }
        getBoundingClientRect() { return this.bounds; }
        fire(name) {
            if (name === 'mouseenter') this.hover = true;
            if (name === 'mouseleave') this.hover = false;
            if (name === 'focus') document.activeElement = this;
            if (name === 'blur') document.activeElement = null;
            this.emit(name, {target: this});
        }
    }
    document.body = new Element('body');
    const container = new Element('div');
    document.body.append(container);
    document.getElementById = id => id === 'selected_milestone' && hasContainer ? container : null;
    document.createElement = tag => new Element(tag);
    function card(id) {
        const element = new Element('div');
        element.dataset.msxMilestoneId = id;
        const body = new Element('div');
        body.className = 'sb-nf-card-body';
        element.append(body);
        return element;
    }
    container.append(...ids.map(card));
    const requests = [], errors = [], timers = new Map();
    const window = Object.assign(new EventEmitter(), {innerWidth: 1200, innerHeight: 900});
    window.addEventListener = window.on.bind(window);
    vm.runInNewContext(source, {
        window, document, AbortController, URLSearchParams,
        console: {error: (...args) => errors.push(args)},
        setTimeout(callback, ms) { const token = {}; timers.set(token, {callback, ms}); return token; },
        clearTimeout: token => timers.delete(token),
        fetch(url, options) {
            return new Promise((resolve, reject) => {
                requests.push({url, options, resolve});
                options.signal.addEventListener('abort', () => reject(new Error('Timed out')));
            });
        },
    });
    return {
        window, document, container, requests, errors, timers, card,
        tooltip: () => document.body.children.find(element => element.id === 'noteMilestoneHvaDetails'),
        async finish(index, milestones, status = 200) {
            requests[index].resolve({
                ok: status === 200, status,
                json: async () => ({success: true, milestones}),
            });
            await new Promise(setImmediate);
        },
    };
}

test('selected milestone history is batched once and tags never submit or inject markup', async () => {
    const h = harness([firstId.toUpperCase(), secondId]);
    assert.equal(h.requests.length, 1);
    const query = new URL(h.requests[0].url, 'http://localhost').searchParams;
    assert.deepEqual(query.getAll('milestone_id'), [firstId, secondId]);
    assert.match(h.container.textContent, /Loading logged HVAs/);
    await h.finish(0, {[firstId]: [row], [secondId]: []});
    const tag = h.container.querySelector('.sb-note-hva-tag');
    assert.equal(tag.type, 'button');
    assert.equal(tag.textContent, row.category);
    assert.equal(tag.dataset.status, 'completed');
    assert.match(h.container.textContent, /No HVAs in local history/);
    tag.fire('mouseenter');
    const tooltip = h.tooltip();
    assert.equal(tooltip.open, true);
    assert.equal(tooltip.querySelector('strong').textContent, row.subject);
    assert.equal(tooltip.querySelector('strong').children.length, 0);
    assert.equal(tooltip.querySelector('p').textContent, row.description);
    assert.equal(tooltip.querySelector('p').children.length, 0);
    assert.match(tooltip.textContent, /45 minutes/);
    assert.match(tooltip.textContent, /Not recorded/);
    assert.equal(tag.attributes['aria-describedby'], tooltip.id);
    assert.equal(tooltip.attributes.role, 'tooltip');
    h.window.SBMilestoneHvas.render();
    assert.equal(h.requests.length, 1);
    assert.equal(h.container.querySelector('.sb-note-hva-tag'), tag);
});

test('keyboard focus exposes details and Escape dismisses without changing focus', async () => {
    const h = harness();
    await h.finish(0, {[firstId]: [row]});
    const tag = h.container.querySelector('.sb-note-hva-tag');
    tag.fire('focus');
    assert.equal(h.tooltip().open, true);
    let prevented = 0;
    h.document.emit('keydown', {key: 'Enter', ctrlKey: true,
        preventDefault() { prevented++; }, stopPropagation() { prevented++; }});
    assert.equal(prevented, 0, 'Ctrl+Enter must remain available to Save Note.');
    h.document.emit('keydown', {key: 'Escape',
        preventDefault() { prevented++; }, stopPropagation() { prevented++; }});
    assert.equal(h.tooltip().open, false);
    assert.equal(h.document.activeElement, tag);
    assert.equal(tag.attributes['aria-describedby'], undefined);
    assert.equal(prevented, 2);
});

test('hover details remain available while the pointer moves into the tooltip', async () => {
    const h = harness();
    await h.finish(0, {[firstId]: [row]});
    const tag = h.container.querySelector('.sb-note-hva-tag');
    tag.fire('mouseenter');
    tag.fire('mouseleave');
    const timer = [...h.timers.values()].find(item => item.ms === 120);
    h.tooltip().fire('mouseenter');
    timer.callback();
    assert.equal(h.tooltip().open, true);
    h.window.emit('scroll', {target: h.tooltip()});
    assert.equal(h.tooltip().open, true);
    h.window.emit('scroll', {target: h.container});
    assert.equal(h.tooltip().open, false);
});

test('a failed or malformed history request is visible and can be retried', async () => {
    for (const invalid of [null, [{...row, status: 'bad'}]]) {
        const h = harness();
        await h.finish(0, {[firstId]: invalid});
        assert.match(h.container.textContent, /Could not load logged HVAs/);
        assert.equal(h.errors.length, 1);
        const retry = h.container.querySelector('button');
        assert.equal(retry.type, 'button');
        retry.fire('click');
        assert.equal(h.requests.length, 2);
        await h.finish(1, {[firstId]: [row]});
        assert.equal(h.container.querySelectorAll('.sb-note-hva-tag').length, 1);
    }
    const h = harness();
    await h.finish(0, {}, 500);
    assert.match(h.container.textContent, /Could not load logged HVAs/);
});

test('removed milestones and stale responses cannot overwrite refreshed context', async () => {
    const h = harness();
    h.window.SBMilestoneHvas.refresh();
    await h.finish(1, {[firstId]: [row]});
    await h.finish(0, {[firstId]: []});
    assert.equal(h.container.querySelectorAll('.sb-note-hva-tag').length, 1);
    const tag = h.container.querySelector('.sb-note-hva-tag');
    tag.fire('focus');
    h.container.replaceChildren(h.card(secondId));
    h.window.SBMilestoneHvas.render();
    assert.equal(h.tooltip().open, false);
    await h.finish(2, {[secondId]: []});
    assert.equal(h.container.querySelectorAll('.sb-note-hva-tag').length, 0);
});

test('timed-out history reads become visible retryable errors', async () => {
    const h = harness();
    [...h.timers.values()].find(item => item.ms === 10000).callback();
    await new Promise(setImmediate);
    assert.equal(h.requests[0].options.signal.aborted, true);
    assert.match(h.container.textContent, /Could not load logged HVAs/);
    assert.equal(h.errors.length, 1);
});

test('large selections are chunked without limiting the HVA rows on any milestone', async () => {
    const ids = Array.from({length: 101}, (_, index) => 'milestone-' + index);
    const h = harness(ids);
    assert.equal(h.requests.length, 2);
    assert.equal(new URL(h.requests[0].url, 'http://localhost').searchParams.getAll(
        'milestone_id').length, 100);
    const data = Object.fromEntries(ids.slice(0, 100).map(id => [id, []]));
    data[ids[0]] = Array.from({length: 105}, (_, index) => ({...row, id: index}));
    await h.finish(0, data);
    assert.equal(h.container.querySelectorAll('.sb-note-hva-tag').length, 105);
    await h.finish(1, {[ids[100]]: []});
    assert.equal(h.container.querySelectorAll('.sb-note-hva-tag').length, 105);
});

test('general notes without milestone controls perform no requests', () => {
    const h = harness([], false);
    assert.equal(h.requests.length, 0);
    assert.equal(h.window.SBMilestoneHvas, undefined);
});

test('new, edited, restored and newly created task paths share the controller', () => {
    const template = fs.readFileSync('templates/note_form.html', 'utf8');
    assert.match(template, /script defer src=.*ui-note-milestone-hvas\.js/);
    const render = template.slice(template.indexOf('function renderSelectedMilestones()'),
        template.indexOf('function removeMilestone'));
    assert.match(render, /window\.SBMilestoneHvas\?\.render\(\)/);
    const create = template.slice(template.indexOf('function createTaskInMSX()'),
        template.indexOf('function clearCreatedTask'));
    assert.match(create, /window\.SBMilestoneHvas\?\.refresh\(\)/);
});
