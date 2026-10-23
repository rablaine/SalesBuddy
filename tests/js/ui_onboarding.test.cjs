const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness({
    autoOpen = false, imported = true, authenticated = true, family = 'graphite', mode = 'dark',
    role = 'se', specialty = 'infra', buckets = ['Fabric'], selected = ['Fabric'],
    storageError = false,
} = {}) {
    const records = new Map(), requests = [], handlers = {}, saved = [], errors = [];
    const defaultResponse = (url, options = {}) => {
        if (url === '/api/msx/status') return {authenticated};
        if (url === '/api/revenue/buckets') return buckets;
        if (url === '/api/revenue/compensated-buckets') return options.method === 'POST'
            ? {success: true, fiscal_year: 'FY27', taxonomy_version: 4} : selected;
        if (url === '/api/preferences/se-specialty') return JSON.parse(options.body);
        if (url === '/api/preferences/dismiss-welcome-modal') {
            return {first_run_modal_dismissed: true};
        }
        return {success: true};
    };
    let responder = defaultResponse;
    class Element {
        constructor(id) {
            this.id = id; this.handlers = {}; this.dataset = {};
            this.style = {}; this.attributes = {}; this.classes = new Set();
            this.children = [];
            this.scrollTop = 0; this.scrollHeight = 1600;
            this.open = false; this.disabled = false; this.value = '';
            this.classList = {
                add: value => this.classes.add(value),
                remove: value => this.classes.delete(value),
                toggle: (value, state) => state ? this.classes.add(value) : this.classes.delete(value),
            };
            records.set(id, this);
        }
        addEventListener(type, handler) { this.handlers[type] = handler; }
        async fire(type, props = {}) {
            const event = {...props, preventDefault() { this.prevented = true; }};
            await this.handlers[type]?.call(this, event); return event;
        }
        getAttribute(name) { return this.attributes[name]; }
        setAttribute(name, value) { this.attributes[name] = value; }
        appendChild(child) { this.children.push(child); }
        replaceChildren(...children) { this.children = children; }
        querySelectorAll(selector) {
            return this.children.flatMap(child => [
                ...(selector === 'input' && child.tagName === 'INPUT' ? [child] : []),
                ...child.querySelectorAll(selector),
            ]);
        }
        blur() {}
    }
    const get = id => records.get(id) || new Element(id);
    const root = get('root');
    root.attributes['data-sb-mode'] = mode;
    root.attributes['data-bs-theme'] = mode;
    root.attributes['data-sb-family'] = family;
    const dialog = get('welcomeModal'); dialog.dataset.sbAutoOpen = String(autoOpen);
    get('onboardSeSpecialty').value = autoOpen ? '' : specialty;
    let generated = 0;
    const document = {documentElement: root, getElementById: get,
        createElement(tag) {
            const element = new Element(`generated-${++generated}`);
            element.tagName = tag.toUpperCase();
            return element;
        },
        querySelectorAll: selector => selector === '.onboarding-step'
            ? [get('onboardingStep1'), get('onboardingStep2')] : []};
    const window = {
        addEventListener: (type, fn) => { handlers[type] = fn; }, removeEventListener() {},
        location: {reload() { window.reloaded = true; }},
        SBDialog: {
            open(element) { element.open = true; },
            close(element, reason) {
                const event = {detail: {reason}, preventDefault() { this.prevented = true; }};
                element.handlers['sb:dialog-before-close']?.(event);
                if (!event.prevented) element.open = false;
            },
        },
    };
    const source = fs.readFileSync('templates/partials/_onboarding_js.html', 'utf8')
        .split('<script>')[1].split('</script>')[0]
        .replace(/\{\{[\s\S]*?\}\}/g, expression => {
            if (/accounts_synced|has_milestones|has_revenue/.test(expression)) {
                const key = /accounts_synced/.test(expression) ? 'accounts'
                    : /has_milestones/.test(expression) ? 'milestones' : 'revenue';
                return String(typeof imported === 'boolean' ? imported : imported[key]);
            }
            if (/user_role/.test(expression)) return role;
            return '';
        });
    vm.runInNewContext(source, {
        window, document, console: {...console, error: (...args) => errors.push(args)}, TextDecoder,
        fetch: async (url, options = {}) => {
            requests.push({url, method: options.method || 'GET', body: options.body});
            const data = await responder(url, options);
            if (data?.headers && data?.body) return data;
            return {ok: true, json: async () => data};
        },
        localStorage: {getItem: () => null, setItem: (key, value) => {
            if (storageError) throw new Error('Storage unavailable');
            saved.push({key, value});
        }},
        setTimeout() {}, setInterval() {}, clearInterval() {},
    });
    handlers.load();
    return {window, records, get, dialog, requests, saved, errors, defaultResponse,
        ready: () => new Promise(resolve => setImmediate(resolve)),
        responder: fn => { responder = fn; }};
}

test('completed users do not auto-open setup or send any request on page load', () => {
    const h = harness();
    assert.equal(h.dialog.open, false);
    assert.equal(typeof h.window.openSetupWizard, 'function');
    assert.deepEqual(h.requests, []);
    assert.deepEqual(h.saved, []);
});

