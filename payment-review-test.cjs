const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.value='';this.textContent='';this.checked=false;this.disabled=false;}
  append(n){this.children.push(n);}
  replaceChildren(...nodes){this.children=nodes;this.textContent='';}
  addEventListener(name,fn){this.handlers[name]=fn;}
  async click(){return this.handlers.click?.();}
}
function nodes(el){return [el,...el.children.flatMap(nodes)];}
async function fixture(handler,authenticated=true,search=''){
  const screen=new Element(),error=new Element(),calls=[],links=[];
  const window={location:{search},Telegram:{WebApp:{initData:authenticated?'SIGNED-SYNTHETIC':'',ready(){},openTelegramLink(u){links.push(u);}}}};
  const document={getElementById:id=>id==='screen'?screen:error,createElement:tag=>new Element(tag)};
  const fetch=async(url,opts)=>{calls.push({url,opts});const value=await handler(url,opts);return {ok:value.status===undefined||value.status===200,status:value.status||200,json:async()=>value.data??value};};
  vm.runInNewContext(fs.readFileSync('payment-review.js','utf8'),{window,document,fetch,Date,URLSearchParams});
  await new Promise(r=>setImmediate(r));
  return {screen,error,calls,links,window,find:t=>nodes(screen).find(n=>n.tag==='button'&&n.textContent.includes(t)),
    field:t=>nodes(screen).find(n=>n.tag==='label'&&n.textContent===t)?.children[0]};
}
const counts={review:1,clarification:0,approved:0,rejected:0,created:0};
const row={code:'AD-123456789ABC',name:'<script>bad()</script>',amount_minor:25000,days:30,state:'review',revision:1,
  user_id:111,username:null,created_at:1790922000,current_expiry:0,receipt_attached:true,history:[]};
const listing={items:[row],waiting:1,total:1,page:1,pages:1,counts};
test('no signed Telegram session means zero API calls',async()=>{
  const ui=await fixture(()=>assert.fail('network must not run'),false);
  assert.equal(ui.calls.length,0);assert.match(ui.error.textContent,/Telegram/);
});
test('full owner path shows bank review, confirmation and stored expiry; no identity in body',async()=>{
  const ui=await fixture(async(url,opts)=>{
    if(url.includes('/admin?'))return listing;
    if(url.endsWith('/notices'))return [{id:'n',kind:'client',state:'uncertain'}];
    if(url.endsWith('/admin/'+row.code))return row;
    if(url.endsWith('/preview')){
      const body=JSON.parse(opts.body);assert.equal(body.actual_amount_minor,25000);assert.equal(body.bank_verified,true);
      assert.equal(body.user_id,undefined);return {confirmation:'FIXTURE-TOKEN',code:row.code,name:row.name,user_id:111,days:30,estimated_expiry:1793514000,valid_until:1790922300};
    }
    if(url.endsWith('/confirm'))return {code:row.code,expires_at:1793514000};
    assert.fail(url);
  });
  await ui.find(row.code).click();
  assert.ok(nodes(ui.screen).some(n=>n.textContent.includes('<script>bad()'))); // rendered as text
  assert.equal(ui.find('Повторити відхилене'),undefined); // uncertain is NOT retryable
  await ui.find('Відкрити квитанцію').click();assert.match(ui.links[0],/paymentreceipt_AD-/);
  ui.field('Рахунок зарахування (IBAN)').value='FIXTURE-ACCOUNT';ui.field('Номер банківської операції').value='FIXTURE-OP';
  ui.field('Фактично зараховано, грн').value='250,00';ui.field('Я особисто перевірив зарахування').checked=true;
  await ui.find('Підтвердити надходження').click();assert.equal(ui.error.textContent,'');
  assert.ok(ui.find('Повернутися без підтвердження'));
  await ui.find('Так, надходження').click();
  assert.ok(nodes(ui.screen).some(n=>n.textContent.includes('Рішення збережено')));
  for(const c of ui.calls){assert.equal(c.opts.headers['X-Telegram-Init-Data'],'SIGNED-SYNTHETIC');assert.ok(!c.url.includes('SIGNED'));}
});
test('server rejection never shows successful access and clears revoked owner data',async()=>{
  let revoked=false;
  const ui=await fixture(()=>revoked?{status:403,data:{detail:'owner_only'}}:listing);
  revoked=true;await ui.find('Оновити').click();
  assert.equal(ui.screen.children.length,0);assert.match(ui.error.textContent,/власнику/);
});
test('all states, search and pagination use the server queue',async()=>{
  const ui=await fixture(()=>({...listing,pages:3,total:45}));
  await ui.find('Усі заявки').click();assert.ok(ui.calls.at(-1).url.includes('state=all'));
  ui.field('Номер заявки, ім’я, username або Telegram ID').value='AD-FIXTURE';
  await ui.find('Знайти').click();assert.ok(ui.calls.at(-1).url.includes('search=AD-FIXTURE'));
  await ui.find('Наступна').click();assert.ok(ui.calls.at(-1).url.includes('page=2'));
});
test('superseded status messages stay visible in history without a retry action',async()=>{
  const ui=await fixture(url=>url.includes('/admin?')?listing:url.endsWith('/notices')?
    [{id:'n',kind:'client',state:'superseded'}]:row);
  await ui.find(row.code).click();
  assert.ok(nodes(ui.screen).some(n=>n.textContent.includes('Замінено новішим статусом заявки')));
  assert.equal(ui.find('Повторити відхилене'),undefined);
});
test('request link opens the owner card directly and uses the configured receiving account',async()=>{
  const ui=await fixture(url=>url.endsWith('/notices')?[]:{...row,receiving_account:'FIXTURE-ACCOUNT'},true,'?code='+row.code);
  assert.ok(ui.calls[0].url.endsWith('/admin/'+row.code));
  assert.equal(ui.field('Рахунок зарахування (IBAN)').value,'FIXTURE-ACCOUNT');
  assert.equal(ui.field('Рахунок зарахування (IBAN)').readOnly,true);
  const all=nodes(ui.screen);
  assert.ok(all.indexOf(ui.find('Підтвердити надходження'))<all.findIndex(n=>n.textContent==='Історія'));
  assert.equal(ui.calls.filter(c=>c.opts.method==='POST').length,0);
});
test('missing bank operation and unchecked verification are explained before any mutation',async()=>{
  const ui=await fixture(url=>url.endsWith('/notices')?[]:{...row,receiving_account:'FIXTURE-ACCOUNT'},true,'?code='+row.code);
  ui.field('Фактично зараховано, грн').value='250';
  await ui.find('Підтвердити надходження').click();
  assert.match(ui.error.textContent,/Номер банківської операції/);
  ui.field('Номер банківської операції').value='FIXTURE-CREDIT';
  await ui.find('Підтвердити надходження').click();
  assert.match(ui.error.textContent,/особисто перевірив/);
  assert.equal(ui.calls.filter(c=>c.opts.method==='POST').length,0);
});
test('server validation errors explain required fields without a false success',async()=>{
  const ui=await fixture(url=>url.endsWith('/preview')?{status:422,data:{detail:'Invalid request'}}:
    url.endsWith('/notices')?[]:{...row,receiving_account:'FIXTURE-ACCOUNT'},true,'?code='+row.code);
  ui.field('Номер банківської операції').value='FIXTURE-CREDIT';
  ui.field('Фактично зараховано, грн').value='250';
  ui.field('Я особисто перевірив зарахування').checked=true;
  await ui.find('Підтвердити надходження').click();
  assert.match(ui.error.textContent,/номер банківської операції/);
  assert.equal(ui.find('Так, надходження'),undefined);
});
