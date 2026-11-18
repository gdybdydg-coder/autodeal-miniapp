/* Real Chromium UI, synthetic stateful API, no provider calls or real mutations. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const repo=path.resolve(__dirname,'..');
const artifacts=path.resolve(repo,'../radar-brands-preview-20261118');
const origin='https://radar-brands-fixture.invalid';
const assets=new Set(['index.html','style.css','radar.css','radar-ui.js','cloud-api.js','billing-ui.js','app-version.js','release.json']);
// Deliberately not alphabetic: the page must preserve the API's popularity order.
const brands=[{id:84,name:'Volkswagen'},{id:6,name:'Audi'},{id:9,name:'BMW'},
 ...Array.from({length:394},(_,i)=>({id:1000+i,name:'Марка '+String(i+1).padStart(3,'0')}))];
const catalog={brands,regions:[{id:10,name:'Київська область'}],year_min:1900,year_max:2027};
const modelMap={9:[{id:3219,brand_id:9,name:'3 Series'},{id:2319,brand_id:9,name:'5 Series'}],6:[{id:49,brand_id:6,name:'A6'}]};
let saved=null;const requests=[],checks=[],errors=[],blocked=[];
function response(url,method,body){
 if(url.pathname==='/api/billing/status')return {paid_access_required:true,access_available:true,sales_enabled:false};
 if(url.pathname==='/api/notifications/status')return {telegram_ready:true,available:true};
 if(url.pathname==='/api/radars/catalog')return url.searchParams.has('brand_id')?{models:modelMap[Number(url.searchParams.get('brand_id'))]||[]}:catalog;
 if(url.pathname==='/api/radars'&&method==='GET')return {radars:saved?[saved]:[],limit:3,access:{active:true}};
 if((url.pathname==='/api/radars'&&method==='POST')||(url.pathname==='/api/radars/'+'a'.repeat(32)&&method==='PUT')){
  saved={...body,id:'a'.repeat(32),slot:1,status:'paused',revision:(saved?.revision||0)+1,
   brands:body.brands.map(b=>({...b,brand_name:brands.find(v=>v.id===b.brand_id).name,
    models:b.model_ids.map(id=>({model_id:id,name:modelMap[b.brand_id].find(m=>m.id===id).name}))})),regions:[]};
  return saved;
 }
 throw Error('Unexpected synthetic endpoint '+url.pathname);
}
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block'});
  await context.addInitScript(()=>{window.Telegram={WebApp:{initData:'SYNTHETIC_ONLY',ready(){},expand(){}}};});
  await context.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url());
   if(url.origin===origin){const file=url.pathname.slice(1)||'index.html';if(!assets.has(file)){blocked.push('asset');return route.abort();}
    return route.fulfill({body:fs.readFileSync(path.join(repo,file)),contentType:{'.js':'application/javascript','.css':'text/css','.html':'text/html','.json':'application/json'}[path.extname(file)]});}
   if(url.hostname==='telegram.org')return route.fulfill({body:'',contentType:'application/javascript'});
   if(url.hostname==='autodeal-api.onrender.com'){requests.push({path:url.pathname,query:url.search,method:req.method(),body:req.postData()?JSON.parse(req.postData()):null});
    return route.fulfill({json:response(url,req.method(),req.postData()?JSON.parse(req.postData()):null)});}
   blocked.push(url.origin);return route.abort();
  });
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  const modelCalls=()=>requests.filter(r=>r.path==='/api/radars/catalog'&&r.query.includes('brand_id=')).length;
  await page.goto(origin);await page.locator('.create-button').first().click();await page.locator('#radarBrands label').first().waitFor();
  assert.deepEqual((await page.locator('#radarBrands label').allTextContents()).slice(0,3),['Volkswagen','Audi','BMW']);
  assert.equal(await page.locator('#radarBrands label').count(),397);checks.push('catalog_popularity_order_preserved');
  await page.locator('#radarName').fill('Будь-яка марка');await page.locator('#radarAllBrands').check();
  assert.equal(await page.locator('#radarBrands input:checked').count(),0);assert.equal(await page.locator('#radarModels details').count(),0);assert.equal(modelCalls(),0);
  fs.mkdirSync(artifacts,{recursive:true});await page.screenshot({path:path.join(artifacts,'all-brands-mobile.png'),fullPage:true});
  await page.locator('#nextRadarStep').click();await page.locator('#radarPrices button').filter({hasText:'5 000–10 000 $'}).click();
  await page.locator('#nextRadarStep').click();await page.locator('#nextRadarStep').click();
  assert.match(await page.locator('#radarReview').innerText(),/Усі марки · усі моделі/);
  await page.locator('#saveRadar').click();await page.locator('#radarPage').waitFor({state:'visible'});
  assert.equal(saved.all_brands,true);assert.deepEqual(saved.brands,[]);assert.equal(modelCalls(),0);
  assert.match(await page.locator('article[data-slot="1"]').innerText(),/Усі марки · усі моделі/);
  checks.push('all_mode_create_review_save_uses_empty_wildcard_without_397_model_calls');
  await page.reload();await page.locator('article[data-slot="1"]').getByRole('button',{name:'Редагувати',exact:true}).click();
  await page.locator('#radarBrands label').first().waitFor();assert.equal(await page.locator('#radarAllBrands').isChecked(),true);assert.equal(modelCalls(),0);
  await page.locator('#brandSearch').fill('bm');assert.deepEqual(await page.locator('#radarBrands label').allTextContents(),['BMW']);
  assert.equal(await page.locator('#radarAllBrands').isChecked(),true);assert.equal(modelCalls(),0);
  await page.locator('#radarBrands label').filter({hasText:/^BMW$/}).click();
  await page.locator('#radarModels details[data-brand-id="9"] input[type="search"]').waitFor();
  assert.equal(await page.locator('#radarAllBrands').isChecked(),false);assert.equal(modelCalls(),1);
  await page.locator('#radarModels .radar-choices label').filter({hasText:/^3 Series$/}).click();
  await page.locator('#brandSearch').fill('no matching brand');assert.equal(await page.locator('#brandSearchEmpty').isVisible(),true);
  assert.equal(await page.locator('#radarModels .radar-choices input:checked').count(),1);
  await page.locator('#radarAllBrands').check();assert.equal(await page.locator('#radarModels details').count(),0);
  await page.locator('#brandSearch').fill('BMW');await page.locator('#radarBrands label').click();
  await page.locator('#radarModels .radar-choices label').first().waitFor();
  assert.equal(await page.locator('#radarModels .radar-choices input:checked').count(),0);assert.equal(modelCalls(),1);
  await page.locator('#radarModels .radar-choices label').filter({hasText:/^3 Series$/}).click();
  checks.push('edit_reopen_search_switches_clear_stale_models_and_fetch_only_selected_brand');
  for(let i=0;i<3;i++)await page.locator('#nextRadarStep').click();
  await page.locator('#saveRadar').click();await page.locator('#radarPage').waitFor({state:'visible'});
  assert.equal(saved.all_brands,false);assert.deepEqual(saved.brands.map(b=>({brand_id:b.brand_id,model_ids:b.model_ids})),[{brand_id:9,model_ids:[3219]}]);
  await page.reload();await page.locator('article[data-slot="1"]').getByRole('button',{name:'Редагувати',exact:true}).click();
  await page.locator('#radarModels .radar-choices input:checked').waitFor();assert.equal(await page.locator('#radarAllBrands').isChecked(),false);
  await page.locator('#radarAllBrands').check();await page.locator('#radarAllBrands').uncheck();await page.locator('#nextRadarStep').click();
  assert.match(await page.locator('#editorError').innerText(),/хоча б одну марку/);
  await page.locator('#radarAllBrands').check();for(let i=0;i<3;i++)await page.locator('#nextRadarStep').click();
  await page.locator('#saveRadar').click();await page.locator('#radarPage').waitFor({state:'visible'});
  assert.equal(saved.all_brands,true);assert.deepEqual(saved.brands,[]);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
  await page.screenshot({path:path.join(artifacts,'all-brands-dashboard-mobile.png'),fullPage:true});
  checks.push('specific_save_reopen_and_switch_back_all_preserve_single_radar_other_filters');
  assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);
  const report={passed:true,checks,synthetic_api_requests:requests.length,model_catalog_requests:modelCalls(),real_api_requests:0,real_mutations:0};
  fs.writeFileSync(path.join(artifacts,'browser-results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
