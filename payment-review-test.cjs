const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.value='';this.textContent='';this.checked=false;this.disabled=false;this.isConnected=true;}
  append(n){n.parent=this;this.children.push(n);}
  replaceChildren(...nodes){for(const n of this.children)n.isConnected=false;this.children=nodes;this.textContent='';}
  addEventListener(name,fn){this.handlers[name]=fn;}
  remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
  showModal(){this.open=true;}
  async click(){return this.handlers.click?.();}
}
function nodes(el){return [el,...el.children.flatMap(nodes)];}
async function fixture(handler,authenticated=true,search=''){
  const screen=new Element(),error=new Element(),body=new Element('body'),calls=[],links=[],revoked=[];
  const window={location:{search},URL:{createObjectURL:()=> 'blob:synthetic-receipt',revokeObjectURL:u=>revoked.push(u)},Telegram:{WebApp:{initData:authenticated?'SIGNED-SYNTHETIC':'',ready(){},openTelegramLink(u){links.push(u);}}}};
  const document={body,getElementById:id=>id==='screen'?screen:error,createElement:tag=>new Element(tag)};
  const fetch=async(url,opts)=>{calls.push({url,opts});const value=await handler(url,opts);return {ok:value.status===undefined||value.status===200,status:value.status||200,json:async()=>value.data??value,blob:async()=>({type:value.contentType||'image/png'})};};
  vm.runInNewContext(fs.readFileSync('payment-review.js','utf8'),{window,document,fetch,Date,URLSearchParams});
  await new Promise(r=>setImmediate(r));
  return {screen,error,body,calls,links,window,revoked,find:t=>nodes(screen).find(n=>n.tag==='button'&&n.textContent.includes(t)),
    field:t=>nodes(screen).find(n=>n.tag==='label'&&n.textContent===t)?.children[0]};
}
const counts={review:1,clarification:0,approved:0,rejected:0,created:0};
const row={code:'AD-123456789ABC',name:'<script>bad()</script>',amount_minor:25000,days:30,state:'review',revision:1,
  user_id:111,username:null,created_at:1790922000,received_at:1790922100,current_expiry:0,receipt_attached:true,history:[]};
