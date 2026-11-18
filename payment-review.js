/* Owner-only Mini App. Authentication stays in memory; no tokens in URLs/storage. */
(function(root) {
  'use strict';
  const api='https://autodeal-api.onrender.com/api/manual-payments';
  const grantsApi='https://autodeal-api.onrender.com/api/admin/subscription-grants';
  const labels={review:'Очікують перевірки',clarification:'Потрібен інший скриншот',approved:'Підтверджені',rejected:'Відхилені',created:'Створені',all:'Усі заявки'};
  const status={review:'⏳ Очікує перевірки',clarification:'📸 Очікуємо інший скриншот',approved:'✅ Підтверджено',rejected:'❌ Відхилено',created:'Очікуємо оплату'};
  const historyLabels={created:'Заявку створено',paid_reported:'Клієнт повідомив про оплату',receipt_received:'Скриншот оплати отримано',review:'Передано на перевірку',clarification:'Запрошено інший скриншот',approved:'Оплату підтверджено',rejected:'Заявку відхилено'};
  const screen=document.getElementById('screen'),error=document.getElementById('error');
  const tabs=document.getElementById('ownerTabs'),paymentsTab=document.getElementById('paymentsTab'),grantsTab=document.getElementById('grantsTab');
  const date=n=>n?new Date(n*1000).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'})+' (Київ)':'немає';
  const money=n=>(n/100).toLocaleString('uk-UA',{minimumFractionDigits:2})+' грн';
  let state='review',page=1,search='',receiptUrls=[],viewer=null,viewRevision=0;
  let section='payments',requestsPending=0,grantBusy=false,grantUncertain=false,pendingGrant=null;
  let grantDraft={telegram_id:'',days:'30',reason:''};
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
  function syncTabs(){paymentsTab.disabled=grantsTab.disabled=requestsPending>0||grantBusy||grantUncertain;}
  function selectedSection(value){section=value;for(const [tab,name]of [[paymentsTab,'payments'],[grantsTab,'grants']]){if(value===name)tab.setAttribute('aria-current','page');else tab.removeAttribute('aria-current');}}
  async function authorized(path,body,base=api,signal){
    const initData=root.Telegram?.WebApp?.initData;
    if(!initData){tabs.hidden=true;throw Error('Відкрийте цей розділ із налаштувань AutoDeal або кнопкою /payments у Telegram.');}
    requestsPending++;syncTabs();
    try{
      const r=await fetch(base+path,{method:body===undefined?'GET':'POST',cache:'no-store',credentials:'omit',redirect:'error',
        ...(signal?{signal}:{}),headers:{'X-Telegram-Init-Data':initData,...(body===undefined?{}:{'Content-Type':'application/json'})},
        body:body===undefined?undefined:JSON.stringify(body)});
      if(!r.ok){
        if(r.status===401||r.status===403){clear();tabs.hidden=true;pendingGrant=null;grantUncertain=false;}
        const data=await r.json().catch(()=>({}));
        const detail=base===grantsApi&&typeof data.detail==='string'&&data.detail.length<=1000&&/[А-Яа-яІіЇїЄєҐґ]/.test(data.detail)?data.detail:null;
        const failure=Error(detail||messages[data.detail]||(r.status===401?'Сесію завершено. Відкрийте розділ знову в Telegram.':r.status===403?'Цей розділ доступний лише власнику.':'Не вдалося виконати дію. Оновіть дані.'));
        failure.status=r.status;failure.code=data.code||'';throw failure;
      }
      tabs.hidden=false;return r;
    }finally{requestsPending--;syncTabs();}
  }
  async function request(path,body){requestsPending++;syncTabs();try{return await (await authorized(path,body)).json();}finally{requestsPending--;syncTabs();}}
  async function grantRequest(path='',body){
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),25000);
    requestsPending++;syncTabs();
    try{return await (await authorized(path,body,grantsApi,controller.signal)).json();}
    finally{clearTimeout(timeout);requestsPending--;syncTabs();}
  }
  const run=fn=>async()=>{error.textContent='';try{await fn();}catch(e){error.textContent=typeof e.message==='string'&&/[А-Яа-яІіЇїЄєҐґ]/.test(e.message)?e.message:'Не вдалося з’єднатися із сервером. Перевірте інтернет і оновіть статус заявки.';error.scrollIntoView?.({block:'center'});}};
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
    const input=field('Номер заявки, ім’я, ім’я користувача або Telegram ID');input.value=search;
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
    el('p','🕒 Отримано: '+date(c.received_at||c.created_at),details);el('p',status[c.state]||'Статус заявки потребує перевірки',details);
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
    (c.history||[]).forEach(h=>el('p',date(h.at)+' · '+(historyLabels[h.action]||'Оновлення заявки')+(h.after?' · доступ до '+date(h.after):''),history));
    root.scrollTo?.(0,0);
    await receipt(c,box);
    if(thisView!==viewRevision)return;
    const notices=await request('/admin/'+encodeURIComponent(code)+'/notices');
    if(thisView!==viewRevision)return;
    if(notices.length){
      const delivery=el('details');el('summary','Статус повідомлень',delivery);
      const noticeLabel={pending:'У черзі',sending:'Відправляється',sent:'Прийнято Telegram',retry:'Очікує повтору',failed:'Відправлення відхилено',superseded:'Замінено новішим статусом заявки',uncertain:'Результат невідомий — потрібна ручна перевірка'};
      notices.forEach(n=>{el('p',(n.kind==='owner'?'Власнику: ':'Клієнту: ')+(noticeLabel[n.state]||'Стан повідомлення потребує перевірки'),delivery);
        if(n.state==='failed')button('Повторити відхилене повідомлення',async()=>{await request('/admin/retry-notice',{notice_id:n.id});await card(code);},delivery);});
    }
  }
  const validTelegramId=value=>typeof value==='string'&&/^[1-9][0-9]{0,15}$/.test(value)&&Number.isSafeInteger(Number(value))&&Number(value)<2**52;
  const grantFailure=err=>typeof err?.message==='string'&&/[А-Яа-яІіЇїЄєҐґ]/.test(err.message)?err.message:'Зв’язок із сервером перервався.';
  function grantPayload(id,days,reason){
    if(!validTelegramId(id))throw Error('Введіть числовий Telegram ID користувача без пробілів і знаків.');
    if(!/^[1-9][0-9]{0,3}$/.test(days)||Number(days)>3650)throw Error('Кількість днів має бути цілим числом від 1 до 3650.');
    const note=reason.trim();if(!note||note.length>500)throw Error('Вкажіть причину надання доступу: від 1 до 500 символів.');
    return {telegram_id:Number(id),days:Number(days),reason:note};
  }
  function drawHistory(parent,data){
    if(!Array.isArray(data.items))throw Error('Не вдалося прочитати історію безоплатного доступу. Оновіть її.');
    parent.replaceChildren();el('h3','Історія безоплатного доступу',parent);
    el('p','Останні '+data.limit+' записів. Оплати зберігаються окремо.',parent).className='grant-hint';
    if(!data.items.length)el('p','Надань доступу в цій вибірці ще немає.',parent);
    for(const entry of data.items){
      const row=el('article',null,parent);
      el('p','Користувач: Telegram ID '+entry.telegram_id,row);
      el('p','Надано: '+date(entry.at)+' · доступ до '+date(entry.expires_at),row);
      el('p','Причина: '+entry.reason,row);
      el('p','Надав: Telegram ID '+entry.actor,row).className='grant-hint';
      el('p','Операція: '+entry.action_id,row).className='grant-hint';
    }
  }
  async function grantHistory(target=''){
    const query=new URLSearchParams({limit:50});if(target)query.set('telegram_id',target);
    return grantRequest('?'+query);
  }
  function grantResult(result){
    pendingGrant=null;grantUncertain=false;syncTabs();clear();
    el('h2','✅ Безоплатний доступ надано').className='grant-success';
    if(result.replayed)el('p','Підтверджено раніше виконане надання. Дні повторно не додано.');
    const summary=el('div');summary.className='grant-summary';
    el('p','Користувач: Telegram ID '+result.telegram_id,summary);
    el('p','Доступ до '+date(result.expires_at),summary);
    el('p','Операція: '+result.action_id,summary).className='grant-hint';
    el('p','Оплату не створено. Користувач має вручну ввімкнути потрібні радари. Команду /stop та налаштування повідомлень збережено.');
    button('Надати доступ іншому користувачу',async()=>{grantDraft={telegram_id:'',days:'30',reason:''};await grantsHome();});
    const history=el('section');history.className='grant-history';
    button('Показати історію цього користувача',async()=>drawHistory(history,await grantHistory(String(result.telegram_id))));
  }
  async function reconcileGrant(preview,output){
    const data=await grantHistory(String(preview.telegram_id));
    if(pendingGrant!==preview)return;
    const committed=data.items?.find(entry=>entry.action_id===preview.action_id&&entry.telegram_id===preview.telegram_id);
    if(committed){grantResult({...committed,replayed:true});return;}
    output.textContent='У видимій історії операції немає. Це не доводить, що вона не виконана. Повторіть перевірку тієї самої операції нижче.';
  }
  function grantConfirmation(preview,uncertainMessage=''){
    clear();el('h2',grantUncertain?'Перевірити результат надання':'Надати безоплатний доступ?');
    const summary=el('div');summary.className='grant-summary';
    el('p','Користувач: Telegram ID '+preview.telegram_id,summary);
    el('p',preview.user_ready?'Чат бота підключено.':'Користувач є в базі, але чат не підключено. Для повідомлень потрібна команда /start.',summary);
    el('p','Додати днів: '+preview.days,summary);
    el('p','Поточний строк доступу: '+date(preview.current_expires_at),summary);
    el('p','Новий строк доступу: '+date(preview.proposed_expires_at),summary);
    el('p','Причина: '+preview.reason,summary);
    el('p','Операція: '+preview.action_id,summary).className='grant-hint';
    el('p','Оплата: 0 грн. Невикористані дні збережуться. Радари та повідомлення автоматично не вмикаються.');
    const notice=el('p');notice.className=grantUncertain?'grant-uncertain':'grant-hint';
    notice.textContent=grantUncertain?(uncertainMessage+' Результат поки невідомий. Не створюйте нове надання. Повторна перевірка нижче використовує ту саму операцію та не додає дні вдруге.'):'Підтвердження дійсне до '+date(preview.valid_until)+'. Перевірте Telegram ID і строк.';
    const actions=el('div');actions.className='grant-actions';
    button(grantUncertain?'Повторити перевірку цієї операції':'Так, надати безоплатний доступ',()=>confirmGrant(preview),actions,true);
    if(grantUncertain)button('Перевірити історію цього користувача',()=>reconcileGrant(preview,notice),actions);
    else button('Змінити дані',async()=>{pendingGrant=null;await grantsHome();},actions);
    syncTabs();root.scrollTo?.(0,0);
  }
  async function confirmGrant(preview){
    if(grantBusy||pendingGrant!==preview)return;
    grantBusy=true;syncTabs();
    try{
      const result=await grantRequest('/confirm',{confirmation_token:preview.confirmation_token});
      if(result.action_id!==preview.action_id||result.telegram_id!==preview.telegram_id||!Number.isFinite(result.expires_at)||result.expires_at<=0)throw Error('Сервер не підтвердив результат цієї операції.');
      grantResult(result);
    }catch(err){
      if(err.status===401||err.status===403){pendingGrant=null;grantUncertain=false;throw err;}
      if([404,409,410,422].includes(err.status)){
        pendingGrant=null;grantUncertain=false;clear();el('h2','Надання не виконано');
        el('p',grantFailure(err));
        el('p','Оновіть дані користувача та перевірте новий строк перед підтвердженням.');
        button('Отримати новий попередній перегляд',()=>grantsHome());
      }else{
        grantUncertain=true;grantConfirmation(preview,grantFailure(err));
      }
    }finally{grantBusy=false;syncTabs();}
  }
  async function grantsHome(){
    const history=await grantHistory();clear();selectedSection('grants');
    el('h2','Безоплатний доступ');
    el('p','Надайте доступ без оплати користувачу, який уже відкривав бота. Підтвердження не створить платіж і не ввімкне радари.');
    const form=el('div');form.className='grant-form';
    const target=field('Telegram ID користувача','text',form);target.id='grantTelegramId';target.inputMode='numeric';target.maxLength=16;target.autocomplete='off';target.value=grantDraft.telegram_id;
    el('p','Користувач бачить свій Telegram ID у «Налаштуваннях» AutoDeal. Ім’я користувача або номер телефону не підходять.',form).className='grant-hint';
    const days=field('Кількість днів','text',form);days.id='grantDays';days.inputMode='numeric';days.maxLength=4;days.value=grantDraft.days;
    el('p','Від 1 до 3650 днів. Чинний оплачений або подарований строк не скоротиться.',form).className='grant-hint';
    const reason=field('Причина надання доступу','textarea',form);reason.id='grantReason';reason.value=grantDraft.reason;
    const previewButton=button('Перевірити перед наданням',async()=>{
      if(grantBusy)return;
      grantDraft={telegram_id:target.value.trim(),days:days.value.trim(),reason:reason.value};
      const payload=grantPayload(grantDraft.telegram_id,grantDraft.days,grantDraft.reason);
      grantBusy=true;syncTabs();
      try{
        const preview=await grantRequest('/preview',payload);
        if(typeof preview.confirmation_token!=='string'||!preview.confirmation_token||typeof preview.action_id!=='string'||preview.telegram_id!==payload.telegram_id||preview.days!==payload.days||preview.reason!==payload.reason||!Number.isFinite(preview.proposed_expires_at))throw Error('Не вдалося перевірити попередній перегляд. Доступ не надано; оновіть дані.');
        pendingGrant=preview;grantUncertain=false;grantConfirmation(preview);
      }finally{grantBusy=false;syncTabs();}
    },form,true);previewButton.id='previewGrant';
    const filter=el('div');filter.className='grant-filter';
    const historyTarget=field('Історія за Telegram ID (необов’язково)','text',filter);historyTarget.inputMode='numeric';historyTarget.maxLength=16;
    const historyBox=el('section');historyBox.className='grant-history';drawHistory(historyBox,history);
    button('Оновити історію',async()=>{const value=historyTarget.value.trim();if(value&&!validTelegramId(value))throw Error('Введіть коректний Telegram ID для історії або залиште поле порожнім.');drawHistory(historyBox,await grantHistory(value));},filter);
    root.scrollTo?.(0,0);
  }
  async function openSection(value){
    if(requestsPending||grantBusy||grantUncertain)return;
    pendingGrant=null;error.textContent='';selectedSection(value);
    if(value==='grants')await grantsHome();else await allRequests();
  }
  async function allRequests(){state='all';page=1;search='';await queue();}
  paymentsTab.addEventListener('click',run(()=>openSection('payments')));
  grantsTab.addEventListener('click',run(()=>openSection('grants')));
  const initialParams=new URLSearchParams(root.location?.search||'');
  const initialCode=initialParams.get('code');
  root.Telegram?.WebApp?.ready();root.Telegram?.WebApp?.expand?.();
  selectedSection(initialParams.get('view')==='grants'?'grants':'payments');
  run(()=>section==='grants'?grantsHome():/^AD-[A-F0-9]{12}$/.test(initialCode||'')?card(initialCode):queue())();
})(window);
