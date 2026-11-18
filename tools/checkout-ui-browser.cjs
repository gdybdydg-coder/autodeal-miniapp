/* Mobile checkout UI with synthetic API/Telegram bridge; no purchase or real messages. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const repo=path.resolve(__dirname,'..');
const artifacts=path.resolve(repo,'../subscription-checkout-preview-20261118');
const origin='https://checkout-ui-fixture.invalid';
const assets=new Set(['index.html','style.css','radar.css','radar-ui.js','cloud-api.js','billing-ui.js','app-version.js','release.json']);
const buy='https://t.me/auto_deal_finder1_bot?start=buy_subscription';
const status='https://t.me/auto_deal_finder1_bot?start=subscribe';
let state={sales_enabled:true,paid_access_required:true,access_available:false,payment_method:'bank_manual',amount_uah:250,days:30,
 purchase_url:buy,status_url:status,request:null,telegram_id:111,can_manage_subscriptions:true,
 terms:'250 грн / 30 днів. Банківський переказ. Власник перевіряє фактичне зарахування. Натискання кнопки та квитанція не відкривають доступ автоматично. Без автосписань. При продовженні невикористані дні зберігаються.',terms_version:'20261002-card-v1'};
const checks=[],requests=[],external=[],errors=[];
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block'});
  await context.addInitScript(()=>{window.__checkoutOpens=[];window.Telegram={WebApp:{initData:'SYNTHETIC_ONLY',ready(){},expand(){},openTelegramLink(url){window.__checkoutOpens.push(url);}}};});
  await context.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(url.origin===origin){const asset=url.pathname.slice(1)||'index.html';if(!assets.has(asset)){external.push('unexpected asset');return route.abort();}
    return route.fulfill({body:fs.readFileSync(path.join(repo,asset)),contentType:{'.js':'application/javascript','.css':'text/css','.html':'text/html','.json':'application/json'}[path.extname(asset)]});}
   if(url.origin==='https://telegram.org')return route.fulfill({body:'',contentType:'application/javascript'});
   if(url.origin==='https://autodeal-api.onrender.com'){
    requests.push({path:url.pathname,method:request.method()});assert.equal(request.method(),'GET');
    if(url.pathname==='/api/billing/status')return route.fulfill({json:state});
    if(url.pathname==='/api/radars')return route.fulfill({json:{radars:[],limit:3,access:{active:state.access_available}}});
    if(url.pathname==='/api/notifications/status')return route.fulfill({json:{telegram_ready:true,available:true}});
    throw Error('Unexpected checkout API path');
   }
   external.push(url.origin);return route.abort();
  });
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  await page.goto(origin);await page.locator('#radarPurchase').waitFor({state:'visible'});
  assert.deepEqual(await page.evaluate(()=>window.__checkoutOpens),[]);
  assert.match(await page.locator('#radarPurchase').innerText(),/250 грн \/ 30 днів/);
  assert.match(await page.locator('#radarPurchase').innerText(),/після перевірки оплати власником/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
  fs.mkdirSync(artifacts,{recursive:true});await page.screenshot({path:path.join(artifacts,'checkout-dashboard-mobile.png'),fullPage:true});
  await page.locator('#radarPurchase summary').click();assert.equal(await page.locator('#radarPurchase details').getAttribute('open'),'');
  assert.ok((await page.locator('#radarPurchase details').innerText()).includes(state.terms));
  checks.push('inactive_offer_price_manual_confirmation_no_autocharge_terms_before_buy');
  await page.locator('#radarPurchase').getByRole('link',{name:'Купити підписку — 250 грн',exact:true}).click();
  assert.deepEqual(await page.evaluate(()=>window.__checkoutOpens),[buy]);
  assert.equal(new URL(page.url()).origin,origin);checks.push('explicit_buy_calls_telegram_bridge_once_without_api_mutation');
  await page.getByRole('button',{name:'Налаштування',exact:true}).click();await page.locator('#settingsPurchase').waitFor({state:'visible'});
  assert.equal(await page.locator('#accountTelegramId').innerText(),'Ваш Telegram ID: 111');assert.equal(await page.locator('#manageSubscriptionsLink').isVisible(),true);
  await page.locator('#settingsPurchase').getByRole('link',{name:'Купити підписку — 250 грн',exact:true}).click();
  assert.deepEqual(await page.evaluate(()=>window.__checkoutOpens),[buy,buy]);
  await page.screenshot({path:path.join(artifacts,'checkout-settings-mobile.png'),fullPage:true});
  checks.push('settings_offer_keeps_owner_management_and_own_telegram_id');
  state={...state,request:{state:'review',awaiting_receipt:false}};await page.reload();
  await page.locator('#radarPurchase').waitFor({state:'visible'});assert.match(await page.locator('#radarPurchase').innerText(),/Повторно не сплачуйте/);
  assert.equal(await page.locator('#radarPurchase').getByRole('link',{name:'Купити підписку — 250 грн',exact:true}).count(),0);
  await page.locator('#radarPurchase').getByRole('link',{name:'Перевірити оплату в боті',exact:true}).click();
  assert.deepEqual(await page.evaluate(()=>window.__checkoutOpens),[status]);checks.push('pending_review_shows_status_without_duplicate_purchase');
  state={...state,request:null,sales_enabled:false};await page.reload();await page.locator('.create-button').first().waitFor();
  await page.waitForFunction(()=>document.getElementById('billingStatus').textContent.includes('Продаж'));
  assert.equal(await page.locator('#radarPurchase').isVisible(),false);assert.equal(await page.locator('#settingsPurchase').isVisible(),false);
  checks.push('closed_sales_never_show_buy');
  state={...state,sales_enabled:true,access_available:true,expires_at:1890000000};await page.reload();await page.locator('.create-button').first().waitFor();
  await page.waitForFunction(()=>document.getElementById('billingStatus').textContent.includes('Доступ активний'));
  assert.equal(await page.locator('#radarPurchase').isVisible(),false);
  await page.getByRole('button',{name:'Налаштування',exact:true}).click();
  assert.equal(await page.locator('#subscriptionStatusLink').getAttribute('href'),status);checks.push('active_access_keeps_status_and_never_auto_resumes_radars');
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.ok(requests.every(r=>r.method==='GET'));
  const report={passed:true,checks,synthetic_api_reads:requests.length,real_api_requests:0,real_purchases:0,real_telegram_messages:0};
  fs.writeFileSync(path.join(artifacts,'browser-results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
