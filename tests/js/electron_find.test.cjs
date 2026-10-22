const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../electron/preload.js'), 'utf8');
const start = source.indexOf('function positionFindBar(bar)');
const end = source.indexOf('function createFindBar()', start);

test('Find sits below actual title and menu bounds, including desktop scale changes', () => {
    const bounds = {'sb-shell-titlebar': 32, 'sb-shell-menubar': 60};
    const context = {document: {getElementById: id => ({
        getBoundingClientRect: () => ({bottom: bounds[id]}),
    })}};
    vm.runInNewContext(source.slice(start, end), context);
    const bar = {style: {}};
    context.positionFindBar(bar);
    assert.equal(bar.style.top, '72px');
    bounds['sb-shell-titlebar'] = 40;
    bounds['sb-shell-menubar'] = 76;
    context.positionFindBar(bar);
    assert.equal(bar.style.top, '88px');
});

test('Find keeps its normal inset on platforms without injected shell chrome', () => {
    const context = {document: {getElementById: () => null}};
    vm.runInNewContext(source.slice(start, end), context);
    const bar = {style: {}};
    context.positionFindBar(bar);
    assert.equal(bar.style.top, '12px');
});

test('Find repositions before opening and on window changes, outside the drag region', () => {
    const find = source.slice(end, source.indexOf('function mountShellTitleBar()', end));
    assert.match(find, /const show = \(\) => \{\s*positionFindBar\(bar\);\s*bar\.classList\.add/);
    assert.match(find, /window\.addEventListener\('resize', \(\) => positionFindBar\(bar\)\)/);
    assert.match(find, /-webkit-app-region: no-drag;/);
});
