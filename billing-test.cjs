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
 const document={getElementById:id=>id==='billingStatus'?target:null,addEventListener:(n,f)=>handlers[n]=f,visibilityState:'visible'};
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
 const document={getElementById:id=>id==='billingStatus'?target:null,addEventListener:(name,fn)=>handlers[name]=fn,visibilityState:'visible',
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

class OfferElement {
 constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.textContent='';this.hidden=false;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 addEventListener(name,handler){this.handlers[name]=handler;}
 remove(){this.removed=true;}
}
const offerNodes=node=>[node,...node.children.flatMap(offerNodes)];
const paidOffer={sales_enabled:true,paid_access_required:true,access_available:false,payment_method:'bank_manual',amount_uah:250,days:30,
 purchase_url:'https://t.me/auto_deal_finder1_bot?start=buy_subscription',status_url:'https://t.me/auto_deal_finder1_bot?start=subscribe',
 terms:'250 грн / 30 днів. Підтвердження власником. Без автосписань.',terms_version:'20261002-card-v1',request:null};
async function offerFixture(initial,{sdk=true,sdkThrows=false}={}){
 let response=initial,reads=0;const handlers={},opened=[];
 const elements={billingStatus:new OfferElement('p'),radarPurchase:new OfferElement('section'),settingsPurchase:new OfferElement('div')};
 const window={AutoDealCloud:{billingStatus:async()=>{reads++;if(response instanceof Error)throw response;return response;}},addEventListener:(name,fn)=>handlers[name]=fn};
 if(sdk)window.Telegram={WebApp:{openTelegramLink:url=>{if(sdkThrows)throw new Error('native fallback');opened.push(url);}}};
 const document={getElementById:id=>elements[id]||null,addEventListener:(name,fn)=>handlers[name]=fn,visibilityState:'visible',createElement:tag=>new OfferElement(tag)};
 vm.runInNewContext(fs.readFileSync('billing-ui.js','utf8'),{window,document,Date});await new Promise(r=>setImmediate(r));
 return {elements,opened,get reads(){return reads;},link:()=>offerNodes(elements.radarPurchase).find(n=>n.tag==='a'),
  async refresh(next){response=next;handlers.pageshow();await new Promise(r=>setImmediate(r));}};
}

test('inactive verified bank offer is visible on dashboard and settings without creating a purchase',async()=>{
 const ui=await offerFixture(paidOffer);assert.equal(ui.reads,1);assert.deepEqual(ui.opened,[]);
 for(const panel of [ui.elements.radarPurchase,ui.elements.settingsPurchase]){
  assert.equal(panel.hidden,false);const nodes=offerNodes(panel),text=nodes.map(n=>n.textContent).join(' ');
  assert.match(text,/250 грн \/ 30 днів/);assert.match(text,/після перевірки оплати власником/);assert.match(text,/Без автоматичних списань/);
  assert.ok(nodes.some(n=>n.tag==='summary'&&n.textContent==='Умови підписки'));
  assert.ok(nodes.some(n=>n.tag==='p'&&n.textContent===paidOffer.terms));
  assert.ok(nodes.findIndex(n=>n.tag==='details')<nodes.findIndex(n=>n.tag==='a'));
  assert.equal(nodes.find(n=>n.tag==='a').textContent,'Купити підписку — 250 грн');
 }
 let prevented=false;ui.link().handlers.click({preventDefault(){prevented=true;}});
 assert.equal(prevented,true);assert.deepEqual(ui.opened,[paidOffer.purchase_url]);assert.equal(ui.reads,1);
});

test('closed or malformed offers never expose a buy link or trust arbitrary destinations',async()=>{
 const cases=[{sales_enabled:false},{sales_enabled:'true'},{payment_method:'stars'},{amount_uah:251},{days:31},{terms:''},{terms_version:''},
  {purchase_url:'https://attacker.invalid/?user_id=111'},{purchase_url:'javascript:alert(1)'},{purchase_url:paidOffer.purchase_url+'&user_id=111'},
  {purchase_url:paidOffer.status_url},{access_available:true},{access_available:undefined}];
 for(const delta of cases){const ui=await offerFixture({...paidOffer,...delta});assert.equal(ui.elements.radarPurchase.hidden,true);assert.equal(ui.elements.settingsPurchase.hidden,true);assert.deepEqual(ui.opened,[]);}
 const incomplete={...paidOffer};delete incomplete.request;const ui=await offerFixture(incomplete);assert.equal(ui.elements.radarPurchase.hidden,true);
});

test('active accounts retain existing billing status without another buy offer',async()=>{
 const ui=await offerFixture({...paidOffer,access_available:true,expires_at:1890000000});
 assert.equal(ui.elements.radarPurchase.hidden,true);assert.equal(ui.elements.settingsPurchase.hidden,true);
 assert.match(ui.elements.billingStatus.textContent,/Доступ активний/);assert.match(ui.elements.billingStatus.textContent,/Київ/);
});

test('review and clarification use status route and do not offer another purchase even if sales remain open',async()=>{
 for(const state of ['review','clarification']){
  const ui=await offerFixture({...paidOffer,request:{state,awaiting_receipt:true}});
  assert.equal(ui.elements.radarPurchase.hidden,false);assert.equal(ui.link().href,paidOffer.status_url);
  assert.doesNotMatch(offerNodes(ui.elements.radarPurchase).map(n=>n.textContent).join(' '),/Купити підписку —/);
  ui.link().handlers.click({preventDefault(){}});assert.deepEqual(ui.opened,[paidOffer.status_url]);
 }
 const reviewed=await offerFixture({...paidOffer,sales_enabled:false,request:{state:'review'}});
 assert.equal(reviewed.link().href,paidOffer.status_url);assert.match(offerNodes(reviewed.elements.radarPurchase).map(n=>n.textContent).join(' '),/Повторно не сплачуйте/);
});

test('created request continues to details when available and never claims that payment was already made',async()=>{
 const ui=await offerFixture({...paidOffer,request:{state:'created',awaiting_receipt:true}});
 assert.equal(ui.link().textContent,'Реквізити та квитанція');assert.equal(ui.link().href,paidOffer.purchase_url);
 assert.match(offerNodes(ui.elements.radarPurchase).map(n=>n.textContent).join(' '),/після переказу/);
 assert.doesNotMatch(offerNodes(ui.elements.radarPurchase).map(n=>n.textContent).join(' '),/вже сплачено|вже оплатили/);
 await ui.refresh({...paidOffer,sales_enabled:false,request:{state:'created',awaiting_receipt:true}});assert.equal(ui.link().href,paidOffer.status_url);
});

test('unknown request state falls back to status instead of creating another payment',async()=>{
 const ui=await offerFixture({...paidOffer,request:{state:'UNEXPECTED_INTERNAL'}});
 assert.equal(ui.link().href,paidOffer.status_url);assert.equal(ui.link().textContent,'Перевірити заявку');
 assert.doesNotMatch(offerNodes(ui.elements.radarPurchase).map(n=>n.textContent).join(' '),/UNEXPECTED_INTERNAL/);
});

test('returning after approval or network failure removes stale purchase controls',async()=>{
 const ui=await offerFixture(paidOffer);assert.ok(ui.link());
 await ui.refresh({...paidOffer,access_available:true});assert.equal(ui.elements.radarPurchase.hidden,true);assert.equal(ui.link(),undefined);
 await ui.refresh(paidOffer);assert.ok(ui.link());await ui.refresh(new Error('offline'));
 assert.equal(ui.elements.radarPurchase.hidden,true);assert.equal(ui.elements.settingsPurchase.hidden,true);assert.equal(ui.link(),undefined);
});

test('terms are rendered as text and native links remain safe when Telegram SDK is unavailable',async()=>{
 for(const options of [{sdk:false},{sdk:true,sdkThrows:true}]){
  const ui=await offerFixture({...paidOffer,terms:'<img src=x onerror=alert(1)> Умови'},options);
  assert.ok(offerNodes(ui.elements.radarPurchase).some(n=>n.textContent.includes('<img')));
  assert.ok(!offerNodes(ui.elements.radarPurchase).some(n=>n.tag==='img'));
  let prevented=false;ui.link().handlers.click({preventDefault(){prevented=true;}});assert.equal(prevented,false);
  assert.equal(ui.link().href,paidOffer.purchase_url);assert.equal(ui.link().rel,'noopener');assert.equal(ui.link().target,'_blank');assert.equal(ui.reads,1);
 }
});
