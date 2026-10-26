const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname,
    '../../templates/partials/_report_export.html'), 'utf8');

async function copyFixture(failHtml = false, customTitle = 'Hygiene', grouped = false,
    warning = '') {
    const cell = label => ({
        hasAttribute: key => key === 'data-export-label',
        getAttribute: key => key === 'data-export-label' ? label : null,
        querySelector: () => null,
    });
    const row = (values, display = '', groupLabel = null) => ({style: {display}, offsetParent: {},
        getAttribute: () => groupLabel,
        querySelectorAll: () => values.map(cell), closest: () => null});
    const table = (heading, rows, dialog = false) => ({
        querySelector: () => null,
        closest: () => dialog ? {} : null,
        querySelectorAll: selector => selector === 'thead th' ?
            [cell(heading), cell('Reason / Notes')] : rows,
    });
    const tables = [
        table('Engagement', [
            ...(grouped ? [row([], '', 'Seller: SQL <owner>')] : []),
            row(['SQL Migration', 'Confirmed <reason>']),
        ]),
        table('Milestone (Seller: Rick Bowles)', [
            row(['SQLMI migration', '']), row(['Filtered off-team', ''], 'none'),
        ]),
        table('Dialog choices', [row(['Not report data', ''])], true),
    ];
    const copied = [], fallback = [], downloads = [];
    const toast = {cloneNode() { return this; }, dataset: {}, setAttribute() {},
        querySelector: () => ({}), contains: () => false, remove() {}, style: {}};
    const script = template.match(/<script>([\s\S]*?)<\/script>/)[1]
        .replace(/const TABLE_SELECTOR = .*?;/, "const TABLE_SELECTOR = 'table';")
        .replace(/const FILENAME = .*?;/, "const FILENAME = 'hygiene';")
        .replace(/const CUSTOM_TITLE = .*?;/, 'const CUSTOM_TITLE = ' +
            JSON.stringify(customTitle) + ';')
        .replace(/const EXPORT_WARNING = .*?;/, 'const EXPORT_WARNING = ' +
            JSON.stringify(warning) + ';');
    const window = {};
    vm.runInNewContext(script, {window, Blob,
        document: {title: 'U2C Attainment - Sales Buddy', querySelectorAll: () => tables,
            querySelector: () => ({textContent: 'Welcome to Sales Buddy!'}),
            getElementById: id => id === 'reportExportNoticeTemplate' ?
                {content: {firstElementChild: toast}} : null,
            createElement: () => ({...toast, textContent: '',
                get innerHTML() {
                    return this.html ?? this.textContent.replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
                },
                set innerHTML(value) { this.html = value; },
                click() {},
            }), body: {appendChild() {}, removeChild() {}}},
        navigator: {clipboard: {
            write: async items => { if (failHtml) throw new Error('Unsupported HTML'); copied.push(...items); },
            writeText: async text => fallback.push(text),
        }},
        ClipboardItem: class {constructor(types) { this.types = types; }},
        URL: {createObjectURL: blob => { downloads.push(blob); return 'blob:test'; },
            revokeObjectURL() {}},
        btoa, unescape, encodeURIComponent,
        setTimeout() {}, requestAnimationFrame() {}});
    window.copyReportForEmail();
    for (let i = 0; i < 8; i++) await Promise.resolve();
    return {copied, fallback, downloads, downloadEmail: window.downloadReportEmail,
        exportCSV: window.exportReportCSV};
}
test('grouped report exports retain seller labels in CSV, HTML and plain text', async () => {
    const h = await copyFixture(false, 'SQL Motions', true);
    assert.ok((await h.copied[0].types['text/html'].text()).includes(
        'Seller: SQL &lt;owner&gt;'));
    assert.ok((await h.copied[0].types['text/plain'].text()).includes('Seller: SQL <owner>'));
    h.exportCSV();
    const csv = await h.downloads[0].text();
    assert.ok(csv.includes('Seller: SQL <owner>'));
    assert.ok(csv.includes('SQL Migration'));
    assert.ok(!csv.includes('Filtered off-team'));
});
test('native clipboard exports both queues in HTML and text with seller and confirmed-note context', async () => {
    const h = await copyFixture();
    const html = await h.copied[0].types['text/html'].text();
    const plain = await h.copied[0].types['text/plain'].text();
    for (const text of ['SQL Migration', 'SQLMI migration', 'Seller: Rick Bowles']) {
        assert.ok(html.includes(text)); assert.ok(plain.includes(text));
    }
    assert.ok(html.includes('Confirmed &lt;reason&gt;'));
    assert.ok(plain.includes('Confirmed <reason>'));
    assert.equal((html.match(/<table /g) || []).length, 2);
    for (const text of ['Filtered off-team', 'Not report data']) {
        assert.ok(!html.includes(text)); assert.ok(!plain.includes(text));
    }
});
test('native plaintext clipboard fallback retains every filtered export table', async () => {
    const h = await copyFixture(true);
    assert.equal(h.fallback.length, 1);
    assert.ok(h.fallback[0].includes('SQL Migration'));
    assert.ok(h.fallback[0].includes('SQLMI migration'));
    assert.ok(!h.fallback[0].includes('Filtered off-team'));
});
test('default consumers preserve every table in both clipboard formats', async () => {
    const h = await copyFixture();
    const html = await h.copied[0].types['text/html'].text();
    const plain = await h.copied[0].types['text/plain'].text();
    assert.ok(html.includes('SQLMI migration'));
    assert.ok(plain.includes('SQL Migration'));
    assert.ok(plain.includes('SQLMI migration'));
    assert.doesNotMatch(template, /NATIVE_EXPORT/);
    h.exportCSV();
    assert.ok((await h.downloads[0].text()).startsWith('Engagement,Reason / Notes\r\n'));
});

