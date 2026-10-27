const assert = require('node:assert/strict');
const test = require('node:test');
const {productLines} = require('../../static/js/ui-revenue-breakdown.js');

test('product evidence displays whole dollars and credits without altering input precision', () => {
    const products = [
        {product: '<SQL & storage>', monthly_revenue: 12345.6789},
        {product: 'Credit', monthly_revenue: -1.234},
        {product: 'Small footprint', monthly_revenue: 0.001},
        {product: 'Rounding boundary', monthly_revenue: 999.9},
    ];
    const before = JSON.stringify(products);
    assert.deepEqual(productLines(products), [
        {name: '<SQL & storage>', amount: '$12,346'},
        {name: 'Credit', amount: '$-1'},
        {name: 'Small footprint', amount: '$0'},
        {name: 'Rounding boundary', amount: '$1,000'},
    ]);
    assert.equal(JSON.stringify(products), before);
    assert.deepEqual(productLines([]), []);
});

test('invalid product evidence raises instead of pretending it is zero spend', () => {
    for (const products of [null, {}, [{product: 3, monthly_revenue: 1}],
        [{product: 'SQL', monthly_revenue: '5'}], [{product: 'SQL', monthly_revenue: Infinity}]]) {
        assert.throws(() => productLines(products), /Invalid revenue product breakdown/);
    }
});
