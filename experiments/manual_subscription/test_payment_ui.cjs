'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const UI=require('./payment-ui.js');
const Memory=require('./demo-adapter.js');

// Event wiring/visibility fixture, not a browser layout or accessibility engine.
function dom(html=fs.readFileSync(__dirname+'/trial.html','utf8')){
  const nodes={};
  for(const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bdata-ui="([^"]+)"[^>]*)>/g)) {
    const attrs=match[2], id=match[3], handlers={};
    assert.ok(!nodes[id],'duplicate UI control');
    nodes[id]={hidden:/\bhidden\b/.test(attrs),disabled:false,textContent:'',checked:false,
      value:attrs.match(/\bvalue="([^"]*)"/)?.[1] || '',
      addEventListener:(event,fn)=>{(handlers[event]??=[]).push(fn)},
      setAttribute:(key,value)=>{nodes[id][key]=value},
      async click(){if(this.disabled) return;for(const fn of handlers.click||[])await fn()},
    };
  }
  nodes.clarification.value=html.match(/<textarea[^>]+data-ui="clarification"[^>]*>([^<]*)/)[1];
  return {nodes,querySelector(selector){
    const id=selector.match(/^\[data-ui="([^"]+)"\]$/)?.[1];
    assert.ok(id&&nodes[id],'control missing from actual HTML: '+selector);
    return nodes[id];
  }};
}

async function flow(api,connected=false,existing=null){
  const root=existing?.root || dom(),n=root.nodes;
  const app=existing?.app || UI.mount(root,api);await app.ready;
  assert.equal(n.admin.hidden,true);assert.equal(n.client.hidden,false);
  assert.equal(n.tariff.textContent,'250 грн · 30 днів · тест');
  await n.create.click();assert.equal(app.state().view.order.state,'awaiting');
  assert.equal(app.state().view.order.amount,250);
  assert.equal(app.state().view.order.days,30);
  assert.equal(app.state().view.payments_enabled,false);
  await n.paid.click();assert.equal(n['receipt-area'].hidden,false);
  await n.receipt.click();assert.equal(app.state().view.order.state,'review');
  assert.equal(app.state().view.membership.active,false);
  await n['admin-tab'].click();assert.equal(n.client.hidden,true);
  assert.equal(n['admin-form'].hidden,false);
  await n.approve.click();assert.ok(n.error.textContent);assert.equal(app.state().view.order.state,'review');
  n.verified.checked=true;n.amount.value='1';
  await n.approve.click();assert.ok(n.error.textContent);assert.equal(app.state().view.membership.active,false);
  n.amount.value='250';n.clarification.value='Уточни тестовий час';
  await n.clarify.click();assert.equal(app.state().view.order.state,'clarification');
  await n['client-tab'].click();assert.equal(n.note.textContent,'Уточни тестовий час');
  await n.paid.click();await n.receipt.click();assert.equal(app.state().view.order.state,'review');
  await n['admin-tab'].click();assert.equal(n.verified.checked,false);
  n.verified.checked=true;await n.approve.click();
  assert.equal(app.state().view.order.state,'approved');assert.equal(n['admin-form'].hidden,true);
  const expiry=app.state().view.membership.expires_at;
  await n['client-tab'].click();assert.match(n.access.textContent,/Абонемент активний до/);
  assert.equal(app.state().view.search_enabled,false);
  await n.create.click();await n.paid.click();await n.receipt.click();
  await n['admin-tab'].click();assert.equal(n.verified.checked,false);
  n.verified.checked=true;await n.approve.click();
  assert.equal(app.state().view.membership.expires_at,expiry+30*86400);
  const refreshed=UI.mount(dom(),api);await refreshed.ready;
  assert.equal(refreshed.state().view.membership.active,true);
  console.log(connected?'Connected UI -> SQLite event checks passed':'In-chat RAM UI event checks passed');
}

