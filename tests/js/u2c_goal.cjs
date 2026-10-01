const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const scripts = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
for (const id of [
    'u2cTrendChart', 'trendEmpty', 'trendScope', 'workloadFilter', 'filterSeller',
    'filterCustomer', 'cardU2cPct', 'cardU2cPctBar',
]) {
    elements[id] = {
        value: '', style: {}, className: '', textContent: '',
        classList: {toggle() {}},
    };
}
let remaining = [];
let committed = [];
let chart;
const context = vm.createContext({
    document: {
        body: {},
        getElementById: id => elements[id] || null,
        addEventListener() {},
        querySelectorAll: selector => {
            if (selector === '#remainingBody tr[data-workload]') return remaining;
            if (selector === '#committedBody tr[data-workload]') return committed;
            return [];
        },
    },
    localStorage: {setItem() {}, removeItem() {}},
    getComputedStyle: () => ({getPropertyValue: () => ''}),
    Chart: class {
        constructor(canvas, config) {
            this.data = config.data;
            this.options = config.options;
            chart = this;
        }
        update() {}
        destroy() {}
    },
});
scripts.forEach(script => vm.runInContext(script, context));
elements.workloadFilter.value = 'Data';
vm.runInContext("renderU2cTrend('Data')", context);
assert.equal(chart.data.datasets.length, 3);
assert.equal(chart.data.datasets[1].label, 'Total pipeline (starting uncommitted)');
assert.equal(chart.data.datasets[2].label, '40% goal');
assert.equal(chart.data.datasets[2].data.at(-1).y, 6151.2);
assert.ok(chart.data.datasets[0].data.at(-1).y > chart.data.datasets[2].data.at(-1).y);
const tooltip = chart.options.plugins.tooltip.callbacks.label;
const lastIndex = chart.data.datasets[0].data.length - 1;
assert.equal(tooltip({datasetIndex: 1, dataIndex: lastIndex}), ' Total pipeline $15,378');
assert.equal(tooltip({datasetIndex: 2, dataIndex: lastIndex}), ' 40% goal $6,151');
for (const prefix of ['Infra', '']) {
    elements.workloadFilter.value = prefix;
    vm.runInContext(`renderU2cTrend('${prefix}')`, context);
    chart.data.datasets[1].data.forEach((point, index) => {
        assert.equal(chart.data.datasets[2].data[index].y, point.y * 40 / 100);
    });
}
function row(acr) {
    return {style: {}, dataset: {workload: 'Data: Fabric', acr: String(acr), liveAcr: '0'}};
}
elements.workloadFilter.value = 'Data';
for (const percentage of [0, 39.9, 40, 48, 100]) {
    remaining = [row(100 - percentage)];
    committed = [row(percentage)];
    vm.runInContext('applyAllFilters()', context);
    const tone = percentage >= 40 ? 'success' : 'warning';
    assert.equal(elements.cardU2cPct.textContent, `${percentage}%`);
    assert.equal(elements.cardU2cPct.className, `mb-1 text-${tone}`);
    assert.equal(elements.cardU2cPctBar.className, `progress-bar bg-${tone}`);
}
