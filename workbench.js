/* Browsing, version checks and historical navigation never call a model. */
(() => {
  'use strict';
  const historical = Boolean(window.YANSHENG_HISTORICAL);
  let current = window.YANSHENG_RELEASE, snapshot, candidate, timer, failures = 0, busy = false;
  let lastCheck = 0, readStarted = false;
  const pageName = () => {
    const part = location.pathname.split('/').pop();
    return !part || part === 'index.html' ? 'today.html' : part.includes('.') ? part : part + '.html';
  };
  const escape = v => String(v ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  const publicPath = path => {
    if (!/^[a-zA-Z0-9_.-]+\.(json|html)$/.test(path)) throw Error('Invalid publication asset');
    return new URL(path, location.href).href;
  };
  function notice(text, actionable = false) {
    const box = document.getElementById('update-notice');
    if (!box) return;
    box.hidden = !text; box.querySelector('span').textContent = text;
    const button = box.querySelector('button'); button.hidden = !actionable;
    button.textContent = historical ? '查看最新研究' : '更新';
  }
  async function loadBundle(manifest) {
    const response = await fetch(publicPath(manifest.bundle.path), {cache:'no-store'});
    if (!response.ok) throw Error('Release unavailable');
    const raw = await response.arrayBuffer();
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', raw))].map(x=>x.toString(16).padStart(2,'0')).join('');
    if (hash !== manifest.bundle.sha256) throw Error('Incomplete release');
    const value = JSON.parse(new TextDecoder().decode(raw));
    if (value.release_id !== manifest.release_id || value.schema !== 'yansheng-workbench-v1') throw Error('Release mismatch');
    for (const name of ['today.html','companies.html','company.html','opportunities.html','directions.html']) {
      if (!value.pages[name]) throw Error('Missing page');
    }
    if (!value.data?.companies || !value.data?.reports) throw Error('Missing research');
    return value;
  }
  function filterCompanies() {
    const input = document.getElementById('company-search');
    if (!input) return;
    let count = 0;
    document.querySelectorAll('#company-list .record').forEach(r => {
      r.hidden = !r.dataset.name.includes(input.value.trim()); if (!r.hidden) count++;
    });
    document.getElementById('company-empty').hidden = count > 0;
  }
  function companyView() {
    const content = document.getElementById('company-content');
    if (!content || !snapshot) return;
    const code = new URLSearchParams(location.search).get('code'), c = snapshot.data.companies[code];
    if (!c) { content.innerHTML='<h1>尚未收录这家公司</h1><p><a href="companies.html">返回公司研究</a></p>'; return; }
    const q = new URLSearchParams(location.search);
    document.getElementById('company-back').href = q.has('from') ? 'today.html?session='+encodeURIComponent(q.get('from')) : 'companies.html';
    document.title='研盛 · '+c.name;
    let out = '<div class="label">公司研究 / '+escape(c.research_date)+' · '+escape(c.depth)+'</div><h1>'+escape(c.name)+' <small>'+escape(c.code)+'</small></h1>';
    if (c.role) out += '<p>'+escape(c.role)+'</p>';
    if (c.direction) out += '<p><a href="directions.html#'+escape(c.direction)+'">查看方向变化</a></p>';
    out += '<h2>当前判断</h2><p>'+escape(c.change || c.summary)+'</p>';
    if(c.status) out += '<p>'+escape(c.status)+'</p>';
    if(c.premise) out += '<h3>为什么关注</h3><p>'+escape(c.premise)+'</p>';
    if(c.observed) out += '<h3>当天验证</h3><p>'+escape(c.observed)+'</p>';
    out += '<h3>下一步验证</h3><p>'+escape(c.next || '等待新的公司证据与市场表现。')+'</p>';
    if(c.boundary) out += '<p>'+escape(c.boundary)+'</p>';
    if(c.conditions?.length) {
      out += '<h3>原条件与结果</h3>';
      c.conditions.forEach(x => {out += '<p>'+escape(x.original)+'（'+escape(x.deadline)+'）：'+escape(x.result)+'</p>';});
    }
    out += '<details id="company-history"><summary>判断历史与原文</summary>';
    (c.history_original || []).forEach(h => {out += '<p>'+escape(h.date)+' · '+escape(h.reason)+'</p>';});
    (c.pool_history || []).forEach(h => {out += '<p>'+escape(h.date)+' · '+escape(h.from || '')+' → '+escape(h.to || '')+'：'+escape(h.reason)+'</p>';});
    (c.records || []).slice().reverse().forEach(r => {
      out += '<section><h3><a href="'+escape(r.page)+'">'+escape(r.date)+' '+(r.session==='close'?'收盘':'盘前')+' · 第'+r.revision+'版</a></h3>';
      r.excerpts.forEach(p => {out += '<p>'+escape(p)+'</p>';}); out += '</section>';
    });
    out += '</details>'; content.innerHTML=out;
  }
  function sessions() {
    const docs = [...document.querySelectorAll('article.doc')]; if(!docs.length) return;
    function show(value) {
      if(value==='pre') value='premarket';
      const selected = document.getElementById(value)||document.getElementById('premarket')||docs[0];
      docs.forEach(d=>d.hidden=d!==selected);
      document.querySelectorAll('button[data-session]').forEach(b=>{
        const active=b.dataset.session===selected.id; b.classList.toggle('active',active); b.setAttribute('aria-pressed',String(active));
      });
      document.querySelector('.date').textContent=selected.dataset.date;
      const toc=document.querySelector('.toc'); toc.replaceChildren();
      selected.querySelectorAll('h2,h3').forEach((h,i)=>{
        h.id=selected.id+'-'+i; const a=document.createElement('a');a.href='#'+h.id;a.textContent=h.textContent;toc.append(a);
      });
    }
    document.querySelectorAll('button[data-session]').forEach(b=>b.onclick=()=>{
      show(b.dataset.session); history.pushState(null,'','?session='+b.dataset.session); window.scrollTo(0,0);
    });
    show(new URLSearchParams(location.search).get('session'));
  }
  function bind() {
    document.documentElement.dataset.release=current;
    sessions(); companyView();
    const input=document.getElementById('company-search'); if(input) input.oninput=filterCompanies;
    const accept=document.getElementById('accept-update'); if(accept) accept.onclick=()=>{
      if(historical) location.href='today.html?session='+encodeURIComponent(new URLSearchParams(location.search).get('session')||'premarket');
      else applyCandidate();
    };
    filterCompanies();
  }
  function missingCurrentCompany(value) {
    if (pageName() !== 'company.html') return false;
    const code = new URLSearchParams(location.search).get('code');
    return Boolean(snapshot?.data.companies[code] && !value.data.companies[code]);
  }
  function applyCandidate() {
    if (!candidate || historical) return;
    if (missingCurrentCompany(candidate)) {
      notice('这家公司暂无新研究，保留当前历史记录。');
      return;
    }
    const page=candidate.pages[pageName()]; if(!page) {notice('此页保留历史记录，请查看最新研究。');return;}
    const query=document.getElementById('company-search')?.value;
    const details=[...document.querySelectorAll('details[open]')].map(x=>x.id).filter(Boolean);
    const y=window.scrollY, name=pageName();
    const doc=new DOMParser().parseFromString(page,'text/html');
    const oldMain=document.querySelector('main'), newMain=doc.querySelector('main');
    if(!oldMain||!newMain) {notice('暂时无法更新，请稍后重试。');return;}
    oldMain.replaceWith(newMain);
    const oldAside=document.querySelector('aside'), newAside=doc.querySelector('aside');
    if(oldAside&&newAside) oldAside.replaceWith(newAside);
    snapshot=candidate;current=candidate.release_id;candidate=null;window.YANSHENG_RELEASE=current;
    bind();
    if(query!==undefined && document.getElementById('company-search')) {document.getElementById('company-search').value=query;filterCompanies();}
    details.forEach(id=>{const el=document.getElementById(id);if(el)el.open=true;});
    if(location.hash&&document.getElementById(location.hash.slice(1))) document.getElementById(location.hash.slice(1)).scrollIntoView();
    else window.scrollTo(0,y);
    notice('');
  }
  function schedule() {
    clearTimeout(timer); if(!document.hidden) timer=setTimeout(check,failures?Math.min(480000,120000*2**(failures-1)):120000);
  }
  async function check() {
    if(busy||document.hidden) {schedule();return;}
    busy=true;lastCheck=Date.now();
    try {
      const url=new URL('publication.json',location.href);url.searchParams.set('check',String(Math.floor(Date.now()/30000)));
      const response=await fetch(url.href,{cache:'no-store'});
      if(!response.ok) throw Error('Manifest unavailable');
      const manifest=await response.json();
      if(manifest.release_id!==current) {
        if(!candidate||candidate.release_id!==manifest.release_id) candidate=await loadBundle(manifest);
        if(historical) notice('有更新的研究可查看。',true);
        else if(missingCurrentCompany(candidate)) notice('这家公司暂无新研究，保留当前历史记录。');
        else if(!readStarted&&window.scrollY<20&&!window.getSelection()?.toString()) applyCandidate();
        else notice('有新研究，点击更新。',true);
      } else if(!snapshot) {
        snapshot=await loadBundle(manifest);bind();
      } else if(failures) notice('');
      failures=0;
    } catch(error) {failures++;notice('暂时无法检查更新，仍可阅读当前内容。',Boolean(candidate));}
    finally {busy=false;schedule();}
  }
  // Load the page's own immutable snapshot, even if another release is already live.
  async function initial() {
    try {
      const response=await fetch(publicPath('release-'+current+'.json'),{cache:'force-cache'});
      if(!response.ok) throw Error('Initial release unavailable');
      const raw=await response.arrayBuffer();
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',raw))].map(x=>x.toString(16).padStart(2,'0')).join('');
      if(hash!==window.YANSHENG_BUNDLE_SHA) throw Error('Initial release incomplete');
      const value=JSON.parse(new TextDecoder().decode(raw));if(value.release_id!==current) throw Error('Initial release mismatch');
      snapshot=value;bind();
    } catch(error) {notice('公司资料暂时无法读取，请稍后重试。');}
    check();
  }
  ['scroll','pointerdown','keydown'].forEach(e=>window.addEventListener(e,()=>{readStarted=true;},{passive:true}));
  document.addEventListener('input',()=>{readStarted=true;},{passive:true});
  window.addEventListener('popstate',sessions);
  window.addEventListener('focus',()=>{if(Date.now()-lastCheck>=30000) check();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(timer);else if(Date.now()-lastCheck>=30000)check();else schedule();});
  bind();initial();
})();
