// Lightweight JavaScript integration test; no browser/network/notifications.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const page = fs.readFileSync('index.html','utf8');
for (const match of page.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const sample = JSON.parse(fs.readFileSync('news.json','utf8'));
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id,{value:'',innerHTML:'',textContent:'',options:[],handlers:{},addEventListener(type,fn){this.handlers[type]=fn;}});
  return nodes.get(id);
}
const context = {document:{getElementById:element},URL,Date,Number,String,Intl,console,
  window:{addEventListener(){}},setInterval(){},stock:()=>null,
  fetch:async()=>({ok:true,json:async()=>sample})};
vm.runInNewContext(fs.readFileSync('news.js','utf8'),context);
setImmediate(()=>{
  assert.match(element('newsCount').textContent,/件/);
  assert.match(element('newsRows').innerHTML,/news-card/);
  element('newsSearch').value='not-a-real-news-query-xyz';
  element('newsSearch').handlers.input();
  assert.match(element('newsRows').innerHTML,/条件に一致する記事はありません/);
  element('newsSearch').value='';
  element('newsOfficial').value='official';
  element('newsOfficial').handlers.change();
  assert.ok(!element('newsRows').innerHTML.includes('報道・その他'));
  sample.articles[0].title='<img src=x onerror=alert(1)>';
  sample.articles[0].url='javascript:alert(1)';
  element('newsOfficial').value='';
  element('newsOfficial').handlers.change();
  assert.ok(!element('newsRows').innerHTML.includes('<img'));
  assert.ok(!element('newsRows').innerHTML.includes('href="javascript:'));
  console.log('UI integration: render, search, official filter, escaping, safe links OK');
});
