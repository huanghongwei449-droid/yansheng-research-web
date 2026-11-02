const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('out/today.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const docs = ['premarket','close'].map(id => {
  const inner = html.match(new RegExp('<article class="doc" id="'+id+'"[\\s\\S]*?<\\/article>'))[0];
  return {id,hidden:true,dataset:{date:inner.match(/data-date="([^"]+)"/)[1]},querySelectorAll(){
    return [...inner.matchAll(/<h[23]>(.*?)<\/h[23]>/g)].map(m=>({textContent:m[1]}));
  }};
});
const buttons=docs.map(d=>({dataset:{session:d.id},classList:{toggle(){}},setAttribute(){}}));
const date={}; const toc={children:[],replaceChildren(){this.children=[]},append(x){this.children.push(x)}};
let back;
const location={search:'?session=close'};
const context={URLSearchParams,location,history:{pushState(a,b,url){location.search=url}},
  window:{scrollTo(){},addEventListener(type,fn){back=fn}},
  document:{querySelectorAll(q){return q==='article.doc'?docs:buttons},getElementById(id){return docs.find(d=>d.id===id)},
    querySelector(q){return q==='.date'?date:toc},createElement(){return {}}}};
vm.runInNewContext(script,context);
assert.equal(docs[1].hidden,false);assert.equal(docs[0].hidden,true);assert.equal(toc.children.length,docs[1].querySelectorAll().length);
assert.equal(toc.children[0].textContent,docs[1].querySelectorAll()[0].textContent);
buttons[0].onclick();assert.equal(docs[0].hidden,false);assert.equal(docs[1].hidden,true);
assert.equal(location.search,'?session=premarket');assert.ok(toc.children.length>3);
location.search='?session=close';back();assert.equal(docs[1].hidden,false);assert.equal(toc.children.length,docs[1].querySelectorAll().length);
assert.ok(!html.includes('report-2026-09-30-close-d26148542d0c.html'));
assert.ok(html.includes('grid-template-columns:210px minmax(0,850px)'));
assert.ok(html.includes('@media(max-width:700px)'));
console.log('PASS: session query, switches, back navigation, per-session directory, withheld v2, original grid and mobile CSS');
