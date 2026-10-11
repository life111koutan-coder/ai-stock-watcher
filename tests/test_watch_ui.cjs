const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const start = html.indexOf('function renderMarketScan(){');
const end = html.indexOf('function filterListed()', start);
const elements = {};
const context = {
  document: {getElementById(id) {return elements[id] ||= {style: {}};}},
  market: [{code: '1111', name: '監視銘柄だけ', score: 99}],
  marketScan: {
    generated_at: '2026-10-09T06:00:00Z', cycle_date: '2026-10-09',
    universe_count: 4434, covered_count: 241, cycle_scanned_count: 250,
    progress_pct: 5.6, cycle_complete: false, rankings: [
      {code: '2222', name: '全銘柄ランキング対象', score: 80, tag: '強気', price: 1000}
    ]
  },
  stock: () => undefined, esc: String, yen: {format: String}, tagClass: () => 'strong'
};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);
context.renderMarketScan();
assert.match(elements.rankingRows.innerHTML, /全銘柄ランキング対象/);
assert.doesNotMatch(elements.rankingRows.innerHTML, /監視銘柄だけ/);
assert.match(elements.scanStatus.textContent, /2026-10-09.*暫定/);
context.marketScan.rankings = [];
context.renderMarketScan();
assert.doesNotMatch(elements.rankingRows.innerHTML, /監視銘柄だけ/);
context.marketScan.cycle_complete = true;
context.renderMarketScan();
assert.match(elements.scanStatus.textContent, /2026-10-09.*完了/);
console.log('Stock ranking UI checks passed');