async function busy(){
  let calls=0,resolve;
  const memory=Memory.create();
  const api={state:memory.state,action:(role,body)=>{calls++;return new Promise(r=>{resolve=()=>r(memory.action(role,body))})}};
  const root=dom(),app=UI.mount(root,api);await app.ready;
  const pending=root.nodes.create.click();await root.nodes.create.click();
  assert.equal(calls,1);assert.equal(root.nodes.create.disabled,true);
  resolve();await pending;assert.equal(root.nodes.create.disabled,false);
  console.log('Busy/double-tap guard passed');
}

async function connected(base){
  const url=new URL(base);
  assert.equal(url.hostname,'127.0.0.1');assert.equal(url.protocol,'http:');
  const config=await (await fetch(base+'/fixture-config.js')).text();
  let actualApi;
  const window={AutoDealPaymentUI:{mount(root,api){actualApi=api;return UI.mount(root,api)}}};
  vm.runInNewContext(config,{window});
  const root=dom();
  const context={window,document:{getElementById:id=>{
    assert.equal(id,'autodeal-payment-trial');return root;
  }},fetch:(path,options)=>{
    assert.ok(['/api/state','/api/action'].includes(path),'transport path must stay local');
    return fetch(base+path,options);
  },Error,JSON};
  vm.runInNewContext(fs.readFileSync(__dirname+'/connected.js','utf8'),context);
  await window.AutoDealFixtureApp.ready;
  // The exported transport mounts the actual page; capture it for the same
  // click sequence. Real requests reach Harness -> Adapter -> SQLite Ledger.
  await flow(actualApi,true,{root,app:window.AutoDealFixtureApp});
}

async function inline(){
  const html=fs.readFileSync(__dirname+'/payment-trial-inline.html','utf8');
  assert.ok(!/<(?:html|body|head|!doctype)\b/i.test(html),'inline output must be a fragment');
  assert.ok(!/fetch\(|XMLHttpRequest|WebSocket|localStorage|sessionStorage|document\.cookie/.test(html));
  for(const tag of html.matchAll(/<button([^>]*)>/g))
    assert.equal((tag[1].match(/\bclass=/g)||[]).length,1,'duplicate/missing button class');
  const root=dom(html);
  const context={document:{getElementById:id=>{assert.equal(id,'autodeal-payment-trial');return root}},
    fetch:()=>{throw Error('No network allowed')},console};
  context.window=context;
  vm.createContext(context);
  for(const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g))vm.runInContext(match[1],context);
  await flow(context.AutoDealTrialApi,false,{root,app:context.AutoDealTrialApp});
  console.log('Exact inline fragment event checks passed');
}

async function reject(){
  const api=Memory.create(),root=dom(),n=root.nodes,app=UI.mount(root,api);
  await app.ready;await n.create.click();await n.paid.click();await n.receipt.click();
  const first=app.state().view.order.id;
  assert.throws(()=>api.action('client',{action:'approve',order_id:first,data:{}}),/недоступна/);
  await n['admin-tab'].click();await n.reject.click();
  await n['client-tab'].click();assert.equal(n.status.textContent,'Відхилено');
  assert.equal(n.create.hidden,false);assert.equal(app.state().view.membership.active,false);
  await n.create.click();assert.notEqual(app.state().view.order.id,first);
  console.log('Rejection/new-order and RAM role checks passed');
}

async function legacyQuote(){
  const root=dom(),n=root.nodes;
  const api={state:()=>({amount:250,days:30,payments_enabled:false,
    order:{id:'AD-OLD-QUOTE',state:'review',amount:249,days:30,receipt_revision:1},
    membership:{active:false,expires_at:null}})};
  await UI.mount(root,api).ready;
  assert.equal(n.tariff.textContent,'249 грн · 30 днів · тест');
  await n['admin-tab'].click();assert.equal(n.amount.value,'249');
  assert.equal(n['admin-summary'].textContent,'На перевірці · 249 грн');
  assert.equal(n.verified.checked,false);
  assert.match(n.create.textContent,/250 грн$/);
  console.log('Prior quote stays 249 in payment/review; new quote stays 250 passed');
}

module.exports={dom};
if(require.main===module)(async()=>{
  await flow(Memory.create());await busy();await reject();await inline();await legacyQuote();
  if(process.argv[2])await connected(process.argv[2]);
})().catch(e=>{console.error(e);process.exitCode=1});