for (const mode of ['light', 'dark']) {
    for (const family of ['graphite', 'midnight', 'harbor', 'carbon']) {
        test(`onboarding saves ${family} independently of ${mode} appearance`, async () => {
            const h = harness({family: 'harbor', mode, imported: false, autoOpen: true});
            const picker = h.get('onboardThemeFamily');
            assert.equal(picker.value, 'harbor');
            assert.deepEqual(h.requests, []);
            h.responder(async () => ({theme_family: family}));
            picker.value = family;
            await picker.fire('change');
            assert.equal(h.get('root').getAttribute('data-sb-family'), family);
            assert.equal(h.get('root').getAttribute('data-sb-mode'), mode);
            assert.equal(h.get('root').getAttribute('data-bs-theme'), mode);
            assert.equal(picker.disabled, false);
            if (family === 'harbor') {
                assert.deepEqual(h.requests, []);
            } else {
                assert.deepEqual(h.requests.map(request => [request.url, request.method,
                    JSON.parse(request.body)]), [
                    ['/api/preferences/theme-family', 'POST', {theme_family: family}],
                ]);
                assert.equal(h.get('onboardThemeResult').textContent, 'Saved.');
            }
        });
    }
}

test('theme saves wait for confirmation and ignore duplicate changes while pending', async () => {
    const h = harness();
    let finish;
    h.responder(() => new Promise(resolve => { finish = resolve; }));
    const picker = h.get('onboardThemeFamily');
    picker.value = 'carbon';
    const pending = picker.fire('change');
    await picker.fire('change');
    assert.equal(picker.disabled, true);
    assert.equal(h.get('root').getAttribute('data-sb-family'), 'graphite');
    assert.equal(h.get('onboardThemeResult').textContent, 'Saving...');
    assert.equal(h.requests.length, 1);
    finish({theme_family: 'carbon'});
    await pending;
    assert.equal(h.get('root').getAttribute('data-sb-family'), 'carbon');
    assert.equal(picker.disabled, false);
});

for (const failure of ['network', 'http', 'mismatch', 'json']) {
    test(`failed theme save (${failure}) retains the confirmed theme and permits retry`, async () => {
        const h = harness({family: 'midnight'});
        h.responder(async () => {
            if (failure === 'network') throw new Error('Network unavailable');
            if (failure === 'http') return {
                headers: {}, body: {}, ok: false, json: async () => ({error: 'Save failed'}),
            };
            if (failure === 'json') return {
                headers: {}, body: {}, ok: true, json: async () => { throw new Error('Invalid JSON'); },
            };
            return {theme_family: 'graphite'};
        });
        const originalError = console.error;
        try {
            console.error = () => {};
            h.get('onboardThemeFamily').value = 'carbon';
            await h.get('onboardThemeFamily').fire('change');
        } finally {
            console.error = originalError;
        }
        assert.equal(h.get('root').getAttribute('data-sb-family'), 'midnight');
        assert.equal(h.get('onboardThemeFamily').value, 'midnight');
        assert.equal(h.get('onboardThemeFamily').disabled, false);
        assert.equal(h.get('onboardThemeResult').getAttribute('role'), 'alert');
        assert.match(h.get('onboardThemeResult').textContent, /Could not save theme/);
        h.responder(async () => ({theme_family: 'carbon'}));
        h.get('onboardThemeFamily').value = 'carbon';
        await h.get('onboardThemeFamily').fire('change');
        assert.equal(h.get('root').getAttribute('data-sb-family'), 'carbon');
        assert.equal(h.get('onboardThemeResult').getAttribute('role'), 'status');
    });
}

test('setup review resyncs its picker after the theme changes in Settings', async () => {
    const h = harness();
    h.get('root').setAttribute('data-sb-family', 'carbon');
    await h.window.openSetupWizard();
    assert.equal(h.get('onboardThemeFamily').value, 'carbon');
    assert.ok(h.requests.every(request => request.method === 'GET'));
});

test('review reopens actual completed progress using GETs only and closes without preference writes', async () => {
    const h = harness(); await h.window.openSetupWizard();
    assert.equal(h.dialog.open, true);
    assert.equal(h.get('onboardingStepBadge').textContent, 'Step 2 of 2');
    for (const phase of ['Accounts', 'Milestones', 'Revenue'])
        assert.equal(h.get(`phase${phase}Status`).textContent, 'Already imported');
    assert.deepEqual(h.requests.map(request => request.method), ['GET', 'GET', 'GET']);
    assert.equal(h.get('onboardNextBtn').disabled, false);
    await h.get('setupReviewClose').fire('click');
    assert.equal(h.dialog.open, false);
    assert.deepEqual(h.saved, []);
    assert.equal(h.window.reloaded, undefined);
});

test('first-run auto-opens mandatory sign-in without automatically signing in or importing', () => {
    const h = harness({autoOpen: true, imported: false});
    assert.equal(h.dialog.open, true);
    assert.equal(h.get('onboardingStepBadge').textContent, 'Step 1 of 2');
    assert.equal(h.get('onboardNextBtn').disabled, true);
    h.window.SBDialog.close(h.dialog, 'escape');
    assert.equal(h.dialog.open, true);
    assert.deepEqual(h.requests, []);
});

