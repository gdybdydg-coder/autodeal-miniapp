/* Optional Playwright smoke: all hosts intercepted; synthetic data only. */
const {chromium}=require('playwright');
const fs=require('fs');
const path=require('path');
const root=path.resolve(__dirname,'..');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:390,height:844}});
    const row={code:'AD-TEST123456',name:'Тестовий клієнт',amount_minor:25000,days:30,state:'review',revision:1,user_id:111,username:null,created_at:1790922000,current_expiry:0,receipt_attached:false,history:[]};
    await page.addInitScript(()=>{window.Telegram={WebApp:{initData:'SIGNED-FIXTURE',ready(){}}};});
    await page.route('**/*',async route=>{
      const u=new URL(route.request().url());
      if(u.hostname==='telegram.org')return route.fulfill({body:'',contentType:'application/javascript'});
      if(u.hostname==='autodeal-api.onrender.com'){
        let data;
        if(u.pathname.endsWith('/notices'))data=[];
        else if(u.pathname.endsWith('/preview'))data={confirmation:'FIXTURE',code:row.code,name:row.name,user_id:111,days:30,estimated_expiry:1793514000,valid_until:1790922300};
        else if(u.pathname.endsWith('/confirm'))data={code:row.code,expires_at:1793514000};
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
    await page.goto('https://fixture.invalid/payment-review.html');
    await page.getByRole('button',{name:/AD-TEST/}).click();
    await page.getByLabel('Ідентифікатор рахунку',{exact:true}).fill('FIXTURE-ACCOUNT');
    await page.getByLabel('Номер банківської операції',{exact:true}).fill('FIXTURE-CREDIT');
    await page.getByLabel('Фактично зараховано, грн',{exact:true}).fill('250,00');
    await page.getByLabel('Я особисто перевірив зарахування',{exact:true}).check();
    await page.getByRole('button',{name:'✅ Підтвердити надходження і відкрити доступ',exact:true}).click();
    await page.getByRole('button',{name:'Так, надходження перевірено — відкрити доступ',exact:true}).waitFor();
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile horizontal overflow');
    if(process.argv[2])await page.screenshot({path:process.argv[2],fullPage:true});
    await page.getByRole('button',{name:'Так, надходження перевірено — відкрити доступ',exact:true}).click();
    await page.getByRole('heading',{name:'✅ Рішення збережено'}).waitFor();
    console.log('Browser fixture: queue → owner review → confirmation → result; mobile 390px no horizontal overflow; all network mocked.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
