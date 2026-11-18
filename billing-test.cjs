const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const {create}=require('./cloud-api');
test('paywall response preserves filters and links to a real billing entrypoint',async()=>{
 const api=create(async()=>({ok:false,status:402}),()=>"signed-fixture");
 await assert.rejects(api.enable(1,true),/Фільтри збережені/);
});
test('billing card refreshes after returning from bot and does not label free access as paid',async()=>{
 let response={sales_enabled:false,paid_access_required:false,access_available:true};
 const target={textContent:""},handlers={};
 const window={AutoDealCloud:{billingStatus:async()=>response},addEventListener:(n,f)=>handlers[n]=f};
 const document={getElementById:()=>target,addEventListener:(n,f)=>handlers[n]=f,visibilityState:'visible'};
 vm.runInNewContext(fs.readFileSync('billing-ui.js','utf8'),{window,document,Date});
 await new Promise(r=>setImmediate(r)); assert.match(target.textContent,/Налаштування радарів доступні без оплати/);assert.match(target.textContent,/Активність підписки/);assert.doesNotMatch(target.textContent,/сповіщення.*безкоштовні/);
 response={sales_enabled:true,paid_access_required:true,access_available:false,amount_stars:137};
 handlers.visibilitychange();await new Promise(r=>setImmediate(r));
 assert.match(target.textContent,/137 ⭐/);assert.match(target.textContent,/Радари збережені/);
 assert.doesNotMatch(target.textContent,/250 грн/);
 response={sales_enabled:true,paid_access_required:true,access_available:false,payment_method:'bank_manual',amount_uah:250};
 handlers.visibilitychange();await new Promise(r=>setImmediate(r));
 assert.match(target.textContent,/250 грн за 30 днів/);assert.doesNotMatch(target.textContent,/⭐/);
});

test('verified admin receives a subscription-management link and every verified user sees own Telegram ID',async()=>{
 let response={sales_enabled:false,paid_access_required:true,access_available:true,can_manage_subscriptions:true,telegram_id:111};
 const children=[],handlers={},target={textContent:'',insertAdjacentElement(where,node){assert.equal(where,'afterend');children.push(node);}};
 const window={AutoDealCloud:{billingStatus:async()=>response},addEventListener:(name,fn)=>handlers[name]=fn};
 const document={getElementById:()=>target,addEventListener:(name,fn)=>handlers[name]=fn,visibilityState:'visible',
  createElement:tag=>({tag,remove(){children.splice(children.indexOf(this),1);}})};
 vm.runInNewContext(fs.readFileSync('billing-ui.js','utf8'),{window,document,Date});
 await new Promise(r=>setImmediate(r));
 assert.equal(children.find(n=>n.id==='accountTelegramId').textContent,'Ваш Telegram ID: 111');
 const link=children.find(n=>n.id==='manageSubscriptionsLink');assert.equal(link.textContent,'Керування підписками');
 assert.equal(link.href,'payment-review.html?view=grants&v=20261118-grants-1');assert.ok(!/[?&](user_id|telegram_id|initData)=/.test(link.href));
 handlers.pageshow();await new Promise(r=>setImmediate(r));assert.equal(children.length,2);
 response={...response,can_manage_subscriptions:false,telegram_id:222};handlers.pageshow();await new Promise(r=>setImmediate(r));
 assert.equal(children.length,1);assert.equal(children[0].textContent,'Ваш Telegram ID: 222');
 response={...response,can_manage_subscriptions:'true'};handlers.pageshow();await new Promise(r=>setImmediate(r));assert.equal(children.length,1);
 window.AutoDealCloud.billingStatus=async()=>{throw new Error('offline');};handlers.pageshow();await new Promise(r=>setImmediate(r));assert.equal(children.length,0);
});