test('reopening review refreshes auth presentation rather than retaining an expired success', async () => {
    const h = harness(); await h.window.openSetupWizard();
    await h.get('setupReviewClose').fire('click');
    h.responder(async url => url === '/api/msx/status' ? {authenticated: false} : []);
    await h.window.openSetupWizard();
    assert.equal(h.get('onboardingStepBadge').textContent, 'Step 1 of 2');
    assert.equal(h.get('signInInitial').classes.has('sb-setup-hidden'), false);
    assert.equal(h.get('signInSuccess').classes.has('sb-setup-hidden'), true);
    assert.ok(h.requests.every(request => request.method === 'GET'));
});

test('review handles a failed status read without POSTs and still allows closing', async () => {
    const h = harness({role: 'dss'});
    h.responder(async url => {
        if (url === '/api/msx/status') throw new Error('Read unavailable');
        return [];
    });
    await h.window.openSetupWizard();
    assert.equal(h.dialog.open, true);
    assert.equal(h.get('onboardingStepBadge').textContent, 'Step 1 of 2');
    assert.equal(h.get('onboardNextBtn').disabled, false);
    assert.ok(h.requests.every(request => request.method === 'GET'));
    await h.get('onboardNextBtn').fire('click');
    assert.equal(h.get('onboardingStepBadge').textContent, 'Step 2 of 2');
    await h.get('onboardNextBtn').fire('click');
    assert.equal(h.dialog.open, false);
});

test('sign-in writes are made only after the existing human sign-in button is selected', async () => {
    const h = harness({imported: false});
    await h.window.openSetupWizard();
    assert.deepEqual(h.requests, []);
    h.responder(async url => url === '/api/msx/az-login/start'
        ? {error: 'Azure CLI not installed'} : {success: true});
    await h.get('startSignInBtn').fire('click');
    assert.deepEqual(h.requests.map(request => [request.url, request.method]),
        [['/api/msx/az-logout', 'POST'], ['/api/msx/az-login/start', 'POST']]);
    assert.equal(h.get('signInNoCli').classes.has('sb-setup-hidden'), false);
    assert.equal(h.get('onboardNextBtn').disabled, false);
});

test('an active sign-in guards review closing and navigation until its error resolves', async () => {
    const h = harness({imported: false}); await h.window.openSetupWizard();
    let finish;
    h.responder(url => url === '/api/msx/az-logout'
        ? new Promise(resolve => { finish = resolve; })
        : {error: 'Azure CLI not installed'});
    const pending = h.get('startSignInBtn').fire('click');
    assert.equal(h.get('onboardNextBtn').disabled, true);
    await h.get('setupReviewClose').fire('click');
    h.window.SBDialog.close(h.dialog, 'escape');
    assert.equal(h.dialog.open, true);
    assert.match(h.get('setupReviewStatus').textContent, /active sign-in or import/);
    finish({success: true}); await pending;
    assert.equal(h.get('onboardNextBtn').disabled, false);
    await h.get('setupReviewClose').fire('click');
    assert.equal(h.dialog.open, false);
});

function streamResponse(chunks, beforeRead = () => {}) {
    const cleanup = {cancelled: 0, released: 0};
    let index = 0;
    const reader = {
        async read() {
            beforeRead(index);
            if (index === chunks.length) return {done: true};
            const chunk = chunks[index++];
            if (chunk instanceof Error) throw chunk;
            return {done: false, value: new TextEncoder().encode(chunk)};
        },
        async cancel() { cleanup.cancelled++; },
        releaseLock() { cleanup.released++; },
    };
    return {ok: true, status: 200, headers: new Headers({'Content-Type': 'text/event-stream'}),
        body: {getReader: () => reader}, cleanup};
}

function event(type, data) {
    return `${type ? `event: ${type}\n` : ''}data: ${JSON.stringify(data)}\n\n`;
}

async function firstRunSeSetup(options = {}) {
    const h = harness({
        autoOpen: true, buckets: ['Databases', 'Fabric'], selected: ['Databases'], ...options,
    });
    await h.ready();
    return h;
}

async function chooseSpecialty(h, specialty = 'infra') {
    h.get('onboardSeSpecialty').value = specialty;
    await h.get('onboardSeSpecialty').fire('change');
}

test('Finish enables after SE selections and saves annual setup before dismissing', async () => {
    const h = await firstRunSeSetup();
    assert.equal(h.get('onboardSeSpecialty').value, '');
    assert.equal(h.get('onboardNextBtn').disabled, true);
    assert.equal(h.get('onboardNextReason').textContent, 'Choose an SE specialty.');
    assert.equal(h.get('onboardNextAction').getAttribute('tabindex'), '0');
    assert.ok(h.requests.every(request => request.method === 'GET'));
    await chooseSpecialty(h, 'other');
    assert.equal(h.get('onboardNextBtn').disabled, false);
    assert.equal(h.get('onboardNextReason').textContent, '');
    assert.equal(h.get('onboardNextAction').getAttribute('tabindex'), '-1');
    assert.ok(h.requests.every(request => request.method === 'GET'));
    await h.get('onboardNextBtn').fire('click');
    assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => [
        r.url, r.body ? JSON.parse(r.body) : null,
    ]), [
        ['/api/preferences/se-specialty', {se_specialty: 'other'}],
        ['/api/revenue/compensated-buckets',
            {buckets: ['Databases'], confirm_for_fiscal_year: true}],
        ['/api/preferences/dismiss-welcome-modal', null],
    ]);
    assert.match(h.get('onboardSeSetupStatus').textContent, /saved for FY27/);
    assert.equal(h.get('onboardNextBtn').disabled, false);
    assert.deepEqual(h.saved, [
        {key: 'salesbuddy_revenue_bucket_filter', value: '["Databases"]'},
        {key: 'salesbuddy_revenue_bucket_taxonomy_version', value: '4'},
    ]);
    assert.equal(h.window.reloaded, true);
    assert.equal(h.dialog.open, false);
    assert.ok(h.requests.some(r => r.url === '/api/preferences/dismiss-welcome-modal'));
});

