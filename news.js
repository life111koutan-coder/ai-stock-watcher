(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const url = value => { try { const u = new URL(value); return u.protocol === 'https:' && !u.username ? u.href : ''; } catch { return ''; } };
  const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ja-JP', {timeZone:'Asia/Tokyo'}) : '不明';
  let data = null, limit = 20, busy = false;
  function render() {
    if (!data) return;
    const query = $('newsSearch').value.trim().toLowerCase(), theme = $('newsTheme').value, code = $('newsStock').value;
    const items = data.articles.filter(a => (!query || [a.title,a.publisher,...a.related_stocks.map(s=>s.name+' '+s.code)].join(' ').toLowerCase().includes(query)) && (!theme || a.themes.includes(theme)) && (!code || (code==='watched' ? a.related_stocks.length : a.related_stocks.some(s=>s.code===code))) && (!$('newsOfficial').value || a.official));
    const weight = {'重要':2,'注目':1,'参考':0};
    items.sort((a,b)=>($('newsSort').value==='priority' ? (weight[b.priority]||0)-(weight[a.priority]||0) : 0) || Date.parse(b.published_at)-Date.parse(a.published_at));
    $('newsCount').textContent = `${items.length}件・過去7日分／重要度は見出し判定、投資判断ではありません`;
    $('newsRows').innerHTML = items.slice(0,limit).map(a=>{
      const link = url(a.url);
      const related = a.related_stocks.map(s=>{
        const live = typeof stock === 'function' ? stock(s.code) : null;
        const label = `${s.name}（${s.code}）`;
        return live ? `<button type="button" class="row-btn" data-news-code="${escape(s.code)}">${escape(label)} · ${new Intl.NumberFormat('ja-JP',{style:'currency',currency:'JPY',maximumFractionDigits:2}).format(live.price)} · 価格スコア ${escape(live.score)}点</button>` : `<div>${escape(label)} · 価格未取得</div>`;
      }).join('');
      return `<article class="news-card ${a.priority==='重要'?'important':''}"><div class="news-tags"><span class="news-tag ${a.priority==='重要'?'important':''}">${escape(a.priority)}</span><span class="news-tag ${a.official?'official':''}">${a.official?'企業公式':'報道・その他'}</span>${a.themes.map(t=>`<span class="news-tag">${escape(t)}</span>`).join('')}</div><h3>${link?`<a href="${escape(link)}" target="_blank" rel="noopener noreferrer">${escape(a.title)} ↗</a>`:escape(a.title)}</h3><div class="news-meta">${escape(a.publisher)}<br>発表・配信：${escape(date(a.published_at))} JST<br>初回取得：${escape(date(a.first_seen_at))} JST</div><p class="news-summary">${escape(a.summary)}</p><div class="news-meta">${escape(a.signal)}<br>判定根拠：${escape(a.reason)}</div><div class="news-related">${related?'見出しに企業名あり（取引関係・株価影響を保証しません）'+related:'監視銘柄の企業名なし · 業界動向の参考情報'}</div></article>`;
    }).join('') || '<div class="empty">条件に一致する記事はありません。取得状況も確認してください。</div>';
    $('newsMore').hidden = items.length <= limit;
  }
  function status() {
    if (!data) return;
    const age = Date.now()-Date.parse(data.generated_at), stale = !Number.isFinite(age)||age>60*60*1000;
    const failed = data.sources.filter(s=>s.status!=='ok').length;
    $('newsStatus').className = 'news-status'+(stale||failed?' warn':'');
    $('newsStatus').textContent = `${stale?'更新が1時間以上遅れています。 ':''}${failed?`取得失敗 ${failed}/${data.sources.length}件・保存済み記事を含みます。 `:'取得元の巡回完了。 '}最終巡回 ${date(data.generated_at)} JST／最終取得成功 ${date(data.last_success_at)} JST。ページは5分ごとに再読込。`;
  }
  async function load() {
    if (busy) return; busy=true; $('newsReload').disabled=true;
    try {
      const response=await fetch('./news.json?ts='+Date.now(),{cache:'no-store'});
      if(!response.ok) throw new Error('HTTP '+response.status);
      const next=await response.json();
      if(!Array.isArray(next.articles)||!Array.isArray(next.sources)) throw new Error('invalid data');
      data=next;
      const selected=$('newsStock').value;
      $('newsStock').innerHTML='<option value="">すべての記事</option><option value="watched">監視銘柄の名前あり</option>'+(data.watchlist||[]).map(s=>`<option value="${escape(s.code)}">${escape(s.name)}</option>`).join('');
      if([...$('newsStock').options].some(o=>o.value===selected)) $('newsStock').value=selected;
      $('newsSources').innerHTML=data.sources.map(s=>`<div class="news-source">${escape(s.name)}：${s.status==='ok'?`取得成功・${Number(s.count)||0}件`:'取得失敗（'+escape(s.error||'不明')+'）'}／最終成功 ${escape(date(s.last_success_at))} JST</div>`).join('');
      $('newsNotifications').textContent='LINE通知：'+(data.notification_status||'未設定');
      status();render();
    } catch {
      $('newsStatus').className='news-status warn';
      $('newsStatus').textContent='ニュースデータを読み込めません。初回収集が未完了、または通信障害です。'+(data?'直前に読み込んだ記事を表示しています。':'架空のニュースは表示しません。');
      if(!data) $('newsRows').innerHTML='<div class="empty">取得完了後に記事が表示されます。</div>';
    } finally {busy=false;$('newsReload').disabled=false;}
  }
  ['newsSearch','newsTheme','newsStock','newsOfficial','newsSort'].forEach(id=>$(id).addEventListener(id==='newsSearch'?'input':'change',()=>{limit=20;render();}));
  $('newsReload').addEventListener('click',load);
  $('newsMore').addEventListener('click',()=>{limit+=20;render();});
  $('newsRows').addEventListener('click',e=>{const button=e.target.closest('[data-news-code]');if(button&&typeof openStock==='function')openStock(button.dataset.newsCode);});
  window.addEventListener('stock-data-updated',render);
  load();setInterval(load,300000);
})();
