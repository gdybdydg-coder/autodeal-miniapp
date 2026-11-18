// Read-only account status. Purchases and consent stay in the authenticated bot.
(function(root) {
  "use strict";
  let busy=false,adminLink=null,accountId=null;
  function showAccountId(target,id) {
    if(!Number.isSafeInteger(id)||id<=0||id>=2**52){accountId?.remove();accountId=null;return;}
    if(!accountId){accountId=document.createElement("p");accountId.id="accountTelegramId";accountId.className="filter-help";target.insertAdjacentElement("afterend",accountId);}
    accountId.textContent="Ваш Telegram ID: "+id;
  }
  function showAdminLink(target,allowed) {
    if(allowed!==true){adminLink?.remove();adminLink=null;return;}
    if(adminLink)return;
    adminLink=document.createElement("a");
    adminLink.id="manageSubscriptionsLink";
    adminLink.className="manager-button telegram-link";
    adminLink.href="payment-review.html?view=grants&v=20261118-grants-1";
    adminLink.textContent="Керування підписками";
    target.insertAdjacentElement("afterend",adminLink);
  }
  async function refresh() {
    const target=document.getElementById("billingStatus");
    if(busy||!target||!root.AutoDealCloud?.billingStatus) return;
    busy=true;
    try {
      const state=await root.AutoDealCloud.billingStatus();
      showAccountId(target,state.telegram_id);
      showAdminLink(target,state.can_manage_subscriptions);
      if (!state.paid_access_required && !state.sales_enabled && state.access_available) {
        target.textContent="Налаштування радарів доступні без оплати. Активність підписки для моніторингу показано на сторінці «Мої радари».";
        return;
      }
      const until=state.expires_at?new Date(state.expires_at*1000).toLocaleString("uk-UA",{timeZone:"Europe/Kyiv"}):"";
      target.textContent=(state.paid_access_required?
        (state.access_available?"✅ Доступ активний.":"Доступ не активний. Радари збережені."):
        "Налаштування радарів доступні без оплати.")+
        (until?" До "+until+" (Київ).":"")+
        (state.sales_enabled?" "+(state.payment_method==='bank_manual'?state.amount_uah+" грн":state.amount_stars+" ⭐")+" за 30 днів. Без автосписань.":" Продаж ще не відкрито.");
    } catch(_) {
      showAdminLink(target,false);
      showAccountId(target,null);
      target.textContent="Статус доступу: /subscription у боті.";
    } finally {busy=false;}
  }
  root.addEventListener("pageshow",refresh);
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible") refresh();});
  refresh();
})(window);
