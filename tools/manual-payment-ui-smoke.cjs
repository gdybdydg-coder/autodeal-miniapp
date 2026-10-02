/* Optional Playwright smoke: all hosts intercepted; synthetic data only. */
const {chromium}=require('playwright');
const fs=require('fs');
const path=require('path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:390,height:844}});
    const row={code:'AD-123456789ABC',name:'Тестовий клієнт',amount_minor:25000,days:30,state:'review',revision:1,user_id:111,username:null,created_at:1790922000,received_at:1790922100,current_expiry:0,receipt_attached:true,receipt_kind:'photo',history:[]};
    const receipt=await page.evaluate(()=>{
      const c=document.createElement('canvas');c.width=1000;c.height=1400;const x=c.getContext('2d');
      x.fillStyle='#f2f6f8';x.fillRect(0,0,1000,1400);x.fillStyle='#18382f';x.font='bold 54px sans-serif';x.fillText('ТЕСТОВИЙ СКРИНШОТ',70,130);
      x.fillStyle='#597268';x.font='36px sans-serif';x.fillText('Не є підтвердженням платежу',70,205);
      x.fillStyle='#18382f';x.font='bold 90px sans-serif';x.fillText('250 грн',70,430);x.font='40px sans-serif';x.fillText('Демонстраційні дані',70,550);
      x.fillStyle='#70847e';x.font='30px sans-serif';x.fillText('Без банківських реквізитів',70,625);x.fillText('та персональних даних',70,675);return c.toDataURL('image/png').split(',')[1];
    });
    const calls=[];
    await page.addInitScript(()=>{window.Telegram={WebApp:{initData:'SIGNED-FIXTURE',ready(){}}};});
    await page.route('**/*',async route=>{
      const request=route.request(),u=new URL(request.url());
      if(u.hostname==='telegram.org')return route.fulfill({body:'',contentType:'application/javascript'});
      if(u.hostname==='autodeal-api.onrender.com'){
        calls.push({url:u.href,method:request.method(),body:request.postDataJSON(),headers:request.headers()});
        assert.equal(request.headers()['x-telegram-init-data'],'SIGNED-FIXTURE');assert.ok(!u.href.includes('SIGNED'));
        if(u.pathname.endsWith('/receipt'))return route.fulfill({body:Buffer.from(receipt,'base64'),contentType:'image/png',headers:{'Access-Control-Allow-Origin':'*','Cache-Control':'no-store'}});
        let data;
        if(u.pathname.endsWith('/notices'))data=[];
        else if(u.pathname.endsWith('/preview')){
          assert.deepEqual(request.postDataJSON(),{code:row.code,revision:1,bank_verified:true});
          data={confirmation:'FIXTURE',code:row.code,name:row.name,user_id:111,days:30,estimated_expiry:1793514000,valid_until:1790922300};
        }
        else if(u.pathname.endsWith('/confirm')){assert.deepEqual(request.postDataJSON(),{confirmation:'FIXTURE'});data={code:row.code,expires_at:1793514000};}
        else if(u.pathname.endsWith('/'+row.code))data=row;
        else data={items:[row],waiting:1,total:1,page:1,pages:1,counts:{review:1,clarification:0,approved:0,rejected:0,created:0}};
        return route.fulfill({json:data,headers:{'Access-Control-Allow-Origin':'*'}});
      }
      if(u.hostname==='fixture.invalid'){
        const file=u.pathname.slice(1)||'payment-review.html';
        if(!['payment-review.html','payment-review.js','payment-review.css'].includes(file))return route.abort();
        return route.fulfill({body:fs.readFileSync(path.join(root,file)),contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'application/javascript':'text/html'});
      }
      throw Error('Unapproved browser request '+u.hostname);
    });
    await page.goto('https://fixture.invalid/payment-review.html?code='+row.code);
    await page.getByRole('img',{name:'Скриншот оплати від клієнта',exact:true}).waitFor();
    assert.equal(await page.locator('input:not([type=checkbox]),textarea').count(),0);
    assert.match(await page.getByRole('img',{name:'Скриншот оплати від клієнта',exact:true}).getAttribute('src'),/^blob:/);
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile horizontal overflow');
    if(process.argv[2])await page.screenshot({path:process.argv[2],fullPage:true});
    await page.getByRole('button',{name:'🔍 Відкрити у повному розмірі',exact:true}).click();
    const full=page.getByRole('img',{name:'Скриншот оплати у повному розмірі',exact:true});await full.waitFor();
    assert.equal(await full.evaluate(img=>img.naturalWidth),1000);
    assert.equal(await full.evaluate(img=>img.clientWidth),1000);
    await page.getByRole('button',{name:'✕ Закрити',exact:true}).click();
    await page.getByRole('button',{name:'✅ Підтвердити оплату й відкрити доступ',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'особисто перевірив'}).waitFor();
    assert.equal(calls.filter(c=>c.method==='POST').length,0);
    await page.getByLabel('Я особисто перевірив зарахування',{exact:true}).check();
    await page.getByRole('button',{name:'✅ Підтвердити оплату й відкрити доступ',exact:true}).click();
    await page.getByRole('button',{name:'Так, надходження перевірено — відкрити доступ',exact:true}).waitFor();
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Confirmation mobile horizontal overflow');
    await page.getByRole('button',{name:'Так, надходження перевірено — відкрити доступ',exact:true}).click();
    await page.getByRole('heading',{name:'✅ Рішення збережено'}).waitFor();
    console.log('Browser fixture: authenticated full-resolution receipt → owner bank verification → field-free preview → confirmation → stored expiry; mobile 390px no horizontal overflow; all network mocked.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e.stack||e.message);process.exitCode=1;});