test('bucket edits remain drafts until Finish without racing per-checkbox writes', async () => {
    const h = await firstRunSeSetup();
    await chooseSpecialty(h);
    const inputs = h.get('onboardBucketList').querySelectorAll('input');
    const databases = inputs.find(input => input.value === 'Databases');
    databases.checked = false;
    await databases.fire('change');
    assert.equal(h.get('onboardNextBtn').disabled, true);
    assert.equal(h.get('onboardNextReason').textContent, 'Choose at least one compensation bucket.');
    const fabric = inputs.find(input => input.value === 'Fabric');
    fabric.checked = true;
    await fabric.fire('change');
    assert.equal(h.get('onboardNextBtn').disabled, false);
    assert.ok(h.requests.every(r => r.method === 'GET'));
    await h.get('onboardNextBtn').fire('click');
    const confirmation = h.requests.find(r =>
        r.url === '/api/revenue/compensated-buckets' && r.method === 'POST');
    assert.deepEqual(JSON.parse(confirmation.body), {
        buckets: ['Fabric'], confirm_for_fiscal_year: true,
    });
    assert.equal(h.get('onboardNextBtn').disabled, false);
});

test('browser storage failure warns without discarding successful server confirmation', async () => {
    const h = await firstRunSeSetup({storageError: true});
    await chooseSpecialty(h);
    await h.get('onboardNextBtn').fire('click');
    assert.match(h.get('onboardSeSetupStatus').textContent, /saved, but this browser/);
    assert.equal(h.get('onboardNextBtn').disabled, false);
    assert.ok(h.errors.length);
    assert.deepEqual(h.saved, []);
    assert.equal(h.window.reloaded, true);
});

for (const failure of ['specialty-http', 'specialty-mismatch', 'bucket-network',
    'bucket-http', 'missing-year', 'missing-taxonomy']) {
    test(`SE confirmation failure (${failure}) stays visible and retryable without finishing`, async () => {
        const h = await firstRunSeSetup();
        await chooseSpecialty(h);
        h.responder((url, options) => {
            const httpError = {headers: {}, body: {}, ok: false,
                json: async () => ({error: 'Save unavailable'})};
            if (url === '/api/preferences/se-specialty') {
                if (failure === 'specialty-http') return httpError;
                if (failure === 'specialty-mismatch') return {se_specialty: 'data'};
            }
            if (url === '/api/revenue/compensated-buckets' && options.method === 'POST') {
                if (failure === 'bucket-network') throw new Error('Network unavailable');
                if (failure === 'bucket-http') return httpError;
                if (failure === 'missing-year') return {success: true, taxonomy_version: 4};
                if (failure === 'missing-taxonomy') return {success: true, fiscal_year: 'FY27'};
            }
            return h.defaultResponse(url, options);
        });
        await h.get('onboardNextBtn').fire('click');
        assert.equal(h.get('onboardSeSetupStatus').getAttribute('role'), 'alert');
        assert.match(h.get('onboardSeSetupStatus').textContent, /Could not save SE setup/);
        assert.equal(h.get('onboardNextBtn').disabled, false);
        assert.equal(h.dialog.open, true);
        assert.equal(h.window.reloaded, undefined);
        assert.ok(!h.requests.some(r => r.url === '/api/preferences/dismiss-welcome-modal'));
        assert.deepEqual(h.saved, []);
        h.responder(h.defaultResponse);
        await h.get('onboardNextBtn').fire('click');
        assert.equal(h.get('onboardNextBtn').disabled, false);
        assert.equal(h.dialog.open, false);
    });
}

test('pending setup save blocks duplicate writes, navigation and review closing', async () => {
    const h = harness({buckets: ['Fabric'], selected: ['Fabric']});
    await h.window.openSetupWizard();
    await chooseSpecialty(h, 'other');
    let finish;
    h.responder((url, options) => url === '/api/preferences/se-specialty'
        ? new Promise(resolve => { finish = resolve; }) : h.defaultResponse(url, options));
    const pending = h.get('onboardNextBtn').fire('click');
    await h.get('onboardNextBtn').fire('click');
    assert.equal(h.get('onboardNextBtn').disabled, true);
    assert.equal(h.get('onboardBackBtn').disabled, true);
    assert.equal(h.get('onboardSeSpecialty').disabled, true);
    assert.match(h.get('onboardNextReason').textContent, /Saving your setup/);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1);
    await h.get('setupReviewClose').fire('click');
    h.window.SBDialog.close(h.dialog, 'escape');
    assert.equal(h.dialog.open, true);
    finish({se_specialty: 'other'});
    await pending;
    assert.equal(h.get('onboardNextBtn').disabled, false);
    assert.equal(h.dialog.open, false);
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 2);
});

