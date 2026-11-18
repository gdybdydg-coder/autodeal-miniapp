/* Real local FastAPI/SQLite acceptance; synthetic signed Telegram user, external I/O blocked. */
'use strict';
const {chromium}=require('playwright');
const crypto=require('node:crypto');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const base=process.env.RADAR_PREVIEW_URL||'http://127.0.0.1:8793';
const artifacts=path.resolve(process.env.RADAR_PREVIEW_ARTIFACTS||'../radar-brands-preview-20261118');
function signed(){
 const fields={auth_date:String(Math.floor(Date.now()/1000)),user:JSON.stringify({id:111})};
 const key=crypto.createHmac('sha256','WebAppData').update('12345:RADAR_BROWSER_FIXTURE_ONLY').digest();
 fields.hash=crypto.createHmac('sha256',key).update(Object.keys(fields).sort().map(k=>k+'='+fields[k]).join('\n')).digest('hex');
 return new URLSearchParams(fields).toString();
}
(async()=>{
 const browser=await chromium.launch({headless:true});
 const checks=[],errors=[],external=[],apiRequests=[];let page;
 fs.mkdirSync(artifacts,{recursive:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block'});
  await context.addInitScript(({proof})=>{window.Telegram={WebApp:{initData:proof,ready(){},expand(){}}};},{proof:signed()});
  await context.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(url.origin===base)return route.continue();
   if(url.origin==='https://telegram.org')return route.fulfill({body:'',contentType:'application/javascript'});
   if(url.origin==='https://autodeal-api.onrender.com'){
    apiRequests.push({path:url.pathname,query:url.search,method:request.method()});
    const response=await route.fetch({url:base+url.pathname+url.search});return route.fulfill({response});
   }
   external.push(url.origin);return route.abort();
  });
  page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
  const modelCalls=()=>apiRequests.filter(r=>r.path==='/api/radars/catalog'&&r.query.includes('brand_id=')).length;
  async function dto(){const data=await page.evaluate(()=>window.AutoDealCloud.radars());assert.equal(data.radars.length,1);return data.radars[0];}
  async function save(){await page.locator('#saveRadar').click();await page.locator('#radarPage').waitFor({state:'visible'});}
  async function reopen(){await page.reload();await page.locator('article[data-slot="1"]').getByRole('button',{name:'Редагувати',exact:true}).click();await page.locator('#radarBrands label').first().waitFor();}
  await page.goto(base);await page.locator('.create-button').first().waitFor();assert.equal(await page.locator('.create-button').count(),3);
  await page.locator('.create-button').first().click();await page.locator('#radarBrands label').first().waitFor();
  await page.locator('#radarName').fill('Усі марки — локальна перевірка');await page.locator('#radarAllBrands').check();
  await page.locator('#nextRadarStep').click();await page.locator('#radarPrices button').filter({hasText:'5 000–10 000 $'}).click();
  await page.locator('#nextRadarStep').click();await page.locator('#nextRadarStep').click();await save();
  let row=await dto();const originalId=row.id;assert.equal(row.all_brands,true);assert.deepEqual(row.brands,[]);assert.equal(row.status,'paused');assert.equal(modelCalls(),0);
  checks.push('real_api_all_brands_create_persists_empty_wildcard_without_model_requests');
  await page.locator('article[data-slot="1"]').getByRole('button',{name:'Увімкнути',exact:true}).click();
  await page.locator('article[data-slot="1"]').getByRole('button',{name:'На паузу',exact:true}).waitFor();
  row=await dto();assert.equal(row.status,'active');assert.equal(row.all_brands,true);assert.deepEqual(row.brands,[]);
  await page.locator('article[data-slot="1"]').getByRole('button',{name:'На паузу',exact:true}).click();
  await page.locator('article[data-slot="1"]').getByRole('button',{name:'Увімкнути',exact:true}).waitFor();
  assert.equal((await dto()).status,'paused');checks.push('real_api_wildcard_resume_and_pause');
  await reopen();assert.equal(await page.locator('#radarAllBrands').isChecked(),true);assert.equal(modelCalls(),0);
  await page.locator('#radarName').fill('Усі марки — змінено');for(let i=0;i<3;i++)await page.locator('#nextRadarStep').click();await save();
  row=await dto();assert.equal(row.id,originalId);assert.equal(row.name,'Усі марки — змінено');assert.equal(row.all_brands,true);assert.deepEqual(row.brands,[]);assert.equal(modelCalls(),0);
  checks.push('real_api_edit_and_reopen_preserve_wildcard_and_identity');
  await reopen();await page.locator('#brandSearch').fill('BMW');await page.locator('#radarBrands label').filter({hasText:/^BMW$/}).click();
  await page.locator('#radarModels details[data-brand-id="9"] input[type="search"]').waitFor();
  await page.locator('#radarModels details[data-brand-id="9"] .radar-choices label').filter({hasText:/^3 Series$/}).click();
  assert.equal(await page.locator('#radarAllBrands').isChecked(),false);
  for(let i=0;i<3;i++)await page.locator('#nextRadarStep').click();await save();
  row=await dto();assert.equal(row.id,originalId);assert.equal(row.all_brands,false);assert.equal(row.brands.length,1);assert.equal(row.brands[0].brand_id,9);assert.deepEqual(row.brands[0].model_ids,[3219]);assert.equal(modelCalls(),1);
  checks.push('real_api_switch_to_specific_bmw_model_persists_one_selection');
  await reopen();await page.locator('#radarModels details[data-brand-id="9"] .radar-choices input:checked').waitFor();
  assert.equal(await page.locator('#radarAllBrands').isChecked(),false);
  await page.locator('#radarAllBrands').check();assert.equal(await page.locator('#radarModels details').count(),0);
  const beforeAllSave=modelCalls();for(let i=0;i<3;i++)await page.locator('#nextRadarStep').click();await save();
  row=await dto();assert.equal(row.id,originalId);assert.equal(row.all_brands,true);assert.deepEqual(row.brands,[]);assert.equal(row.price_category,3);assert.deepEqual(row.region_ids,[]);assert.equal(modelCalls(),beforeAllSave);
  await reopen();assert.equal(await page.locator('#radarAllBrands').isChecked(),true);assert.equal(modelCalls(),beforeAllSave);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
  checks.push('real_api_specific_reopen_switch_back_all_clears_models_without_expansion');
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  await page.screenshot({path:path.join(artifacts,'real-api-all-brands-mobile.png'),fullPage:true});
  const report={passed:true,checks,real_local_api_requests:apiRequests.length,model_catalog_requests:modelCalls(),
    all_mode_model_requests:0,external_requests:0,production_mutations:0,final_radar:{id:row.id,all_brands:row.all_brands,brands:row.brands,status:row.status,revision:row.revision,price_category:row.price_category}};
  fs.writeFileSync(path.join(artifacts,'real-api-browser-results.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }catch(error){
  fs.writeFileSync(path.join(artifacts,'real-api-browser-results.json'),JSON.stringify({passed:false,checks,errors,error:error.message,apiRequests,external},null,2));
  if(page)await page.screenshot({path:path.join(artifacts,'real-api-failure.png'),fullPage:true}).catch(()=>{});throw error;
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
