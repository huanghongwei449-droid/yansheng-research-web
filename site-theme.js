/* Self-drawn SVGs, no external assets. Text nodes are preserved verbatim. */
(() => {
  'use strict';
  const ns = 'http://www.w3.org/2000/svg';
  const icons = {
    calendar: '<rect x="2.5" y="4" width="11" height="9.5" rx="1"/><path d="M5 2v4M11 2v4M3 7h10M5 9h2M9 9h2"/>',
    target: '<circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="3"/><circle cx="8" cy="8" r=".7"/>',
    building: '<path d="M3 14V3l6-1v12M9 6h4v8M2 14h12M5 5h1M5 8h1M5 11h1M11 9h.5M11 11h.5"/>',
    market: '<path d="M2 13h12M3 11l3-3 2 1 4-5M10 4h2v2"/>',
    drivers: '<circle cx="8" cy="8" r="2"/><circle cx="3" cy="4" r="1"/><circle cx="13" cy="4" r="1"/><circle cx="3" cy="12" r="1"/><circle cx="13" cy="12" r="1"/><path d="M4 4.5 6.5 7M12 4.5 9.5 7M4 11.5 6.5 9M12 11.5 9.5 9"/>',
    radar: '<circle cx="8" cy="8" r="6"/><path d="M8 2v6l4-3M8 5a3 3 0 1 0 3 3"/>',
    pool: '<path d="M3 3h10v10H3zM5 6h6M5 8h6M5 10h4"/><circle cx="11.5" cy="11.5" r="2"/>',
    plan: '<rect x="3" y="2.5" width="10" height="11" rx="1"/><path d="M5 6l1 1 2-2M9 6h2M5 10l1 1 2-2M9 10h2"/>',
    risk: '<path d="M8 2 14 13H2L8 2zM8 6v3M8 11h.01"/>',
    document: '<path d="M4 2h6l2 2v10H4zM10 2v3h2M6 7h4M6 9h4M6 11h3"/>',
    confirmed: '<circle cx="8" cy="8" r="6"/><path d="m4.5 8 2.3 2.3 4.7-5"/>',
    candidate: '<circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3 3M7 5v4M5 7h4"/>',
    paused: '<circle cx="8" cy="8" r="6"/><path d="M6.3 5.3v5.4M9.7 5.3v5.4"/>',
    exited: '<path d="M8 2.5H3.5v11H8M7 8h6M10.5 5.5 13 8l-2.5 2.5"/>',
    pass: '<circle cx="8" cy="8" r="5.5"/><path d="m5 8 2 2 4-4"/>',
    fail: '<circle cx="8" cy="8" r="5.5"/><path d="m5.5 5.5 5 5m0-5-5 5"/>',
    wait: '<circle cx="8" cy="8" r="5.5"/><path d="M8 4.5v4l2.5 1.5"/>',
    plus: '<circle cx="8" cy="8" r="5.5"/><path d="M8 5v6M5 8h6"/>',
    up: '<path d="M8 13V3M4 7l4-4 4 4"/>',
    down: '<path d="M8 3v10m-4-4 4 4 4-4"/>',
    minus: '<circle cx="8" cy="8" r="5.5"/><path d="M5 8h6"/>'
  };
  const states = {'未通过':'fail','不通过':'fail','通过':'pass','候补':'wait','未触发':'wait','已触发':'pass','失效':'fail','新增':'plus','升级':'up','降级':'down','剔除':'minus'};
  const pattern = /未通过|不通过|未触发|已触发|通过|候补|失效|新增|升级|降级|剔除|[+\-−]\d+(?:\.\d+)?%/g;
  function svg(kind) {
    const el=document.createElementNS(ns,'svg'); el.setAttribute('viewBox','0 0 16 16');
    el.setAttribute('aria-hidden','true'); el.setAttribute('focusable','false');
    el.setAttribute('fill','none'); el.setAttribute('stroke','currentColor');
    el.setAttribute('stroke-width','1.7'); el.setAttribute('stroke-linecap','round'); el.setAttribute('stroke-linejoin','round');
    el.classList.add('theme-icon'); el.innerHTML=icons[kind]; return el;
  }
  function iconFor(el) {
    const text=el.textContent.trim();
    const group = el.parentElement && el.parentElement.id;
    if (group && {'pool-confirmed':1,'pool-candidate':1,'pool-paused':1,'pool-exited':1}[group]) return group.slice(5);
    if(el.matches('header nav a')) {
      if(text.includes('今日研究')) return 'calendar';
      if(text.includes('机会跟踪')) return 'target';
      if(text.includes('公司研究')) return 'building';
      return 'document';
    }
    if(/风险|公告/.test(text)) return 'risk';
    if(/交易计划|今日计划|明天怎么跟/.test(text)) return 'plan';
    if(/雷达|扫描/.test(text)) return 'radar';
    if(/关注池|个股池|观察名单|候选|二波池/.test(text)) return 'pool';
    if(/题材|驱动|方向|主线/.test(text)) return 'drivers';
    if(/市场背景|市场表现|外围|宏观/.test(text)) return 'market';
    return 'document';
  }
  function adorn(root) {
    if (!(root instanceof Element)) return;
    const all = (selector) => [...(root.matches(selector)?[root]:[]), ...root.querySelectorAll(selector)];
    all('header nav a, h2, h3, #pool-exited > summary').forEach(el => {
      if(el.dataset.themeIcon || el.closest('.pool-item')) return;
      el.dataset.themeIcon='1'; el.prepend(svg(iconFor(el)));
    });
    all('p').forEach(p => {
      const prev=p.previousElementSibling;
      if (/一句话定调|指引一句话|先说结论/.test(p.textContent) || (prev && /^一句话定调$/.test(prev.textContent.trim()))) p.classList.add('theme-callout');
      const strong=p.querySelector(':scope > strong:first-child');
      const classification=strong && strong.textContent.trim().match(/^(入选|候补)[一二三四五六七八九十\d]/);
      if(classification && !p.closest('.pool-item') && !p.classList.contains('theme-direction-card')) {
        p.classList.add('theme-direction-card');
        const badge=document.createElement('span');badge.className='theme-badge';
        badge.setAttribute('aria-hidden','true');badge.append(svg(classification[1]==='入选'?'pass':'wait'));
        badge.append(document.createTextNode(classification[1]));strong.after(badge);
      }
    });
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode(node){
      if(!node.parentElement || node.parentElement.closest('script,style,textarea,option,.theme-token,.theme-badge,svg,.theme-toc,.visual-radar')) return NodeFilter.FILTER_REJECT;
      pattern.lastIndex=0; return pattern.test(node.data)?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT;
    }});
    const nodes=[];while(walker.nextNode()) nodes.push(walker.currentNode);
    for(const node of nodes){
      const frag=document.createDocumentFragment();let pos=0;pattern.lastIndex=0;
      for(const match of node.data.matchAll(pattern)){
        frag.append(document.createTextNode(node.data.slice(pos,match.index)));
        const span=document.createElement('span');span.className='theme-token';
        if(states[match[0]]){span.classList.add(['pass','plus','up'].includes(states[match[0]])?'theme-positive':'theme-negative');if (!node.parentElement.closest('.pool-item')) span.append(svg(states[match[0]]));}
        else span.classList.add(match[0][0]==='+'?'theme-positive':'theme-negative');
        span.append(document.createTextNode(match[0]));frag.append(span);pos=match.index+match[0].length;
      }
      frag.append(document.createTextNode(node.data.slice(pos)));node.replaceWith(frag);
    }
  }
  function toc() {
    if(document.querySelector('.theme-toc')) return;
    const main=document.querySelector('main'); if(!main) return;
    const details=document.createElement('details');details.className='theme-toc';
    const summary=document.createElement('summary');summary.textContent='本页目录'; details.append(summary);
    const list=document.createElement('div');details.append(list);main.before(details);
    function refresh(){
      list.replaceChildren();
      [...main.querySelectorAll('h2')].filter(h=>!h.closest('[hidden]')).forEach((h,i)=>{
        if(!h.id)h.id='theme-section-'+i;
        const a=document.createElement('a');a.href='#'+h.id;a.textContent=h.textContent;list.append(a);
      });
    }
    details.addEventListener('toggle',()=>{if(details.open)refresh()});
  }
  adorn(document.body);toc();
  const observer=new MutationObserver(records=>{
    observer.disconnect();
    for(const record of records) for(const node of record.addedNodes){
      if(node.nodeType===Node.ELEMENT_NODE && !node.closest('.theme-token,.theme-badge,svg,.theme-toc')) adorn(node);
      else if(node.nodeType===Node.TEXT_NODE && node.parentElement && !node.parentElement.closest('.theme-token,script,style,.theme-toc')) adorn(node.parentElement);
    }
    observer.observe(document.body,{childList:true,subtree:true});
  });
  observer.observe(document.body,{childList:true,subtree:true});
})();