test('bucket load failures offer retry rather than silently accepting missing setup', async () => {
    const h = harness({buckets: ['Fabric'], selected: ['Fabric']});
    h.responder(url => {
        if (url === '/api/revenue/buckets') throw new Error('Read failed');
        return h.defaultResponse(url);
    });
    await h.window.openSetupWizard();
    assert.equal(h.get('onboardReloadBuckets').classes.has('sb-setup-hidden'), false);
    assert.equal(h.get('onboardSeSetupStatus').getAttribute('role'), 'alert');
    assert.equal(h.get('onboardNextBtn').disabled, true);
    assert.match(h.get('onboardNextReason').textContent, /Retry loading/);
    h.responder(h.defaultResponse);
    await h.get('onboardReloadBuckets').fire('click');
    assert.equal(h.get('onboardReloadBuckets').classes.has('sb-setup-hidden'), true);
    assert.equal(h.get('onboardNextBtn').disabled, false);
    assert.ok(h.requests.every(r => r.method === 'GET'));
});

test('an SE must retry failed revenue before selecting buckets and finishing', async () => {
    const h = await firstRunSeSetup({
        imported: {accounts: true, milestones: true, revenue: false},
    });
    h.responder((url, options) => url === '/api/revenue/sync'
        ? streamResponse([event('error', {message: 'Revenue import unavailable'})])
        : h.defaultResponse(url, options));
    await h.get('phaseRevenueRetry').fire('click');
    assert.equal(h.get('onboardBucketPickerCard').classes.has('sb-setup-hidden'), false);
    await chooseSpecialty(h, 'other');
    assert.equal(h.get('onboardNextBtn').disabled, true);
    assert.match(h.get('onboardNextReason').textContent, /No compensation buckets.*Retry/);
    await h.get('onboardNextBtn').fire('click');
    assert.equal(h.dialog.open, true);
    assert.ok(!h.requests.some(r =>
        r.url === '/api/revenue/compensated-buckets' && r.method === 'POST'));
    h.responder((url, options) => url === '/api/revenue/sync'
        ? streamResponse([event('complete', {success: true})])
        : h.defaultResponse(url, options));
    await h.get('phaseRevenueRetry').fire('click');
    assert.equal(h.get('onboardNextBtn').disabled, false);
    await h.get('onboardNextBtn').fire('click');
    assert.equal(h.dialog.open, false);
});

test('missing both SE choices explains disabled Finish and prevents programmatic dismissal', async () => {
    const h = await firstRunSeSetup({selected: []});
    assert.equal(h.get('onboardNextReason').textContent,
        'Choose an SE specialty and at least one compensation bucket.');
    await h.get('onboardNextBtn').fire('click');
    h.window.SBDialog.close(h.dialog, 'finished');
    assert.equal(h.dialog.open, true);
    assert.ok(h.requests.every(r => r.method === 'GET'));
});

test('empty successful revenue cannot silently bypass the required bucket choice', async () => {
    const h = await firstRunSeSetup({buckets: [], selected: []});
    assert.match(h.get('onboardSeSetupStatus').textContent, /Retry the revenue import/);
    await chooseSpecialty(h);
    assert.equal(h.get('onboardNextBtn').disabled, true);
    assert.match(h.get('onboardNextReason').textContent, /No compensation buckets/);
    await h.get('onboardNextBtn').fire('click');
    assert.equal(h.dialog.open, true);
    assert.ok(h.requests.every(r => r.method === 'GET'));
});

for (const action of ['footer', 'header', 'escape', 'api']) {
    test(`review ${action} cannot bypass missing choices and saves valid edits on exit`, async () => {
        const h = harness();
        await h.window.openSetupWizard();
        await chooseSpecialty(h, 'other');
        const fabric = h.get('onboardBucketList').querySelectorAll('input')
            .find(input => input.value === 'Fabric');
        fabric.checked = false;
        await fabric.fire('change');
        assert.equal(h.get('onboardNextBtn').disabled, true);
        assert.equal(h.get('setupReviewClose').disabled, true);
        const close = async () => {
            if (action === 'footer') await h.get('onboardNextBtn').fire('click');
            else if (action === 'header') await h.get('setupReviewClose').fire('click');
            else {
                h.window.SBDialog.close(h.dialog, action);
                await h.ready();
            }
        };
        await close();
        assert.equal(h.dialog.open, true);
        assert.ok(h.requests.every(r => r.method === 'GET'));
        if (action !== 'footer') {
            await h.get('onboardBackBtn').fire('click');
            await close();
            assert.equal(h.dialog.open, true);
            await h.get('onboardNextBtn').fire('click');
        }
        fabric.checked = true;
        await fabric.fire('change');
        assert.equal(h.get('onboardNextBtn').disabled, false);
        assert.equal(h.get('setupReviewClose').disabled, false);
        await close();
        assert.equal(h.dialog.open, false);
        assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => r.url), [
            '/api/preferences/se-specialty', '/api/revenue/compensated-buckets',
        ]);
        assert.equal(h.window.reloaded, undefined);
        assert.match(h.get('onboardSeSetupStatus').textContent, /saved for FY27/);
    });
}

