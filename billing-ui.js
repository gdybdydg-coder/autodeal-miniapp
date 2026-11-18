// Read-only account status. Purchases and consent stay in the authenticated bot.
(function(root) {
  "use strict";
  const BUY_URL="https://t.me/auto_deal_finder1_bot?start=buy_subscription";
  const STATUS_URL="https://t.me/auto_deal_finder1_bot?start=subscribe";
  let busy=false,adminLink=null,accountId=null;
  const node=(tag,text)=>{const element=document.createElement(tag);if(text!==undefined)element.textContent=text;return element;};
  function telegramLink(label,url){
    const link=node("a",label);link.href=url;link.target="_blank";link.rel="noopener";
    link.addEventListener("click",event=>{
      if(typeof root.Telegram?.WebApp?.openTelegramLink!=="function")return;
      try{root.Telegram.WebApp.openTelegramLink(url);event.preventDefault();}catch(_){/* Keep the native link fallback. */}
    });
    return link;
  }
  function purchaseState(state){
    if(!state||state.access_available!==false)return null;
    const ready=state.sales_enabled===true&&state.payment_method==="bank_manual"&&state.amount_uah===250&&state.days===30
      &&state.purchase_url===BUY_URL&&typeof state.terms==="string"&&state.terms.trim().length>0&&state.terms.length<=5000
      &&typeof state.terms_version==="string"&&state.terms_version.length>0;
    const request=state.request;
    if(request&&request.state==="review")return {kind:"pending",title:"Оплата на перевірці",text:"Власник перевіряє зарахування. Повторно не сплачуйте. Після підтвердження доступ стане активним.",label:"Перевірити оплату в боті",url:STATUS_URL};
    if(request&&request.state==="clarification")return {kind:"pending",title:"Потрібне уточнення оплати",text:"Відкрийте заявку в боті й надішліть потрібну квитанцію або уточнення. Нову оплату не створюємо.",label:"Відкрити заявку в боті",url:STATUS_URL};
    if(request&&request.state==="created")return {kind:"pending",title:"Продовжити оформлення",text:"Заявку вже створено. У боті доступні реквізити та надсилання квитанції після переказу.",label:"Реквізити та квитанція",url:ready?BUY_URL:STATUS_URL};
    if(request&&!['approved','rejected'].includes(request.state))return {kind:"pending",title:"Перевірте стан заявки",text:"Актуальний стан доступний у чаті бота.",label:"Перевірити заявку",url:STATUS_URL};
    // Missing request metadata is not proof that a user has no pending payment.
    if(!ready||(!Object.hasOwn(state,"request")))return null;
    return {kind:"buy",title:"Підключіть моніторинг",text:"250 грн / 30 днів. Банківський переказ. Доступ після перевірки оплати власником. Без автоматичних списань.",label:"Купити підписку — 250 грн",url:BUY_URL,terms:state.terms};
  }
  function renderPurchase(state){
    const offer=purchaseState(state);
    for(const id of ["radarPurchase","settingsPurchase"]){
      const container=document.getElementById(id);if(!container)continue;
      container.replaceChildren();container.hidden=!offer;if(!offer)continue;
      container.append(node("h3",offer.title),node("p",offer.text));
      if(offer.kind==="buy"){
        const terms=node("details"),summary=node("summary","Умови підписки");terms.className="purchase-terms";
        terms.append(summary,node("p",offer.terms));container.append(terms);
        const consent=node("p","Натискаючи «Купити підписку», ви погоджуєтеся з умовами. Бот одразу надішле реквізити; після переказу надішліть йому квитанцію.");consent.className="purchase-consent";container.append(consent);
      }
      const link=telegramLink(offer.label,offer.url);link.className=offer.kind==="buy"?"primary-button purchase-button":"secondary-button purchase-button";
      container.append(link);
    }
  }
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
      renderPurchase(state);
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
      renderPurchase(null);
      target.textContent="Статус доступу: /subscription у боті.";
    } finally {busy=false;}
  }
  root.addEventListener("pageshow",refresh);
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible") refresh();});
  refresh();
})(window);
