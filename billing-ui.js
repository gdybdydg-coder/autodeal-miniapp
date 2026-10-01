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
        (state.access_available?"Доступ активний.":"Для пошуку потрібен абонемент. Фільтри збережені."):
        "Пошук зараз безкоштовний.")+
        (until?" Строк доступу: "+until+" (Київ).":"")+
        (state.sales_enabled?" "+state.amount_stars+" ⭐ за 30 днів. Без автоматичних списань.":" Продаж ще не відкрито.");
    } catch(_) {
      target.textContent="Не вдалося оновити статус. Актуальні умови та доступ: /subscription у боті.";
    } finally {busy=false;}
  }
  root.addEventListener("pageshow",refresh);
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible") refresh();});
  refresh();
})(window);