for (const failure of ['network', 'http', 'unconfirmed']) {
    test(`failed setup dismissal (${failure}) keeps the dialog open and retries without resaving`, async () => {
        const h = await firstRunSeSetup();
        await chooseSpecialty(h);
        h.responder((url, options) => {
            if (url === '/api/preferences/dismiss-welcome-modal') {
                if (failure === 'network') throw new Error('Offline');
                if (failure === 'http') return {headers: {}, body: {}, ok: false,
                    json: async () => ({error: 'Write failed'})};
                return {};
            }
            return h.defaultResponse(url, options);
        });
        await h.get('onboardNextBtn').fire('click');
        assert.equal(h.dialog.open, true);
        assert.equal(h.window.reloaded, undefined);
        assert.match(h.get('setupReviewStatus').textContent, /Could not finish setup/);
        assert.equal(h.get('onboardNextBtn').disabled, false);
        h.responder(h.defaultResponse);
        await h.get('onboardNextBtn').fire('click');
        assert.equal(h.dialog.open, false);
        assert.equal(h.window.reloaded, true);
        assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => r.url), [
            '/api/preferences/se-specialty', '/api/revenue/compensated-buckets',
            '/api/preferences/dismiss-welcome-modal', '/api/preferences/dismiss-welcome-modal',
        ]);
    });
}

for (const specialty of ['data', 'infra', 'apps_ai', 'other']) {
    test(`seller skips SE setup with saved ${specialty} specialty`, async () => {
        const h = await firstRunSeSetup({role: 'dss', specialty});
        assert.equal(h.get('onboardBucketPickerCard').classes.has('sb-setup-hidden'), true);
        assert.equal(h.get('onboardNextBtn').disabled, false);
        assert.ok(!h.requests.some(r => /buckets|specialty/.test(r.url)));
    });
}

test('first account sync detecting a seller hides SE setup without a reload', async () => {
    const h = harness({imported: false});
    h.responder((url, options) => {
        if (url === '/api/preferences/match-seller') return {matched: true, role_set: 'dss'};
        if (['/api/msx/accounts/sync', '/api/milestone-tracker/sync', '/api/revenue/sync']
            .includes(url)) return streamResponse([event('complete', {success: true})]);
        return h.defaultResponse(url, options);
    });
    await h.get('onboardStartImport').fire('click');
    assert.equal(h.get('onboardBucketPickerCard').classes.has('sb-setup-hidden'), true);
    assert.ok(!h.requests.some(r => /buckets|specialty/.test(r.url)));
});

for (const first of ['Milestones', 'Revenue']) {
    for (const fails of [false, true]) {
        test(`${first} ${fails ? 'failing' : 'finishing'} first waits for its parallel peer before scrolling`, async () => {
            const h = harness({imported: false});
            await h.window.openSetupWizard();
            await h.get('onboardNextBtn').fire('click');
            const pending = new Map();
            h.responder((url, options) => phaseCases.some(phase => phase.endpoint === url)
                ? new Promise(resolve => pending.set(url, resolve))
                : h.defaultResponse(url, options));
            const running = h.get('onboardStartImport').fire('click');
            await h.ready();
            assert.deepEqual([...pending.keys()], ['/api/msx/accounts/sync']);
            pending.get('/api/msx/accounts/sync')(
                streamResponse([event('complete', {success: true})]));
            await h.ready();
            assert.ok(pending.has('/api/milestone-tracker/sync'));
            assert.ok(pending.has('/api/revenue/sync'));
            const firstPhase = phaseCases.find(phase => phase.name === first);
            const secondPhase = phaseCases.find(phase =>
                phase.name !== 'Accounts' && phase.name !== first);
            pending.get(firstPhase.endpoint)(streamResponse([
                event(fails ? 'error' : 'complete', fails
                    ? {message: 'Source unavailable'} : firstPhase.terminal),
            ]));
            await h.ready();
            assert.equal(h.get('onboardingBody').scrollTop, 0);
            h.requests.length = 0;
            assert.equal(h.get('onboardNextBtn').disabled, true);
            assert.equal(h.get('onboardBucketPickerCard').classes.has('sb-setup-hidden'), true);
            await h.get('setupReviewClose').fire('click');
            assert.equal(h.dialog.open, true);
            pending.get(secondPhase.endpoint)(
                streamResponse([event('complete', secondPhase.terminal)]));
            await running;
            assert.equal(h.get('onboardBucketPickerCard').classes.has('sb-setup-hidden'), false);
            assert.equal(h.get('onboardingBody').scrollTop, h.get('onboardingBody').scrollHeight);
            assert.match(h.get(`phase${first}Status`).textContent,
                fails ? /Source unavailable/ : /Done in 2s/);
            assert.match(h.get(`phase${secondPhase.name}Status`).textContent, /Done in 2s/);
        });
    }
}

