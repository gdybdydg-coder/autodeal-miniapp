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
  function mount(root, api) {
    const $ = id => root.querySelector('[data-ui="' + id + '"]');
    let role='client', view=null, busy=false, receiptOpen=false;
    let receiptSequence=0, paymentSequence=0;
    const buttons=['client-tab','admin-tab','create','paid','receipt','approve','clarify','reject'];
    function render() {
      if (!view) return;
      const order=view.order, state=order?.state;
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
    for (const name of ['client','admin']) $(''+name+'-tab').addEventListener('click',async()=>{
      if(busy) return; role=name; receiptOpen=false; await load();
    });
    $('create').addEventListener('click',()=>act('create',{},null));
    $('paid').addEventListener('click',()=>{receiptOpen=true;render();});
    $('receipt').addEventListener('click',()=>act('receipt',{
      receipt_ref:'SYNTH-RECEIPT-'+(++receiptSequence)+'-'+Date.now(),paid_at:Math.floor(Date.now()/1000)}));
    $('approve').addEventListener('click',()=>act('approve',{
      actual_amount:Number($('amount').value),bank_verified:$('verified').checked,
      payment_ref:'SYNTH-PAY-'+(++paymentSequence)+'-'+Date.now()}));
    $('clarify').addEventListener('click',()=>act('clarify',{note:$('clarification').value}));
    $('reject').addEventListener('click',()=>act('reject'));
    return {ready:load(),state:()=>({role,view,busy})};
  }
  return {mount};
});
