(function(root,factory){
  const api=factory(typeof module==='object'&&module.exports ? require('./workflow.js') : root.ManualDemo);
  if(typeof module==='object'&&module.exports) module.exports=api;
  else root.AutoDealTrialMemory=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(Workflow){
  'use strict';
  const AMOUNT=250, DAYS=30;
  function create() {
    let order=null, membershipExpiry=null, sequence=0;
    const usedReferences=[];
    const now=()=>Math.floor(Date.now()/1000);
    function state(role) {
      if(!['client','admin'].includes(role)) throw Error('Невідома тестова роль');
      return {amount:AMOUNT,days:DAYS,tariff_confirmed:true,payments_enabled:false,search_enabled:false,
        order:order ? {...order,expires_at:order.expires||null,clarification:order.state==='clarification'?order.note:null} : null,
        membership:{expires_at:membershipExpiry,active:!!membershipExpiry&&now()<membershipExpiry}};
    }
    function action(role,body) {
      state(role);
      if(body.action==='create') {
        if(role!=='client') throw Error('Створи заявку у вкладці клієнта');
        if(!order||['approved','rejected'].includes(order.state))
          order={id:'AD-DEMO-'+(++sequence),state:'awaiting',amount:AMOUNT,days:DAYS,
            created:now(),expires:membershipExpiry||0,note:'',receiptRevision:0};
      } else {
        if(!order||body.order_id!==order.id) throw Error('Заявку не знайдено');
        const client=body.action==='receipt';
        if(role!==(client?'client':'admin')) throw Error('Ця дія недоступна в обраній ролі');
        const d=body.data;
        let event;
        if(client) event={type:'receipt',filename:'DEMO.pdf',receiptReference:d.receipt_ref,paidAt:d.paid_at};
        else if(body.action==='approve') event={type:'approve',amount:d.actual_amount,
          bankVerified:d.bank_verified,reference:d.payment_ref,usedReferences};
        else if(body.action==='clarify') event={type:'clarify',note:d.note};
        else if(body.action==='reject') event={type:'reject'};
        else throw Error('Невідома дія');
        order=Workflow.transition(order,event,now());
        if(body.action==='approve') {membershipExpiry=order.expires;usedReferences.push(d.payment_ref);}
      }
      return state(role);
    }
    return {state,action};
  }
  return {create};
});