test('retrying a missing phase scrolls to selections without repeating finished syncs', async () => {
    const h = harness({autoOpen: true,
        imported: {accounts: true, milestones: false, revenue: true}});
    await h.ready();
    assert.equal(h.get('onboardingBody').scrollTop, 0);
    h.responder((url, options) => url === '/api/milestone-tracker/sync'
        ? streamResponse([event('complete', {success: true})])
        : h.defaultResponse(url, options));
    await h.get('phaseMilestonesRetry').fire('click');
    assert.equal(h.get('onboardingBody').scrollTop, h.get('onboardingBody').scrollHeight);
    assert.deepEqual(h.requests.filter(r => phaseCases.some(p => p.endpoint === r.url))
        .map(r => r.url),
        ['/api/milestone-tracker/sync']);
});

test('resuming completed first-run imports reveals the required selections at the bottom', async () => {
    const h = await firstRunSeSetup();
    assert.equal(h.get('onboardingBody').scrollTop, h.get('onboardingBody').scrollHeight);
    assert.equal(h.get('onboardSeSpecialty').value, '');
    assert.equal(h.get('onboardNextBtn').disabled, true);
});

const phaseCases = [
    {name: 'Accounts', endpoint: '/api/msx/accounts/sync', button: 'phaseAccountsRetry',
        terminal: {complete: true, summary: {customers_created: 3, duration: 2}}},
    {name: 'Milestones', endpoint: '/api/milestone-tracker/sync', button: 'phaseMilestonesRetry',
        terminal: {success: true, synced: 3, created: 4, duration: 2}},
    {name: 'Revenue', endpoint: '/api/revenue/sync', button: 'phaseRevenueRetry',
        terminal: {success: true, customers_with_data: 3, customers: 5, months: 6, duration_s: 2}},
];

for (const phase of phaseCases) {
    test(`${phase.name}: admission, queue, domain progress and terminal preserve setup results`, async () => {
        const h = harness({imported: phase.name !== 'Accounts'});
        const observed = [];
        const frames = [
            event('accepted', {job_id: 42}),
            event('queued', {message: 'Waiting for another sync'}),
            event('start', {message: 'Worker started', total: 5}),
            event('progress', {message: 'Fetching records', customer: 'Fetching records',
                progress: 40, current: 2, total: 5}),
            event(phase.name === 'Accounts' ? '' : 'complete', phase.terminal),
        ];
        // Split across data, event and CRLF boundaries, not just whole SSE frames.
        const wire = frames.join('').replace(/\n/g, '\r\n');
        const response = streamResponse([...wire].map(char => char), () => {
            observed.push(h.get(`phase${phase.name}Status`).textContent);
        });
        h.responder(async url => {
            if (url === phase.endpoint) return response;
            if (url === '/api/milestone-tracker/sync' || url === '/api/revenue/sync') {
                return streamResponse([event('complete', {success: true})]);
            }
            if (url === '/api/revenue/buckets') return [];
            return {success: true};
        });
        await h.get(phase.button).fire('click');
        assert.ok(observed.includes('Sync accepted, waiting for worker...'));
        assert.ok(observed.includes('Waiting for another sync'));
        assert.ok(observed.includes('Worker started'));
        assert.ok(observed.includes('Fetching records'));
        assert.equal(h.get(`phase${phase.name}Bar`).style.width, '40%');
        assert.equal(h.get(`phase${phase.name}Status`).textContent, 'Done in 2s');
        assert.match(h.get(`phase${phase.name}Summary`).innerHTML, /3/);
        assert.deepEqual(response.cleanup, {cancelled: 1, released: 1});
        assert.ok(!h.requests.some(request => request.url === '/api/msx/sync-marketing'));
        if (phase.name === 'Accounts') {
            assert.equal(h.requests.filter(request =>
                request.url === '/api/admin/fetch-favicons').length, 1);
            assert.ok(h.requests.find(request => request.url === '/api/preferences/match-seller'));
            const syncRequests = h.requests.filter(request => phaseCases.some(p =>
                p.endpoint === request.url)).map(request => request.url);
            assert.equal(syncRequests[0], phaseCases[0].endpoint);
            assert.deepEqual(new Set(syncRequests.slice(1)),
                new Set(phaseCases.slice(1).map(phase => phase.endpoint)));
        }
    });

    for (const failure of [
        {name: 'truncated stream', chunks: [event('accepted', {job_id: 42}),
            event('progress', {progress: 100, message: 'Saving'}), 'event: complete\ndata: {"success":'],
            message: /JSON/},
        {name: 'EOF without terminal', chunks: [event('accepted', {job_id: 42})],
            message: /before completion/},
        {name: 'terminal error', chunks: [event('error', {message: 'Worker failed'})],
            message: /Worker failed/},
        {name: 'unsuccessful completion', chunks: [event('complete',
            {success: false, message: 'Partial import'})], message: /Partial import/},
        {name: 'unconfirmed completion', chunks: [event('complete', {})],
            message: /could not be confirmed/},
        {name: 'network failure', chunks: [new Error('Connection lost')], message: /Connection lost/},
    ]) {
        test(`${phase.name}: ${failure.name} cannot finish setup or admit follow-ups`, async () => {
            const h = harness({imported: false});
            const response = streamResponse(failure.chunks);
            h.responder(async url => url === phase.endpoint ? response : {success: true});
            await h.get(phase.button).fire('click');
            assert.match(h.get(`phase${phase.name}Status`).textContent, failure.message);
            assert.equal(h.get(`phase${phase.name}Status`).classes.has('sb-setup-danger'), true);
            assert.equal(h.get(`phase${phase.name}Summary`).innerHTML, '');
            assert.deepEqual(response.cleanup, {cancelled: 1, released: 1});
            assert.ok(!h.requests.some(request => ['/api/admin/fetch-favicons',
                '/api/preferences/match-seller', '/api/msx/sync-marketing'].includes(request.url)));
            if (phase.name === 'Accounts') {
                assert.ok(!h.requests.some(request => phaseCases.slice(1)
                    .some(p => p.endpoint === request.url)));
            }
        });
    }

    test(`${phase.name}: resolved JSON job result works without parsing an SSE body`, async () => {
        const h = harness({imported: false});
        const result = {success: true, ...phase.terminal};
        h.window.SBJobs = {message: job => job.progress.message};
        h.responder(async (url, options) => {
            if (url === phase.endpoint) {
                options.onJobProgress({status: 'running',
                    progress: {percent: 35, message: 'Observer progress'}});
                assert.equal(h.get(`phase${phase.name}Bar`).style.width, '35%');
                assert.equal(h.get(`phase${phase.name}Status`).textContent, 'Observer progress');
                return new Response(JSON.stringify(result),
                    {headers: {'Content-Type': 'application/json'}});
            }
            if (phaseCases.some(p => p.endpoint === url)) {
                return streamResponse([event('complete', {success: true})]);
            }
            return url === '/api/revenue/buckets' ? [] : {success: true};
        });
        await h.get(phase.button).fire('click');
        assert.equal(h.get(`phase${phase.name}Status`).textContent, 'Done in 2s');
    });

    test(`${phase.name}: unresolved JSON admission is not a successful import`, async () => {
        const h = harness({autoOpen: true, imported: false});
        h.responder(async url => url === phase.endpoint
            ? new Response(JSON.stringify({accepted: true, job_id: 42}), {
                status: 202, headers: {'Content-Type': 'application/json'},
            }) : {success: true});
        await h.get(phase.button).fire('click');
        assert.match(h.get(`phase${phase.name}Status`).textContent, /completion could not be confirmed/);
        assert.equal(h.get('onboardNextBtn').disabled, true);
    });
}

