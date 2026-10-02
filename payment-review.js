/* Owner-only Mini App. Authentication stays in memory; no tokens in URLs/storage. */
(function(root) {
  'use strict';
  const api='https://autodeal-api.onrender.com/api/manual-payments';
  const labels={review:'Очікують перевірки',clarification:'Потрібне уточнення',approved:'Підтверджені',rejected:'Відхилені',created:'Створені',all:'Усі заявки'};
  const screen=document.getElementById('screen'),error=document.getElementById('error');
  const date=n=>n?new Date(n*1000).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'})+' (Київ)':'немає';
  const money=n=>(n/100).toLocaleString('uk-UA',{minimumFractionDigits:2})+' грн';
  let state='review',page=1,search='';
  const el=(tag,value,parent=screen)=>{const n=document.createElement(tag);if(value!=null)n.textContent=value;parent.append(n);return n;};
  const messages={owner_only:'Цей розділ доступний лише власнику.',manual_review_disabled:'Черга ручної оплати ще не ввімкнена на сервері.',
    owner_configuration_unavailable:'Потрібна перевірка налаштувань власника.',bank_credit_already_used:'Цю банківську операцію вже використано. Перевірте іншу заявку.',
    amount_requires_clarification:'Сума не збігається з тарифом. Залиште заявку на уточненні.',
    request_changed:'Заявка змінилася. Відкрийте її ще раз.',request_or_access_changed:'Заявка або строк доступу змінилися чи підтвердження застаріло. Перевірте картку ще раз.',
    explicit_owner_verification_required:'Підтвердіть, що особисто перевірили фактичне зарахування.',
    invalid_bank_reference:'Вкажіть рахунок і номер операції з банку: щонайменше 3 символи.',
    invalid_text:'Приберіть невидимі символи з номера операції або введіть його вручну.',
    invalid_amount_minor:'Введіть точну суму зарахування в гривнях.',
    'Invalid request':'Перевірте рахунок, номер банківської операції, суму та позначку про перевірку зарахування.',
    payment_storage_unavailable_do_not_pay_again:'Тимчасова помилка збереження. Не вимагайте повторної оплати. Оновіть статус заявки.'};
  async function request(path,body){
    const initData=root.Telegram?.WebApp?.initData;
    if(!initData)throw Error('Відкрийте цей розділ кнопкою /payments у Telegram.');
    const r=await fetch(api+path,{method:body===undefined?'GET':'POST',cache:'no-store',credentials:'omit',
      headers:{'X-Telegram-Init-Data':initData,...(body===undefined?{}:{'Content-Type':'application/json'})},
      body:body===undefined?undefined:JSON.stringify(body)});
    const data=await r.json();
    if(!r.ok){
      if(r.status===401||r.status===403)screen.replaceChildren();
      throw Error(messages[data.detail]||(r.status===401?'Сесію завершено. Відкрийте розділ знову в Telegram.':'Не вдалося виконати дію. Оновіть статус заявки.'));
    }
    return data;
  }
  const run=fn=>async()=>{error.textContent='';try{await fn();}catch(e){error.textContent=e.message;error.scrollIntoView?.({block:'center'});}};
  function button(title,fn,parent=screen,primary=false){
    const b=el('button',title,parent);b.type='button';if(primary)b.className='primary';
    b.addEventListener('click',run(async()=>{if(b.disabled)return;b.disabled=true;try{await fn();}finally{b.disabled=false;}}));return b;
  }
  function field(title,type='text'){
    const l=el('label',title),input=el(type==='textarea'?'textarea':'input',null,l);
    if(type!=='textarea')input.type=type;input.maxLength=type==='textarea'?500:120;return input;
  }
  async function queue(){
    const q=await request('/admin?'+new URLSearchParams({state,page,search}));screen.replaceChildren();
    el('h2',q.waiting+' очікують перевірки');
    if(!q.items.length)el('p','У цій вибірці заявок немає.');
    q.items.forEach(r=>{const b=button('Перевірити оплату · '+r.name+' · '+money(r.amount_minor)+' · '+r.code,()=>card(r.code));b.className='card';});
    Object.keys(labels).forEach(s=>button(labels[s]+(s==='all'?'':' · '+q.counts[s]),async()=>{state=s;page=1;await queue();}));
    const input=field('Номер заявки, ім’я, username або Telegram ID');input.value=search;
    button('Знайти',async()=>{search=input.value;page=1;await queue();});
    el('p','Сторінка '+q.page+' / '+q.pages+' · знайдено '+q.total);
    if(page>1)button('← Попередня',async()=>{page--;await queue();});
    if(page<q.pages)button('Наступна →',async()=>{page++;await queue();});
    button('Оновити',queue);
  }
  function approval(c,code){
    el('h3','Підтвердження оплати');
    el('p','Звір платіж у своєму банку. Вкажи номер операції та точну суму зарахування.');
    const account=field('Рахунок зарахування (IBAN)'),operation=field('Номер банківської операції'),paid=field('Фактично зараховано, грн'),verified=field('Я особисто перевірив зарахування','checkbox');
    account.value=c.receiving_account||'';account.readOnly=Boolean(c.receiving_account);
    account.placeholder='IBAN рахунку, на який надійшли гроші';
    operation.placeholder='Номер операції з історії твого банку';
    paid.inputMode='decimal';paid.placeholder='250,00';
    button('✅ Підтвердити надходження і відкрити доступ',async()=>{
      const reference=(input,label)=>{
        const value=input.value.trim();
        if(value.length<3||value.length>120||/[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}]/u.test(value))
          throw Error(label+': введіть від 3 до 120 символів без невидимих знаків.');
        return value;
      };
      const receiving=reference(account,'Рахунок зарахування'),credit=reference(operation,'Номер банківської операції');
      const v=paid.value.trim().replace(',','.');if(!/^\d+(\.\d{1,2})?$/.test(v))throw Error('Введіть точну суму в гривнях.');
      const parts=v.split('.'),minor=Number(parts[0])*100+Number((parts[1]||'').padEnd(2,'0'));
      if(!Number.isSafeInteger(minor)||minor<=0||minor>100000000)throw Error('Введіть коректну суму зарахування.');
      if(!verified.checked)throw Error('Позначте «Я особисто перевірив зарахування» після звірки з банком.');
      const p=await request('/admin/preview',{code,revision:c.revision,account:receiving,operation:credit,actual_amount_minor:minor,bank_verified:true});
      screen.replaceChildren();el('h2','Остаточне підтвердження');el('p',p.name+' · ID '+p.user_id+' · '+p.code);
      el('p',p.days+' днів. Доступ буде до '+date(p.estimated_expiry));
      el('p','Невикористані дні збережуться.');
      button('Так, надходження перевірено — відкрити доступ',async()=>{
        const result=await request('/admin/confirm',{confirmation:p.confirmation});
        screen.replaceChildren();el('h2','✅ Рішення збережено');el('p','Доступ до '+date(result.expires_at));
        button('Переглянути заявку',()=>card(code));button('📋 Усі очікують',waiting);
      },screen,true);
      button('Повернутися без підтвердження',()=>card(code));
      root.scrollTo?.(0,0);
    },screen,true);
  }
  async function card(code){
    const c=await request('/admin/'+encodeURIComponent(code));screen.replaceChildren();
    el('h2','Заявка '+c.code);el('p',c.name+(c.username?' · @'+c.username:' · без username')+' · Telegram ID '+c.user_id);
    el('p',money(c.amount_minor)+' / '+c.days+' днів · '+labels[c.state]);el('p','Створена '+date(c.created_at));
    el('p','Чинний доступ до '+date(c.current_expiry));
    if(c.state==='review')approval(c,code);
    if(c.state==='created')el('p','Клієнт ще не натиснув «Я оплатив». Підтвердження з’явиться після повідомлення про оплату.');
    if(c.state==='clarification')el('p','Очікуємо уточнення від клієнта. Після повторного «Я оплатив» можна перевірити надходження.');
    el('p','Повідомлена сума: '+(c.reported_amount_minor?money(c.reported_amount_minor):'не вказана'));
    if(c.transfer_note)el('p',c.transfer_note);
    el('p',c.receipt_attached?'📎 Квитанцію прикріплено. Вона не замінює звірку з банком.':'Квитанцію не додано.');
    if(c.receipt_attached)button('📎 Відкрити квитанцію в боті',()=>root.Telegram.WebApp.openTelegramLink('https://t.me/auto_deal_finder1_bot?start=paymentreceipt_'+encodeURIComponent(c.code)));
    if(c.owner_note)el('p',c.owner_note);
    el('h3','Історія');c.history.forEach(h=>el('p',date(h.at)+' · '+h.action+(h.after?' · доступ до '+date(h.after):'')));
    const notices=await request('/admin/'+encodeURIComponent(code)+'/notices');
    if(notices.length){
      el('h3','Повідомлення');
      const noticeLabel={pending:'У черзі',sending:'Відправляється',sent:'Прийнято Telegram',retry:'Очікує повтору',failed:'Відправлення відхилено',superseded:'Замінено новішим статусом заявки',uncertain:'Результат невідомий — потрібна ручна перевірка'};
      notices.forEach(n=>{el('p',(n.kind==='owner'?'Власнику: ':'Клієнту: ')+(noticeLabel[n.state]||n.state));
        if(n.state==='failed')button('Повторити відхилене повідомлення',async()=>{await request('/admin/retry-notice',{notice_id:n.id});await card(code);});});
    }
    if(['created','review','clarification'].includes(c.state)){
      const note=field('Уточнення або причина відхилення','textarea');
      button('💬 Запросити уточнення',async()=>{await request('/admin/state',{code,state:'clarification',note:note.value,revision:c.revision});await card(code);});
      button('❌ Відхилити',async()=>{await request('/admin/state',{code,state:'rejected',note:note.value,revision:c.revision});await card(code);});
    }
    button('📋 Усі очікують',waiting);
    root.scrollTo?.(0,0);
  }
  async function waiting(){state='review';page=1;search='';await queue();}
  const initialCode=new URLSearchParams(root.location?.search||'').get('code');
  root.Telegram?.WebApp?.ready();root.Telegram?.WebApp?.expand?.();
  run(()=>/^AD-[A-F0-9]{12}$/.test(initialCode||'')?card(initialCode):queue())();
})(window);