test('data-quality warnings survive HTML, plaintext, CSV and email attachment exports', async () => {
    const warning = 'SQL Server 2016 data is incorrect. Do not use <counts>, "customer lists".';
    const h = await copyFixture(false, 'SQL 2016 EOS / ESU', true, warning);
    const html = await h.copied[0].types['text/html'].text();
    const plain = await h.copied[0].types['text/plain'].text();
    assert.ok(html.includes('<strong>' + warning.replace('<counts>', '&lt;counts&gt;') + '</strong>'));
    assert.ok(!html.includes('<counts>'));
    assert.ok(plain.startsWith(warning + '\n\n'));
    assert.ok(plain.includes('Seller: SQL <owner>'));
    h.exportCSV();
    const csv = await h.downloads[0].text();
    const firstRow = '"' + warning.replace(/"/g, '""') + '"';
    assert.equal(csv.split('\r\n')[0], firstRow);
    assert.ok(csv.includes('SQL Migration'));
    h.downloadEmail();
    const eml = await h.downloads[1].text();
    const decoded = eml.replace(/=\r\n/g, '').replace(/=([0-9A-F]{2})/g,
        (_, hex) => String.fromCharCode(parseInt(hex, 16)));
    assert.ok(decoded.includes(warning.replace('<counts>', '&lt;counts&gt;')));
    const attachment = eml.match(/Content-Disposition: attachment;[^\r\n]*\r\n\r\n([\s\S]*?)\r\n--/);
    assert.ok(attachment);
    const attachedCsv = Buffer.from(attachment[1].replace(/\s/g, ''), 'base64').toString('utf8');
    assert.ok(attachedCsv.includes(firstRow));
});

test('plaintext clipboard fallback retains the data-quality warning', async () => {
    const warning = 'SQL Server 2016 data is known to be incorrect.';
    const h = await copyFixture(true, 'SQL 2016 EOS / ESU', false, warning);
    assert.ok(h.fallback[0].startsWith(warning + '\n\n'));
    assert.ok(h.fallback[0].includes('SQL Migration'));
});

test('clipboard HTML excludes the hidden welcome heading when no custom title is supplied',
    async () => {
        const h = await copyFixture(false, '');
        const html = await h.copied[0].types['text/html'].text();
        const plain = await h.copied[0].types['text/plain'].text();
        assert.match(html, /U2C Attainment/);
        assert.doesNotMatch(html, /Welcome to Sales Buddy/);
        assert.doesNotMatch(plain, /Welcome to Sales Buddy/);
    });

test('downloaded email uses the report title in subject and body without onboarding copy',
    async () => {
        const h = await copyFixture(false, '');
        h.downloadEmail();
        const eml = await h.downloads[0].text();
        const decoded = eml.replace(/=\r\n/g, '').replace(/=([0-9A-F]{2})/g,
            (_, hex) => String.fromCharCode(parseInt(hex, 16)));
        assert.match(eml, /^Subject: U2C Attainment - /m);
        assert.match(decoded, /margin-bottom: 8px;">U2C Attainment<\/p>/);
        assert.match(eml, /Content-Disposition: attachment; filename="hygiene.csv"/);
        assert.doesNotMatch(decoded, /Welcome to Sales Buddy/);
        assert.match(decoded, /SQL Migration/);
    });