test('milestone customer errors stay progress while a successful terminal reports partial counts', async () => {
    const h = harness();
    const response = streamResponse([
        event('progress', {current: 1, total: 2, customer: 'Account A', status: 'error',
            error: 'Customer unavailable'}),
        event('complete', {success: true, synced: 1, failed: 1}),
    ]);
    h.responder(async url => url === '/api/milestone-tracker/sync' ? response : {success: true});
    await h.get('phaseMilestonesRetry').fire('click');
    assert.equal(h.get('phaseMilestonesStatus').textContent, 'Done');
    assert.match(h.get('phaseMilestonesSummary').innerHTML, /1 failed/);
});

test('revenue accepts a fully decoded final terminal frame at EOF', async () => {
    const h = harness();
    const response = streamResponse(['event: complete\ndata: {"success":true,"months":12}']);
    h.responder(async url => url === '/api/revenue/sync' ? response : []);
    await h.get('phaseRevenueRetry').fire('click');
    assert.equal(h.get('phaseRevenueStatus').textContent, 'Done');
    assert.match(h.get('phaseRevenueSummary').innerHTML, /12 months/);
    assert.deepEqual(response.cleanup, {cancelled: 1, released: 1});
});

test('VPN terminal errors show the existing VPN banner and free the reader', async () => {
    const h = harness();
    let banners = 0;
    h.window.showVpnBanner = () => { banners++; };
    const response = streamResponse([event('vpn_blocked', {message: 'Connect to VPN'})]);
    h.responder(async url => url === '/api/milestone-tracker/sync' ? response : {success: true});
    await h.get('phaseMilestonesRetry').fire('click');
    assert.equal(banners, 1);
    assert.match(h.get('phaseMilestonesStatus').textContent, /Connect to VPN/);
    assert.deepEqual(response.cleanup, {cancelled: 1, released: 1});
});

test('generic worker start has no made-up customer total and event names reset for each frame', async () => {
    const h = harness();
    const observed = [];
    const response = streamResponse([
        event('accepted', {job_id: 42}),
        event('start', {phase: 'starting'}),
        event('progress', {message: 'Worker progress'}),
        event('', {message: 'Loading milestones'}),
        event('complete', {success: true}),
    ], () => observed.push(h.get('phaseMilestonesStatus').textContent));
    h.responder(async url => url === '/api/milestone-tracker/sync' ? response : {success: true});
    await h.get('phaseMilestonesRetry').fire('click');
    assert.ok(observed.includes('Worker started this sync...'));
    assert.ok(observed.includes('Worker progress'));
    assert.ok(observed.includes('Loading milestones'));
    assert.ok(!observed.some(label => /undefined|Syncing 0 customers/.test(label)));
});
