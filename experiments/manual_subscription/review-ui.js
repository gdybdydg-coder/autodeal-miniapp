'use strict';
(() => {
  let token = null, code = null, state = 'review', page = 1, query = '';
  const screen = document.querySelector('#screen');
  const label = {awaiting:'Створені',review:'Очікують перевірки',clarification:'Потрібне уточнення',approved:'Підтверджені',rejected:'Відхилені',all:'Усі заявки'};
  const date = v => v ? new Date(v*1000).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'})+' (Київ)' : 'немає';
  const el = (tag,text,parent=screen) => {const n=document.createElement(tag);if(text!=null)n.textContent=text;parent.append(n);return n;};
  const run = f => async () => {document.querySelector('#error').textContent='';try{await f();}catch(e){document.querySelector('#error').textContent=e.message;}};
  const button = (text,fn,parent=screen) => {const b=el('button',text,parent);b.type='button';b.addEventListener('click',run(fn));return b;};
  const field = (text,type='text',parent=screen) => {const l=el('label',text,parent);const i=el('input',null,l);i.type=type;return i;};
  async function http(path,body,raw=false){
    const headers={'X-Local-Session':token||'','X-AutoDeal-Local':'1'};
    if(body!==undefined)headers['Content-Type']=raw?body.type:'application/json';
    if(raw)headers['X-Order-ID']=code;
    const r=await fetch(path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:raw?body:JSON.stringify(body)});
    const data=await r.json();if(!r.ok)throw Error(data.error||'Помилка локального тесту');return data;
  }
  async function act(command,data){return (await http('/api/action',{command,data})).result;}
  async function refresh(){const d=await http('/api/state');screen.hidden=false;screen.replaceChildren();document.querySelector('#login').hidden=true;document.querySelector('#logout').hidden=false;if(d.role==='admin')return queue();client(d);}
  function client(d){
    el('h2','250 грн · 30 днів');el('p','Пошук за твоїми фільтрами та відповідні вигідні пропозиції в Telegram. Фільтри збережені.');
    el('p','Доступ відкриває власник після перевірки фактичного зарахування. Автоматичних списань немає.');
    if(!d.order){button('Створити тестову заявку',async()=>{code=await act('create',{});await refresh();});return;}
    code=d.order.code;el('h3','Заявка '+code);el('p',d.order.amount+' грн · '+d.order.days+' днів · '+label[d.order.state]);
    if(d.order.state==='review')el('p','Заявка прийнята. Очікує перевірки власником.');
    if(d.order.clarification)el('p',d.order.clarification);
    if(d.order.rejection_reason)el('p',d.order.rejection_reason+' Відхилення заявки не означає повернення коштів.');
    if(d.order.expires_at)el('p','Доступ до '+date(d.order.expires_at));
    if(d.payment_instruction){
      const p=d.payment_instruction;
      el('p','Отримувач: '+p.recipient_name);el('p','Код отримувача: '+p.recipient_code);
      el('p','Банк: '+p.bank_name);el('p','IBAN: '+p.iban);
      el('p','Призначення переказу потребує погодження. Не переказуй кошти в цьому тесті.');
      button('Скопіювати IBAN',()=>navigator.clipboard.writeText(p.iban));
    }
    else el('p','Реквізити для цього тесту не налаштовані. Переказ не виконуй.');
    if(['awaiting','review','clarification'].includes(d.order.state)){
      const amount=field('Вказана сума (необов’язково)','number'),note=field('Приблизний час / відомості переказу (необов’язково)');
      button('Я оплатив / доповнити заявку',async()=>{const data={code};if(amount.value)data.amount=Number(amount.value);if(note.value)data.note=note.value;await act('paid',data);await refresh();});
      const file=field('Тестова квитанція, необов’язково','file');file.accept='.png,.jpg,.jpeg,.pdf';
      button('Додати квитанцію',async()=>{if(!file.files[0])throw Error('Обери тестовий файл');await http('/api/receipt',file.files[0],true);await refresh();});
    }else button('Нова заявка / продовження',async()=>{await act('create',{});await refresh();});
    button('Переглянути статус',refresh);button('Підтримка',async()=>el('p','У робочому боті: /paysupport із номером заявки. Цей локальний тест повідомлень не надсилає.'));
  }
  async function queue(){
    const q=await act('/payments',{state,search:query,page,size:20});screen.replaceChildren();
    el('h2','Черга власника · /payments');el('p',q.waiting+' очікують · найстаріша '+Math.floor(q.oldest_age/60)+' хв');
    Object.keys(label).forEach(s=>button(label[s]+(s==='all'?'':' ('+q.counts[s]+')'),async()=>{state=s;page=1;await queue();}));
    const input=field('Номер заявки, ім’я, username або Telegram ID');input.value=query;
    button('Знайти',async()=>{query=input.value;page=1;await queue();});
    q.items.forEach(o=>button(o.code+' · '+(o.name||'Клієнт')+' · '+o.amount+' грн · '+label[o.state],()=>card(o.code)));
    el('p','Сторінка '+q.page+' із '+q.pages+' · у вибірці '+q.total);
    if(page>1)button('Попередня',async()=>{page--;await queue();});if(page<q.pages)button('Наступна',async()=>{page++;await queue();});
  }
  async function card(id){
    code=id;const c=await act('card',{code});screen.replaceChildren();
    el('h2','Заявка '+code);el('p',c.name+(c.username?' @'+c.username:' · без username')+' · Telegram ID '+c.uid);
    el('p','Створена: '+date(c.created)+' · '+c.amount+' грн / '+c.days+' днів');
    el('p','Статус: '+label[c.state]+' · чинний доступ до '+date(c.current_expiry));
    el('p','Вказаний переказ: '+(c.reported_amount??'сума не вказана')+' · '+date(c.receipt_at));
    el('p','Квитанція: '+(c.receipt||'не додана'));if(c.transfer_note)el('p',c.transfer_note);
    if(c.receipt?.startsWith('RCPT-'))button('Завантажити квитанцію',async()=>{const r=await fetch('/api/receipts/'+encodeURIComponent(c.receipt),{headers:{'X-Local-Session':token}});if(!r.ok)throw Error('Файл недоступний');const u=URL.createObjectURL(await r.blob());const a=el('a','Зберегти файл');a.href=u;a.download='receipt';a.click();URL.revokeObjectURL(u);a.remove();});
    el('h3','Історія');c.history.forEach(h=>el('p',date(h[2])+' · '+h[1]));
    if(['review','clarification','awaiting'].includes(c.state)){
      const reason=field('Причина відхилення / запит уточнення');
      if(c.state==='review')button('Запросити уточнення',async()=>{await act('clarify',{code,note:reason.value});await card(code);});
      button('Відхилити',async()=>{await act('reject',{code,reason:reason.value});await card(code);});
    }
    if(c.state==='review'){
      el('h3','Ручна звірка фактичного зарахування');
      const account=field('Постійний ідентифікатор рахунку'),transaction=field('Номер зарахованої банківської операції'),amount=field('Фактично зарахована сума','number'),verified=field('Я особисто перевірив зарахування','checkbox');
      button('Підтвердити надходження і відкрити доступ',async()=>{
        const p=await act('preview',{code,account:account.value,transaction:transaction.value,actual_amount:Number(amount.value),bank_verified:verified.checked});
        screen.replaceChildren();el('h2','Остаточне підтвердження');el('p',p.name+' · ID '+p.user_id+' · '+p.code+' · '+p.days+' днів');
        el('p','Попередня дата завершення: '+date(p.expires_at)+'. Відлік почнеться в момент підтвердження; остаточна дата може бути пізнішою до 5 хвилин.');
        button('Так, відкрити доступ',async()=>{const expiry=await act('confirm',{confirmation:p.confirmation});screen.replaceChildren();el('h2','Тестовий доступ відкрито до '+date(expiry));button('Усі очікують',queue);});
        button('Скасувати',()=>card(code));
      });
    }
    button('Усі очікують',async()=>{state='review';query='';page=1;await queue();});
  }
  document.querySelector('#enter').addEventListener('click',run(async()=>{const d=await http('/api/login',{invitation:document.querySelector('#invite').value});token=d.token;document.querySelector('#invite').value='';await refresh();}));
  document.querySelector('#logout').addEventListener('click',run(async()=>{await http('/api/logout',{});token=null;screen.replaceChildren();screen.hidden=true;document.querySelector('#login').hidden=false;document.querySelector('#logout').hidden=true;}));
})();
