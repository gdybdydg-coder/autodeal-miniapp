"use strict";
// No token or initData persistence. Notification consent is always explicit.
window.Telegram?.WebApp?.ready();
let cloudBusy=false;
let notificationState=null;
function renderNotificationStatus() {
  const state=notificationState;
  $("notificationStatus").textContent=!state?.available?"Сповіщення ще готуються до запуску":
    !state.telegram_ready?"Потрібно надіслати /start у чаті бота":
    !state.test_sent?"Надішли тестове повідомлення, щоб перевірити зв’язок":
    "Можна ввімкнути один пошук · перевірка приблизно щохвилини";
  $("testNotification").disabled=cloudBusy||!state?.available||!state?.telegram_ready;
}
function cloudMessage(message) {$("cloudStatus").textContent=message;}
async function cloudAction(action) {
  if(cloudBusy) return;
  cloudBusy=true;
  $("saveCloudSearch").disabled=true;$("refreshCloud").disabled=true;
  renderNotificationStatus();
  cloudMessage("З’єднуюся із сервером…");
  try {await action();} catch(error) {cloudMessage(error.message);}
  finally {cloudBusy=false;$("saveCloudSearch").disabled=false;$("refreshCloud").disabled=false;renderNotificationStatus();}
}
async function loadCloud() {
  $("cloudSearchList").replaceChildren();
  $("cloudTitle").textContent="В акаунті Telegram";
  const items=await window.AutoDealCloud.list();
  if(!Array.isArray(items)) throw Error("Некоректна відповідь сервера.");
  notificationState=null;
  if(window.AutoDealCloud.notificationStatus) {
    try {notificationState=await window.AutoDealCloud.notificationStatus();}catch(_){}
  }
  renderNotificationStatus();
  const cards=items.map(item=>{
    const filters=savedAPI.normalize(item.filters);
    const card=document.createElement("article");card.className="saved-search-card";
    const name=document.createElement("h3");name.textContent=item.name;
    const summary=document.createElement("p");summary.className="filter-help";summary.textContent=summarizeFilters(filters);
    const status=document.createElement("p");status.className="filter-help";
    const statuses={starting:"готуємо початковий список, попередні авто не розсилаємо",watching:"моніторинг працює",
      checking:"оцінюємо нові авто",coverage_limited:"частину авто не встигли оцінити: звузь фільтри пошуку",
      quota_exceeded:"пауза: ліміт запитів AUTO.RIA",busy:"очікуємо завершення іншого запиту",
      search_limit:"перевірку продовжимо наступним циклом",window_gap:"забагато нових результатів: звузь фільтри та ввімкни пошук знову",
      unsupported_filter:"фільтр не підтверджено AUTO.RIA: зміни пошук",invalid_response:"пауза: некоректна відповідь AUTO.RIA"};
    status.textContent="Збережено в акаунті · "+(item.enabled?
      (notificationState?.available?(statuses[item.monitor_status]||"очікуємо перевірку джерела"):"моніторинг тимчасово недоступний"):"без сповіщень");
    const actions=document.createElement("div");actions.className="saved-actions";
    actions.append(managerButton("Відкрити пошук",()=>openSavedSearch(filters)),managerButton("Видалити із сервера",()=>{
      if(cloudBusy) return;
      actions.replaceChildren(managerButton("Так, видалити із сервера",()=>cloudAction(async()=>{
        await window.AutoDealCloud.remove(item.id);await loadCloud();
      })),managerButton("Скасувати",()=>cloudAction(loadCloud)));
    }));
    if(window.AutoDealCloud.enable) {
      const toggle=managerButton(item.enabled?"Вимкнути сповіщення":"Увімкнути сповіщення",()=>cloudAction(async()=>{
        await window.AutoDealCloud.enable(item.id,!item.enabled);await loadCloud();
      }));
      toggle.disabled=!item.enabled&&!(notificationState?.available&&notificationState.telegram_ready&&notificationState.test_sent);
      actions.append(toggle);
    }
    card.append(name,summary,status,actions);return card;
  });
  $("cloudSearchList").replaceChildren(...cards);
  $("cloudTitle").textContent="В акаунті Telegram ("+items.length+")";
  cloudMessage(items.length?"Список оновлено.":"В акаунті ще немає пошуків.");
}
window.AutoDealCloudSearches={refresh:()=>cloudAction(loadCloud)};
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
    if(managerSession===savingSession) {$("saveSearchForm").hidden=true;draftFilters=null;}
    toast("Збережено в акаунті · без сповіщень");
    await loadCloud();
  });
});
