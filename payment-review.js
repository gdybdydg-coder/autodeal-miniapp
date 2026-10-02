/* Owner-only Mini App. Authentication stays in memory; no tokens in URLs/storage. */
(function(root) {
  'use strict';
  const api='https://autodeal-api.onrender.com/api/manual-payments';
  const labels={review:'Очікують перевірки',clarification:'Потрібен інший скриншот',approved:'Підтверджені',rejected:'Відхилені',created:'Створені',all:'Усі заявки'};
  const status={review:'⏳ Очікує перевірки',clarification:'📸 Очікуємо інший скриншот',approved:'✅ Підтверджено',rejected:'❌ Відхилено',created:'Очікуємо оплату'};
  const screen=document.getElementById('screen'),error=document.getElementById('error');
  const date=n=>n?new Date(n*1000).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'})+' (Київ)':'немає';
  const money=n=>(n/100).toLocaleString('uk-UA',{minimumFractionDigits:2})+' грн';
  let state='review',page=1,search='',receiptUrls=[],viewer=null,viewRevision=0;
  const el=(tag,value,parent=screen)=>{const n=document.createElement(tag);if(value!=null)n.textContent=value;parent.append(n);return n;};
  const messages={owner_only:'Цей розділ доступний лише власнику.',manual_review_disabled:'Черга ручної оплати ще не ввімкнена на сервері.',
    owner_configuration_unavailable:'Потрібна перевірка налаштувань власника.',bank_credit_already_used:'Цей платіж уже підтверджено. Оновіть заявку.',
    request_changed:'Заявка змінилася. Відкрийте її ще раз.',request_or_access_changed:'Заявка або строк доступу змінилися. Перевірте картку ще раз.',
    explicit_owner_verification_required:'Підтвердіть, що особисто перевірили фактичне зарахування.',
    'Invalid request':'Не вдалося перевірити підтвердження. Оновіть заявку та спробуйте ще раз.',
    payment_storage_unavailable_do_not_pay_again:'Тимчасова помилка збереження. Не вимагайте повторної оплати. Оновіть статус заявки.'};
  function clear(){
    viewRevision++;
    viewer?.remove();viewer=null;
    receiptUrls.forEach(url=>root.URL.revokeObjectURL(url));receiptUrls=[];
    screen.replaceChildren();
  }
  async function authorized(path,body){
    const initData=root.Telegram?.WebApp?.initData;
    if(!initData)throw Error('Відкрийте цей розділ кнопкою /payments у Telegram.');
    const r=await fetch(api+path,{method:body===undefined?'GET':'POST',cache:'no-store',credentials:'omit',
      headers:{'X-Telegram-Init-Data':initData,...(body===undefined?{}:{'Content-Type':'application/json'})},
      body:body===undefined?undefined:JSON.stringify(body)});
    if(!r.ok){
      if(r.status===401||r.status===403)clear();
      const data=await r.json().catch(()=>({}));
      throw Error(messages[data.detail]||(r.status===401?'Сесію завершено. Відкрийте розділ знову в Telegram.':'Не вдалося виконати дію. Оновіть статус заявки.'));
    }
    return r;
  }
  async function request(path,body){return (await authorized(path,body)).json();}
  const run=fn=>async()=>{error.textContent='';try{await fn();}catch(e){error.textContent=e.message;error.scrollIntoView?.({block:'center'});}};
  function button(title,fn,parent=screen,primary=false){
    const b=el('button',title,parent);b.type='button';if(primary)b.className='primary';
    b.addEventListener('click',run(async()=>{if(b.disabled)return;b.disabled=true;try{await fn();}finally{b.disabled=false;}}));return b;
  }
  function field(title,type='text',parent=screen){
    const l=el('label',title,parent),input=el(type==='textarea'?'textarea':'input',null,l);
    if(type!=='textarea')input.type=type;input.maxLength=type==='textarea'?500:120;return input;
  }
  function fullReceipt(url){
    viewer?.remove();viewer=el('dialog',null,document.body);viewer.className='receipt-viewer';
    const close=button('✕ Закрити',()=>{viewer?.remove();viewer=null;},viewer);close.className='receipt-close';
    const image=el('img',null,viewer);image.src=url;image.alt='Скриншот оплати у повному розмірі';
    viewer.addEventListener('close',()=>{viewer?.remove();viewer=null;});viewer.showModal();
  }
  async function receipt(c,box){
    if(!c.receipt_attached){el('p','Скриншот ще не додано.',box);return;}
    const loading=el('p','Завантажуємо скриншот…',box);
    let r;
    try{r=await authorized('/admin/'+encodeURIComponent(c.code)+'/receipt');}
    catch(e){loading.textContent='Не вдалося завантажити скриншот. Оновіть заявку.';throw e;}
    const blob=await r.blob();
    if(!['image/jpeg','image/png','image/webp','application/pdf'].includes(blob.type)){
      loading.textContent='Формат квитанції недоступний для перегляду.';return;
    }
    if(!box.isConnected)return;
    const url=root.URL.createObjectURL(blob);receiptUrls.push(url);box.replaceChildren();
    if(blob.type==='application/pdf'){
      const link=el('a','📎 Відкрити квитанцію PDF',box);link.href=url;link.target='_blank';link.rel='noopener';return;
    }
    const image=el('img',null,box);image.src=url;image.alt='Скриншот оплати від клієнта';image.className='receipt-image';
    button('🔍 Відкрити у повному розмірі',()=>fullReceipt(url),box);
  }
  async function queue(){
    const q=await request('/admin?'+new URLSearchParams({state,page,search}));clear();
    el('h2',q.waiting+' очікують перевірки');
    if(!q.items.length)el('p','У цій вибірці заявок немає.');
    q.items.forEach(r=>{const b=button(r.name+' · '+money(r.amount_minor)+' · '+r.code,()=>card(r.code));b.className='card';});
    const filters=el('div');filters.className='queue-filters';
    Object.keys(labels).forEach(s=>button(labels[s]+(s==='all'?'':' · '+(q.counts[s]||0)),async()=>{state=s;page=1;await queue();},filters));
    const input=field('Номер заявки, ім’я, username або Telegram ID');input.value=search;
    button('Знайти',async()=>{search=input.value;page=1;await queue();});
    el('p','Сторінка '+q.page+' / '+q.pages+' · знайдено '+q.total);
    if(page>1)button('← Попередня',async()=>{page--;await queue();});
    if(page<q.pages)button('Наступна →',async()=>{page++;await queue();});
    button('Оновити',queue);
  }
  function approval(c,code){
    el('p','Перевірте надходження '+money(c.amount_minor)+' у своєму банку. Скриншот сам по собі не підтверджує оплату.');
    const verified=field('Я особисто перевірив зарахування','checkbox');
    button('✅ Підтвердити оплату й відкрити доступ',async()=>{
      if(!verified.checked)throw Error('Позначте «Я особисто перевірив зарахування» після звірки з банком.');
      const p=await request('/admin/preview',{code,revision:c.revision,bank_verified:true});
      clear();el('h2','Підтвердити доступ?');el('p',p.name+' · ID '+p.user_id+' · '+p.code);
      el('p',p.days+' днів. Доступ буде до '+date(p.estimated_expiry));el('p','Невикористані дні збережуться.');
      button('Так, надходження перевірено — відкрити доступ',async()=>{
        const result=await request('/admin/confirm',{confirmation:p.confirmation});
        clear();el('h2','✅ Рішення збережено');el('p','Доступ до '+date(result.expires_at));
        button('Переглянути заявку',()=>card(code));button('📋 Усі заявки',allRequests);
      },screen,true);
      button('Повернутися без підтвердження',()=>card(code));root.scrollTo?.(0,0);
    },screen,true);
  }
  async function card(code){
    const c=await request('/admin/'+encodeURIComponent(code));clear();
    const thisView=viewRevision;
    const box=el('div');box.className='receipt';
    el('h2','💳 Заявка на підписку AutoDeal');
    const details=el('div');details.className='request-details';
    el('p','👤 Клієнт: '+c.name+(c.username?' · @'+c.username:''),details);
    el('p','🆔 Telegram ID: '+c.user_id,details);el('p','🧾 Заявка: '+c.code,details);
    el('p','💰 До сплати за тарифом: '+money(c.amount_minor),details);el('p','📅 Термін: '+c.days+' днів',details);
    el('p','🕒 Отримано: '+date(c.received_at||c.created_at),details);el('p',status[c.state]||c.state,details);
    if(c.current_expiry)el('p','Доступ до '+date(c.current_expiry),details);
    if(c.state==='review')approval(c,code);
    if(c.state==='created')el('p',c.awaiting_receipt?'📸 Очікуємо скриншот від клієнта.':'Клієнт ще не повідомив про оплату.');
    if(c.owner_note)el('p',c.owner_note);
    if(['created','review','clarification'].includes(c.state)){
      button('💬 Попросити інший скриншот',async()=>{await request('/admin/state',{code,state:'clarification',note:'Надішліть, будь ласка, інший скриншот оплати.',revision:c.revision});await card(code);});
      button('❌ Відхилити',async()=>{
        clear();el('h2','Відхилити заявку '+code+'?');
        const note=field('Причина для клієнта','textarea');note.value='Не вдалося підтвердити надходження оплати.';
        button('Відхилити заявку',async()=>{await request('/admin/state',{code,state:'rejected',note:note.value,revision:c.revision});await card(code);});
        button('Скасувати',()=>card(code));
      });
    }
    button('📋 Усі заявки',allRequests);
    const history=el('details');el('summary','Історія заявки',history);
    (c.history||[]).forEach(h=>el('p',date(h.at)+' · '+h.action+(h.after?' · доступ до '+date(h.after):''),history));
    root.scrollTo?.(0,0);
    await receipt(c,box);
    if(thisView!==viewRevision)return;
    const notices=await request('/admin/'+encodeURIComponent(code)+'/notices');
    if(thisView!==viewRevision)return;
    if(notices.length){
      const delivery=el('details');el('summary','Статус повідомлень',delivery);
      const noticeLabel={pending:'У черзі',sending:'Відправляється',sent:'Прийнято Telegram',retry:'Очікує повтору',failed:'Відправлення відхилено',superseded:'Замінено новішим статусом заявки',uncertain:'Результат невідомий — потрібна ручна перевірка'};
      notices.forEach(n=>{el('p',(n.kind==='owner'?'Власнику: ':'Клієнту: ')+(noticeLabel[n.state]||n.state),delivery);
        if(n.state==='failed')button('Повторити відхилене повідомлення',async()=>{await request('/admin/retry-notice',{notice_id:n.id});await card(code);},delivery);});
    }
  }
  async function allRequests(){state='all';page=1;search='';await queue();}
  const initialCode=new URLSearchParams(root.location?.search||'').get('code');
  root.Telegram?.WebApp?.ready();root.Telegram?.WebApp?.expand?.();
  run(()=>/^AD-[A-F0-9]{12}$/.test(initialCode||'')?card(initialCode):queue())();
})(window);
