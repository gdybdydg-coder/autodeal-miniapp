"use strict";
// No token or initData persistence. Notification consent is always explicit.
window.Telegram?.WebApp?.ready();
let cloudBusy=false;
let notificationState=null;
function renderNotificationStatus() {
  const state=notificationState;
  $("notificationStatus").textContent=!state?.available?"Готуються до запуску":
    !state.telegram_ready?"Підключи чат бота":
    !state.test_sent?"Перевір зв’язок тестовим повідомленням":
    "Готові до ввімкнення · один пошук";
  $("testNotification").disabled=cloudBusy||!state?.available||!state?.telegram_ready;
}
function cloudMessage(message) {
  $("cloudStatus").textContent=message;
  $("cloudStatus").hidden=message==="Список оновлено.";
  $("settingsStatus").textContent=message;
  $("settingsStatus").hidden=message==="Список оновлено.";
  if($("subscriptionMenu").open) {
    $("subscriptionMenuError").textContent=message;
    $("subscriptionMenuError").hidden=message==="Список оновлено.";
  }
}
async function cloudAction(action) {
  if(cloudBusy) return;
  cloudBusy=true;
  $("saveCloudSearch").disabled=true;$("refreshCloud").disabled=true;
  $("refreshSettings").disabled=true;
  renderNotificationStatus();
  cloudMessage("З’єднуюся із сервером…");
  try {await action();} catch(error) {cloudMessage(error.message);}
  finally {cloudBusy=false;$("saveCloudSearch").disabled=false;$("refreshCloud").disabled=false;$("refreshSettings").disabled=false;renderNotificationStatus();}
}
async function loadCloud() {
  $("cloudSearchList").replaceChildren();
  $("cloudTitle").textContent="Підписки в акаунті Telegram";
  $("cloudCount").textContent="…";
  const items=await window.AutoDealCloud.list();
  if(!Array.isArray(items)) throw Error("Некоректна відповідь сервера.");
  notificationState=null;
  if(window.AutoDealCloud.notificationStatus) {
    try {notificationState=await window.AutoDealCloud.notificationStatus();}catch(_){}
  }
  renderNotificationStatus();
  const cards=items.map(item=>{
    const filters=savedAPI.normalize(item.filters);
    const statuses={starting:"готуємо початковий список, попередні авто не розсилаємо",watching:"моніторинг працює",
      checking:"оцінюємо нові авто",coverage_limited:"частину авто не встигли оцінити: звузь фільтри пошуку",
      quota_exceeded:"пауза: ліміт запитів AUTO.RIA",busy:"очікуємо завершення іншого запиту",
      search_limit:"перевірку продовжимо наступним циклом",window_gap:"забагато нових результатів: звузь фільтри та ввімкни пошук знову",
      unsupported_filter:"фільтр не підтверджено AUTO.RIA: зміни пошук",invalid_response:"пауза: некоректна відповідь AUTO.RIA"};
    const status=item.enabled?
      (notificationState?.available?(statuses[item.monitor_status]||"очікуємо перевірку джерела"):"моніторинг тимчасово недоступний"):"Сповіщення вимкнені";
    return subscriptionCard({...item,filters},{cloud:true,status,
      canToggle:item.enabled||!!(notificationState?.available&&notificationState.telegram_ready&&notificationState.test_sent),
      toggle:window.AutoDealCloud.enable?()=>cloudAction(async()=>{
        await window.AutoDealCloud.enable(item.id,!item.enabled);await loadCloud();
        closeSubscriptionActions();
      }):null,
      remove:()=>cloudAction(async()=>{
        await window.AutoDealCloud.remove(item.id);await loadCloud();closeSubscriptionActions();
      })});
  });
  $("cloudSearchList").replaceChildren(...cards);
  $("cloudTitle").textContent="Підписки в акаунті Telegram ("+items.length+")";
  $("cloudCount").textContent=String(items.length);
  cloudMessage(items.length?"Список оновлено.":"В акаунті ще немає пошуків.");
}
async function loadSettings() {
  notificationState=null;
  notificationState=await window.AutoDealCloud.notificationStatus();
  renderNotificationStatus();
  cloudMessage("Стан підключення оновлено.");
}
window.AutoDealCloudSearches={refresh:()=>cloudAction(loadCloud),refreshSettings:()=>cloudAction(loadSettings)};
$("refreshSettings").addEventListener("click",()=>cloudAction(loadSettings));
$("refreshCloud").addEventListener("click",()=>cloudAction(loadCloud));
$("testNotification").addEventListener("click",()=>cloudAction(async()=>{
  const result=await window.AutoDealCloud.testNotification();
  await loadCloud();
  cloudMessage(result.state==="sent"?"Тест надіслано. Перевір чат бота; потім увімкни потрібний пошук.":
    "Доставку тесту не підтверджено. Перевір чат; автоматично повторювати повідомлення не будемо.");
}));
$("saveCloudSearch").addEventListener("click",()=>{
  if(!draftFilters||!$("saveSearchForm").reportValidity()) return;
  const name=$("savedName").value.trim();
  const filters=savedAPI.normalize(draftFilters);
  const savingSession=managerSession;
  if(!name) {cloudMessage("Введи назву пошуку.");return;}
  cloudAction(async()=>{
    await window.AutoDealCloud.save(name,filters);
    if(managerSession===savingSession) {draftFilters=null;savedScope="cloud";renderSavedView();}
    toast("Збережено в акаунті · без сповіщень");
    await loadCloud();
  });
});
