(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AutoDealPaymentUI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';
  const names = {awaiting:'Очікує квитанцію', review:'На перевірці',
    clarification:'Потрібне уточнення', approved:'Підтверджено', rejected:'Відхилено'};
  function date(value) {
    return value ? new Intl.DateTimeFormat('uk-UA', {timeZone:'Europe/Kyiv',
      day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(value * 1000) : '—';
  }
  function mount(root, api, options={}) {
    const $ = id => root.querySelector('[data-ui="' + id + '"]');
    const fixed=options.role, files=typeof api.upload==='function';
    if(fixed && !['client','admin'].includes(fixed)) throw Error('Невідома тестова роль');
    let role=fixed || 'client', view=null, busy=false, receiptOpen=false;
    let reviewKey=null;
    let receiptSequence=0, paymentSequence=0;
    const buttons=['client-tab','admin-tab','create','paid','receipt','approve','clarify','reject'];
    if(files) buttons.push('download');
    if(fixed) for(const id of ['client-tab','admin-tab']) $(id).hidden=true;
    if(files) {
      $('file-area').hidden=false;
      $('receipt-kind').textContent='Файл тестової квитанції';
      $('receipt-help').textContent='PNG, JPEG або PDF · до 2 МБ · без реального переказу';
      $('receipt').textContent='Надіслати файл на перевірку';
      $('payment-ref-area').hidden=false;
      $('review-message').textContent='Файл на перевірці. Адміністратор входить окремо.';
    }
    function render() {
      if (!view) return;
      const order=view.order, state=order?.state;
      if(files) {
        const key=state==='review'?order.id+':'+order.receipt_revision:null;
        if(key!==reviewKey) {
          $('verified').checked=false;
          $('payment-ref').value=key?'SYNTH-'+order.id:'';
          if(key) $('amount').value=String(order.amount);
          reviewKey=key;
        }
      }
      $('client-tab').setAttribute('aria-pressed',String(role==='client'));
      $('admin-tab').setAttribute('aria-pressed',String(role==='admin'));
      $('client').hidden=role!=='client'; $('admin').hidden=role!=='admin';
      $('tariff').textContent=view.amount+' грн · '+view.days+' днів · тест';
      $('order-id').textContent=order ? 'Заявка '+order.id : 'Заявку ще не створено';
      $('status').textContent=order ? names[state] : 'Почни тестову заявку';
      $('access').textContent=view.membership.active ? 'Абонемент активний до '+date(view.membership.expires_at) : 'Абонемент неактивний';
      $('note').textContent=order?.clarification || '';
      $('note').hidden=!order?.clarification;
      $('create').hidden=!!order && !['approved','rejected'].includes(state);
      $('create').textContent=state==='approved' ? 'Спробувати продовження' : 'Створити тестову заявку';
      const canSend=['awaiting','clarification'].includes(state);
      $('paid').hidden=!canSend || receiptOpen;
      $('paid').textContent=state==='clarification' ? 'Виправити квитанцію' : 'Я оплатив';
      $('receipt-area').hidden=!canSend || !receiptOpen;
      $('review-message').hidden=state!=='review';
      $('admin-summary').textContent=order ? names[state]+' · '+order.amount+' грн' : 'Спочатку створи заявку як клієнт';
      $('admin-form').hidden=state!=='review';
      if(files) $('download').hidden=role!=='admin' || !view.receipt_file;
      $('admin-done').hidden=state==='review' || !order;
      $('admin-done').textContent=state==='approved' ? 'Підтвердження збережене. Перевір вкладку клієнта.' : names[state] || '';
      for (const id of buttons) $(id).disabled=busy;
    }
    async function load() {
      busy=true;
      try { view=await api.state(role); $('error').textContent=''; }
      catch(e) { $('error').textContent=e.message; }
      finally { busy=false; render(); }
    }
    async function act(action,data={},orderId=view?.order?.id || null) {
      if (busy) return;
      busy=true; render(); $('error').textContent='';
      try {
        view=await api.action(role,{action,order_id:orderId,data});
        receiptOpen=false;
      } catch(e) { $('error').textContent=e.message; }
      finally { busy=false; render(); }
    }
    if(!fixed) for (const name of ['client','admin']) $(''+name+'-tab').addEventListener('click',async()=>{
      if(busy) return; role=name; receiptOpen=false; await load();
    });
    $('create').addEventListener('click',()=>act('create',{},null));
    $('paid').addEventListener('click',()=>{receiptOpen=true;render();});
    $('receipt').addEventListener('click',async()=>{
      if(!files) return act('receipt',{
        receipt_ref:'SYNTH-RECEIPT-'+(++receiptSequence)+'-'+Date.now(),paid_at:Math.floor(Date.now()/1000)});
      if(busy) return;
      const file=$('file').files?.[0];
      if(!file) { $('error').textContent='Спочатку обери тестовий файл.';return; }
      busy=true;render();$('error').textContent='';
      try {
        view=await api.upload(view.order.id,file);receiptOpen=false;$('file').value='';
      } catch(e) { $('error').textContent=e.message; }
      finally { busy=false;render(); }
    });
    if(files) $('download').addEventListener('click',async()=>{
      if(busy || !view?.receipt_file || role!=='admin') return;
      busy=true;render();$('error').textContent='';
      try { await api.download(view.receipt_file.id); }
      catch(e) { $('error').textContent=e.message; }
      finally { busy=false;render(); }
    });
    $('approve').addEventListener('click',()=>act('approve',{
      actual_amount:Number($('amount').value),bank_verified:$('verified').checked,
      payment_ref:files?$('payment-ref').value:'SYNTH-PAY-'+(++paymentSequence)+'-'+Date.now()}));
    $('clarify').addEventListener('click',()=>act('clarify',{note:$('clarification').value}));
    $('reject').addEventListener('click',()=>act('reject'));
    return {ready:load(),state:()=>({role,view,busy})};
  }
  return {mount};
});
