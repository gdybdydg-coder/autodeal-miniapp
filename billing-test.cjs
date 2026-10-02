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
 await new Promise(r=>setImmediate(r)); assert.match(target.textContent,/безкоштовний/);assert.match(target.textContent,/ще не відкрито/);
 response={sales_enabled:true,paid_access_required:true,access_available:false,amount_stars:137};
 handlers.visibilitychange();await new Promise(r=>setImmediate(r));
 assert.match(target.textContent,/137 ⭐/);assert.match(target.textContent,/Фільтри збережені/);
 assert.doesNotMatch(target.textContent,/250 грн/);
 response={sales_enabled:true,paid_access_required:true,access_available:false,payment_method:'bank_manual',amount_uah:250};
 handlers.visibilitychange();await new Promise(r=>setImmediate(r));
 assert.match(target.textContent,/250 грн за 30 днів/);assert.doesNotMatch(target.textContent,/⭐/);
});
