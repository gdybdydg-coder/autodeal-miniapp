// Read-only account status. Purchases and consent stay in the authenticated bot.
(function(root) {
  "use strict";
  let busy=false;
  async function refresh() {
    const target=document.getElementById("billingStatus");
    if(busy||!target||!root.AutoDealCloud?.billingStatus) return;
    busy=true;
    try {
      const state=await root.AutoDealCloud.billingStatus();
      const until=state.expires_at?new Date(state.expires_at*1000).toLocaleString("uk-UA",{timeZone:"Europe/Kyiv"}):"";
      target.textContent=(state.paid_access_required?
        (state.access_available?"✅ Доступ активний.":"Доступ не активний. Фільтри збережені."):
        "Пошук зараз безкоштовний.")+
        (until?" До "+until+" (Київ).":"")+
        (state.sales_enabled?" "+(state.payment_method==='bank_manual'?state.amount_uah+" грн":state.amount_stars+" ⭐")+" за 30 днів. Без автосписань.":" Продаж ще не відкрито.");
    } catch(_) {
      target.textContent="Статус доступу: /subscription у боті.";
    } finally {busy=false;}
  }
  root.addEventListener("pageshow",refresh);
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible") refresh();});
  refresh();
})(window);
