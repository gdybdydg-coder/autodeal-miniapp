/* Real Chromium DOM checks; all API responses are synthetic, external I/O blocked. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.RADAR_PREVIEW_URL || 'http://127.0.0.1:8791';
const artifacts = path.resolve(process.env.RADAR_PREVIEW_ARTIFACTS || '../radar-preview-20261118');
const attack = '<img src=x onerror="window.__xss=1">';
const radar = { id:'a'.repeat(32),slot:1,name:attack,status:'paused',revision:1,
  brands:[{brand_id:9,brand_name:'BMW '+attack,model_ids:[],models:[]}],price_category:3,
  regions:[{id:10,name:attack}],region_ids:[10],year_from:2000,year_to:2020,min_profit:10 };
const catalog = { brands:[{id:9,name:'BMW',models_available:true}],
  regions:[{id:10,name:'Київська область'},{id:1,name:'Вінницька область'}],year_min:1900,year_max:2027 };
const checks = [], calls = [], errors = [], blocked = [];
function response(url) {
  if(url.pathname==='/api/radars')return {radars:[radar],limit:3,access:{active:false}};
  if(url.pathname==='/api/radars/catalog')return url.searchParams.has('brand_id')
    ? {models:[{id:3219,brand_id:9,name:'3 Series'}]} : catalog;
  if(url.pathname==='/api/billing/status')return {paid_access_required:true,access_available:false,sales_enabled:false};
  if(url.pathname==='/api/notifications/status')return {telegram_ready:false,available:false};
  if(url.pathname.endsWith('/notices'))return [];
  if(url.pathname.startsWith('/api/manual-payments/admin/'))return {
    code:'AD-123456789ABC',name:attack,username:attack,user_id:111,owner_note:attack,
    amount_minor:25000,days:30,state:'unknown_internal',receipt_attached:false,
    created_at:1790922000,history:[{at:1790922000,action:'unknown_internal_action'}]};
  throw Error('Unexpected fixture API path '+url.pathname);
}
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext({viewport:{width:320,height:760}});
    await context.addInitScript(()=>{window.Telegram={WebApp:{initData:'SYNTHETIC_DOM_ONLY',ready(){},expand(){}}};});
    await context.route('**/*',async route=>{
      const url=new URL(route.request().url());
      if(url.origin===base)return route.continue();
      if(url.hostname==='telegram.org')return route.fulfill({body:'',contentType:'application/javascript'});
      if(url.hostname==='autodeal-api.onrender.com'){
        calls.push({path:url.pathname,method:route.request().method()});
        return route.fulfill({json:response(url)});
      }
      blocked.push(url.origin+url.pathname);return route.abort();
    });
    const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base);await page.locator('.create-button').first().waitFor();
    assert.equal(await page.locator('.radar-card h2').first().textContent(),attack);
    assert.equal(await page.locator('.radar-card img,.radar-card script').count(),0);
    await page.locator('article[data-slot="1"] button[aria-label]').click();
    assert.ok((await page.locator('#deleteRadarTitle').textContent()).includes(attack));
    assert.equal(await page.locator('#deleteRadarDialog img').count(),0);
    await page.locator('#cancelRadarDelete').click();checks.push('stored_names_brands_regions_dialogs_render_as_text');
    await page.getByRole('button',{name:'Увімкнути',exact:true}).click();
    assert.match(await page.locator('#radarError').innerText(),/активна підписка/);
    checks.push('unpaid_resume_sends_no_mutation');
    await page.locator('.create-button').first().click();await page.locator('#radarName').fill(attack);
    await page.locator('#radarBrands input').first().waitFor();await page.locator('#nextRadarStep').click();
    assert.match(await page.locator('#editorError').innerText(),/хоча б одну марку/);
    assert.equal(await page.locator('.editor-step[data-step="0"]').isVisible(),true);
    await page.locator('#radarBrands label').filter({hasText:/^BMW$/}).click();
    await page.locator('#radarModels input[type="search"]').waitFor();
    await page.locator('#nextRadarStep').click();await page.locator('#nextRadarStep').click();
    assert.match(await page.locator('#editorError').innerText(),/одну категорію ціни/);
    await page.locator('#radarPrices button').filter({hasText:'5 000–10 000 $'}).click();
    await page.locator('#radarRegions label').filter({hasText:'Київська'}).click();
    assert.equal(await page.locator('#radarAllUkraine').isChecked(),false);
    await page.locator('#radarAllUkraine').check();assert.equal(await page.locator('#radarRegions input:checked').count(),0);
    await page.locator('#nextRadarStep').click();
    assert.equal(await page.locator('#radarYearTo option[value="1999"]').isDisabled(),true);
    await page.locator('#radarYearTo').evaluate(el=>{el.value='1999';el.dispatchEvent(new Event('change',{bubbles:true}));});
    await page.locator('#nextRadarStep').click();assert.match(await page.locator('#editorError').innerText(),/коректні роки/);
    await page.locator('#radarYearTo').selectOption('2020');
    for(const percent of [50,5])await page.locator('#radarProfits button').filter({hasText:new RegExp('^'+percent+'%$')}).click();
    assert.equal(await page.locator('#radarProfits button[aria-pressed="true"]').count(),1);
    await page.locator('#nextRadarStep').click();assert.equal(await page.locator('#radarReview h3').textContent(),attack);
    assert.equal(await page.locator('#radarReview img').count(),0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
    checks.push('mobile_invalid_brand_price_year_blocked_and_regions_profit_exclusive');
    assert.equal(calls.filter(call=>call.method!=='GET').length,0);
    await page.goto(base+'/payment-review.html?code=AD-123456789ABC');
    await page.getByRole('heading',{name:'💳 Заявка на підписку AutoDeal',exact:true}).waitFor();
    assert.equal(await page.locator('#screen img,#screen script').count(),0);
    assert.match(await page.locator('#screen').innerText(),/Статус заявки потребує перевірки/);
    assert.ok((await page.locator('#screen').innerText()).includes(attack));
    assert.doesNotMatch(await page.locator('#screen').innerText(),/unknown_internal/);
    assert.equal(await page.evaluate(()=>window.__xss),undefined);
    checks.push('owner_name_username_notes_are_text_unknown_states_are_ukrainian');
    assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);
    assert.equal(fs.existsSync(path.resolve(__dirname,'fixtures/legacy-search-shell.html')),false);
    checks.push('no_javascript_errors_external_requests_or_executable_legacy_fixture');
    fs.mkdirSync(artifacts,{recursive:true});
    const result={passed:true,checks,synthetic_api_requests:calls.length,real_api_requests:0,external_requests:0};
    fs.writeFileSync(path.join(artifacts,'browser-security-results.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