const listing={items:[row],waiting:1,total:1,page:1,pages:1,counts};
const basic=url=>url.includes('/admin?')?listing:url.endsWith('/notices')?[]:url.endsWith('/receipt')?{contentType:'image/png'}:row;
test('no signed Telegram session means zero API calls',async()=>{
  const ui=await fixture(()=>assert.fail('network must not run'),false);
  assert.equal(ui.calls.length,0);assert.match(ui.error.textContent,/Telegram/);
});
test('full owner path uses authenticated receipt and approval without removed fields',async()=>{
  const ui=await fixture(async(url,opts)=>{
    if(url.endsWith('/notices'))return [{id:'n',kind:'client',state:'uncertain'}];
    if(url.endsWith('/preview')){
      assert.deepEqual(JSON.parse(opts.body),{code:row.code,revision:1,bank_verified:true});
      return {confirmation:'FIXTURE-TOKEN',code:row.code,name:row.name,user_id:111,days:30,estimated_expiry:1793514000};
    }
    if(url.endsWith('/confirm')){
      assert.deepEqual(JSON.parse(opts.body),{confirmation:'FIXTURE-TOKEN'});return {code:row.code,expires_at:1793514000};
    }
    return basic(url);
  });
  await ui.find(row.code).click();
  assert.ok(nodes(ui.screen).some(n=>n.textContent.includes('<script>bad()'))); // text, never HTML
  assert.equal(ui.find('Повторити відхилене'),undefined); // uncertain is NOT retryable
  assert.ok(nodes(ui.screen).some(n=>n.tag==='img'&&n.src==='blob:synthetic-receipt'));
  await ui.find('Відкрити у повному розмірі').click();
  assert.ok(nodes(ui.body).some(n=>n.tag==='dialog'&&n.open));
  assert.ok(nodes(ui.body).some(n=>n.tag==='img'&&n.alt.includes('повному розмірі')));
  for(const label of ['Рахунок зарахування (IBAN)','Номер банківської операції','Фактично зараховано, грн'])assert.equal(ui.field(label),undefined);
  ui.field('Я особисто перевірив зарахування').checked=true;
  await ui.find('Підтвердити оплату').click();assert.equal(ui.error.textContent,'');
  assert.ok(ui.find('Повернутися без підтвердження'));assert.deepEqual(ui.revoked,['blob:synthetic-receipt']);
  await ui.find('Так, надходження').click();
  assert.ok(nodes(ui.screen).some(n=>n.textContent.includes('Рішення збережено')));
  for(const c of ui.calls){assert.equal(c.opts.headers['X-Telegram-Init-Data'],'SIGNED-SYNTHETIC');assert.ok(!c.url.includes('SIGNED'));assert.equal(c.opts.cache,'no-store');}
});
test('server rejection clears revoked owner data and private receipt object URLs',async()=>{
  let revoked=false;
  const ui=await fixture(url=>revoked?{status:403,data:{detail:'owner_only'}}:basic(url),true,'?code='+row.code);
  revoked=true;await ui.find('Усі заявки').click();
  assert.equal(ui.screen.children.length,0);assert.match(ui.error.textContent,/власнику/);assert.deepEqual(ui.revoked,['blob:synthetic-receipt']);
});
test('all states, search and pagination use the server queue',async()=>{
  const ui=await fixture(()=>({...listing,pages:3,total:45}));
  await ui.find('Усі заявки').click();assert.ok(ui.calls.at(-1).url.includes('state=all'));
  ui.field('Номер заявки, ім’я, username або Telegram ID').value='AD-FIXTURE';
  await ui.find('Знайти').click();assert.ok(ui.calls.at(-1).url.includes('search=AD-FIXTURE'));
  await ui.find('Наступна').click();assert.ok(ui.calls.at(-1).url.includes('page=2'));
});
test('superseded status messages stay visible without a retry action',async()=>{
  const ui=await fixture(url=>url.endsWith('/notices')?[{id:'n',kind:'client',state:'superseded'}]:basic(url));
  await ui.find(row.code).click();
  assert.ok(nodes(ui.screen).some(n=>n.textContent.includes('Замінено новішим статусом заявки')));
  assert.equal(ui.find('Повторити відхилене'),undefined);
});
test('direct link displays receipt and expected plan amount, no obsolete form or external receipt URL',async()=>{
  const ui=await fixture(url=>url.endsWith('/notices')?[]:url.endsWith('/receipt')?{contentType:'image/jpeg'}:
    {...row,receiving_account:'SECRET-ACCOUNT',transfer_note:'OLD-DETAIL',receipt_url:'https://evil.invalid/file'},true,'?code='+row.code);
  assert.ok(ui.calls[0].url.endsWith('/admin/'+row.code));
  assert.ok(ui.calls.some(c=>c.url.endsWith('/admin/'+row.code+'/receipt')));
  assert.ok(ui.calls.every(c=>c.url.startsWith('https://autodeal-api.onrender.com/api/manual-payments/')));
  const text=nodes(ui.screen).map(n=>n.textContent).join(' ');
  assert.match(text,/До сплати за тарифом: 250/);assert.match(text,/Отримано:.*Київ/);assert.doesNotMatch(text,/SECRET-ACCOUNT|OLD-DETAIL|без username/);
  assert.equal(ui.calls.filter(c=>c.opts.method==='POST').length,0);
});
test('unchecked bank verification blocks approval before any mutation',async()=>{
  const ui=await fixture(basic,true,'?code='+row.code);
  await ui.find('Підтвердити оплату').click();assert.match(ui.error.textContent,/особисто перевірив/);
  assert.equal(ui.calls.filter(c=>c.opts.method==='POST').length,0);
});
test('server validation errors never ask for removed fields or show success',async()=>{
  const ui=await fixture(url=>url.endsWith('/preview')?{status:422,data:{detail:'Invalid request'}}:basic(url),true,'?code='+row.code);
  ui.field('Я особисто перевірив зарахування').checked=true;
  await ui.find('Підтвердити оплату').click();assert.match(ui.error.textContent,/Оновіть заявку/);
  assert.doesNotMatch(ui.error.textContent,/рахунок|операції|суму/);assert.equal(ui.find('Так, надходження'),undefined);
});
test('request another screenshot uses the same request and no extra form',async()=>{
  let body;
  const ui=await fixture((url,opts)=>{if(url.endsWith('/state')){body=JSON.parse(opts.body);return {...row,state:'clarification'};}return basic(url);},true,'?code='+row.code);
  await ui.find('Попросити інший скриншот').click();
  assert.deepEqual(body,{code:row.code,state:'clarification',note:'Надішліть, будь ласка, інший скриншот оплати.',revision:1});
});
test('legacy review without receipt remains visible and approvable',async()=>{
  const ui=await fixture(url=>url.endsWith('/notices')?[]:{...row,receipt_attached:false,received_at:undefined},true,'?code='+row.code);
  assert.ok(ui.find('Підтвердити оплату'));assert.equal(ui.calls.some(c=>c.url.endsWith('/receipt')),false);
  assert.ok(nodes(ui.screen).some(n=>n.textContent==='Скриншот ще не додано.'));
});
test('legacy PDF uses a private blob link and unsafe image types are not embedded',async()=>{
  const pdf=await fixture(url=>url.endsWith('/receipt')?{contentType:'application/pdf'}:basic(url),true,'?code='+row.code);
  assert.ok(nodes(pdf.screen).some(n=>n.tag==='a'&&n.href==='blob:synthetic-receipt'&&n.rel==='noopener'));
  const svg=await fixture(url=>url.endsWith('/receipt')?{contentType:'image/svg+xml'}:basic(url),true,'?code='+row.code);
  assert.ok(!nodes(svg.screen).some(n=>n.tag==='img'));assert.ok(!nodes(svg.screen).some(n=>n.tag==='a'));
});
test('receipt auth failure clears owner card instead of exposing cached private data',async()=>{
  const ui=await fixture(url=>url.endsWith('/receipt')?{status:403,data:{detail:'owner_only'}}:basic(url),true,'?code='+row.code);
  assert.equal(ui.screen.children.length,0);assert.match(ui.error.textContent,/власнику/);
  assert.equal(ui.calls.some(c=>c.url.endsWith('/notices')),false);
});
