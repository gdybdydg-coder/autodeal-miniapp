'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const UI=require('./payment-ui.js');
const Transport=require('./owner-transport.js');
const {dom}=require('./test_payment_ui.cjs');

// Actual owner-login.js + UI handlers + raw transport, minimal DOM event fixture.
// No real browser renderer, file picker, object URL engine or bank document.
function page(role,fetchLocal,html){
  let current;
  const ids={};
  for(const id of ['owner-login','owner-session','owner-code','owner-enter','owner-error',
                  'owner-deadline','owner-rotate','owner-logout','owner-session-error']){
    const handlers={};
    ids[id]={hidden:id==='owner-session',disabled:false,value:'',textContent:'',
      addEventListener(event,fn){(handlers[event]??=[]).push(fn)},
      async click(){if(this.disabled)return;for(const fn of handlers.click||[])await fn()},
    };
    assert.ok(html.includes('id="'+id+'"'),'ID missing from served page');
  }
  function fresh(){
    const root=dom(html);root.hidden=true;
    root.cloneNode=()=>fresh();root.replaceWith=replacement=>{current=replacement};
    return root;
  }
  current=fresh();
  const downloads=[];
  const document={
    getElementById:id=>id==='autodeal-payment-trial'?current:ids[id],
    createElement:tag=>{assert.equal(tag,'a');return {click(){downloads.push(this.download)},remove(){}}},
    body:{appendChild(){}},
  };
  const window={location:{pathname:'/'+role},fetch:fetchLocal,
                AutoDealOwnerTransport:Transport,AutoDealPaymentUI:UI};
  const context={window,document,Intl,URL:{createObjectURL:()=> 'blob:synthetic',revokeObjectURL(){}},
                 setTimeout:fn=>fn()};
  vm.runInNewContext(fs.readFileSync(__dirname+'/owner-login.js','utf8'),context);
  return {ids,root:()=>current,downloads};
}

async function main(){
  const config=JSON.parse(fs.readFileSync(0,'utf8'));
  const url=new URL(config.base);
  assert.equal(url.hostname,'127.0.0.1');assert.equal(url.protocol,'http:');
  const transportPaths=new Set(['/api/login','/api/state','/api/action','/api/receipt','/api/rotate','/api/logout']);
  let loseApprovalResponse=false;
  const fetchLocal=async(path,options)=>{
    assert.ok(transportPaths.has(path)||/^\/api\/receipts\/RCPT-[a-f0-9]{32}$/.test(path));
    const response=await fetch(config.base+path,options);
    if(loseApprovalResponse && path==='/api/action' && response.ok && JSON.parse(options.body).action==='approve') {
      loseApprovalResponse=false;
      throw Error('Synthetic lost HTTP approval response');
    }
    return response;
  };
  const [clientPage,adminPage]=await Promise.all(['/client','/admin'].map(async path=>{
    const response=await fetch(config.base+path);assert.equal(response.status,200);return response.text();
  }));
  assert.ok(!clientPage.includes('fixture-config.js'));
  const client=page('client',fetchLocal,clientPage),admin=page('admin',fetchLocal,adminPage);
  client.ids['owner-code'].value=config.invitations.client;
  await client.ids['owner-enter'].click();assert.equal(client.root().hidden,false);
  assert.equal(client.ids['owner-login'].hidden,true);
  let c=client.root().nodes;
  assert.equal(c['client-tab'].hidden,true);assert.equal(c['admin-tab'].hidden,true);
  await c['admin-tab'].click();assert.equal(c.admin.hidden,true);
  await c.create.click();assert.equal(c.status.textContent,'Очікує квитанцію');
  await c.paid.click();assert.equal(c['file-area'].hidden,false);
  await c.receipt.click();assert.match(c.error.textContent,/обери тестовий файл/);
  assert.equal(c.status.textContent,'Очікує квитанцію');
  const file=new Blob([Buffer.from(config.png,'base64')],{type:'image/png'});
  c.file.files=[file];await c.receipt.click();assert.equal(c.status.textContent,'На перевірці');
  assert.match(c.access.textContent,/неактивний/);

  admin.ids['owner-code'].value=config.invitations.admin;
  await admin.ids['owner-enter'].click();let a=admin.root().nodes;
  assert.equal(a.client.hidden,true);assert.equal(a.admin.hidden,false);
  assert.equal(a.download.hidden,false);await a.download.click();
  assert.deepEqual(admin.downloads,['receipt.png']);
  await a.approve.click();assert.ok(a.error.textContent);assert.equal(a.status.textContent,'На перевірці');
  a.verified.checked=true;a.amount.value='1';await a.approve.click();assert.ok(a.error.textContent);
  a.amount.value='249';await a.clarify.click();assert.equal(a.status.textContent,'Потрібне уточнення');
  await client.ids['owner-rotate'].click();
  assert.equal(client.root().hidden,false);assert.equal(client.ids['owner-error'].textContent,'');
  // A fresh authorized mount sees durable clarification without sharing the admin session.
  const clientApi=Transport.create(fetchLocal,'client');
  await clientApi.login(config.refreshInvitation);
  const refreshed=dom(clientPage),refreshedApp=UI.mount(refreshed,clientApi,{role:'client'});
  await refreshedApp.ready;assert.match(refreshed.nodes.note.textContent,/Уточни/);
  await refreshed.nodes.paid.click();refreshed.nodes.file.files=[file];
  await refreshed.nodes.receipt.click();assert.equal(refreshed.nodes.status.textContent,'На перевірці');
  // Reload admin UI via a new invitation rather than a public role selector.
  await admin.ids['owner-logout'].click();assert.equal(admin.root().hidden,true);
  admin.ids['owner-code'].value=config.adminRefreshInvitation;
  await admin.ids['owner-enter'].click();a=admin.root().nodes;
  assert.equal(a.verified.checked,false);a.verified.checked=true;
  loseApprovalResponse=true;await a.approve.click();
  assert.equal(a.status.textContent,'На перевірці');
  assert.match(a.error.textContent,/lost HTTP/);
  const firstExpiry=(await clientApi.state()).membership.expires_at;
  await a.approve.click();
  assert.equal(a.status.textContent,'Підтверджено');
  const status=await clientApi.state();assert.equal(status.membership.active,true);
  assert.equal(status.membership.expires_at,firstExpiry,'lost response retry cannot extend access twice');
  assert.equal(status.search_enabled,false);assert.equal(status.role,'client');
  await assert.rejects(clientApi.action('admin',{action:'approve',order_id:status.order.id,
    data:{payment_ref:'SYNTH-BYPASS',actual_amount:249,bank_verified:true}}));
  assert.equal((await clientApi.state()).role,'client','role denial must not clear a valid client session');
  await assert.rejects(clientApi.download(status.receipt_file.id));
  await clientApi.logout();await assert.rejects(clientApi.state());
  await client.ids['owner-logout'].click();assert.equal(client.root().hidden,true);
  assert.equal(client.root().nodes.status.textContent,'','logout clears the private DOM snapshot');
  assert.equal(client.ids['owner-session'].hidden,true);
  await admin.ids['owner-logout'].click();assert.equal(admin.root().hidden,true);
  console.log('Separate login -> file upload -> admin download/clarify/approve -> SQLite checks passed');
  console.log('Rotation/logout, hidden role controls, private DOM reset and client denial checks passed');
  console.log('Lost approval response -> same payment reference retry -> one membership grant passed');
}

main().catch(error=>{console.error(error);process.exitCode=1});
