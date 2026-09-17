"use strict";
// No token or initData persistence. Notification consent is always explicit.
window.Telegram?.WebApp?.ready();
let cloudBusy=false;
let notificationState=null;
function renderNotificationStatus() {
  const state=notificationState;
  const testAvailable=state?.test_available??state?.available;
  $("notificationStatus").textContent=!state?"Стан підключення ще не підтверджено":!testAvailable&&!state.available?"Готуються до запуску":
    !state.telegram_ready?"Підключи чат бота":
    !state.test_sent?"Перевір зв’язок тестовим повідомленням":
    !state.available?"Тест надіслано · моніторинг ще не ввімкнено":
    state.discovery?.needs_attention?"Перевірка AUTO.RIA затримується":
    state.activity?.enabled_subscriptions>0&&state.discovery?.successful_groups===0?"Чекаємо першої успішної перевірки AUTO.RIA":
    state.activity?.enabled_subscriptions>0?"Моніторинг працює":"Готові до ввімкнення";
  const activity=state?.activity;
  $("launchProgress").textContent=!activity?"Увійди через Telegram, щоб побачити стан перевірок.":
    !activity.enabled_subscriptions?"Активних підписок ще немає. У розділі «Підписки» натисни ⋯ і ввімкни потрібну.":
    "Активних підписок: "+activity.enabled_subscriptions+". Нових авто: "+activity.new_listings+
    ". Оцінено: "+activity.evaluated+". Бракує даних: "+activity.unknown+". У черзі: "+activity.pending+
    ". Повідомлень прийнято Telegram: "+activity.messages_accepted+"."+
    (state.discovery?.needs_attention?" Є затримка або помилка отримання оголошень. Деталі — у картці підписки.":"")+
    (Number.isFinite(activity.last_delivery?.discovery_to_telegram_seconds)?
      " Остання доставка після виявлення: "+Math.ceil(activity.last_delivery.discovery_to_telegram_seconds)+" с.":"");
  $("testNotification").disabled=cloudBusy||!testAvailable||!state?.telegram_ready;
  $("subscriptionsHint").textContent=!state?(cloudBusy?"Перевіряємо стан сповіщень…":"Стан сповіщень не підтверджено. Онови список, щоб перевірити підключення."):
    !state.available?"Підписки збережені. Сповіщення ще готуються до запуску.":
    !state.telegram_ready?"Для сповіщень відкрий чат бота й надішли /start.":
    !state.test_sent?"Перевір зв’язок тестовим повідомленням у налаштуваннях.":
    "Вмикай сповіщення в меню ⋯ потрібних підписок. Однакові фільтри перевіряються разом.";
}
function cloudMessage(message) {
  $("cloudStatus").textContent=message;
  $("cloudStatus").hidden=message==="Список оновлено.";
  $("settingsStatus").textContent=message;
  $("settingsStatus").hidden=message==="Список оновлено.";
  $("saveStatus").textContent=message;
  $("saveStatus").hidden=message==="Список оновлено.";
  if($("subscriptionMenu").open) {
    $("subscriptionMenuError").textContent=message;
    $("subscriptionMenuError").hidden=message==="Список оновлено.";
  }
}
async function cloudAction(action) {
  if(cloudBusy) return;
  cloudBusy=true;
  $("saveCloudSearch").disabled=true;$("confirmSaveSearch").disabled=true;$("refreshCloud").disabled=true;
  $("refreshSettings").disabled=true;
  renderNotificationStatus();
  cloudMessage("З’єднуюся із сервером…");
  try {await action();} catch(error) {cloudMessage(error.message);}
  finally {cloudBusy=false;$("saveCloudSearch").disabled=false;$("confirmSaveSearch").disabled=false;$("refreshCloud").disabled=false;$("refreshSettings").disabled=false;renderNotificationStatus();}
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
    const statuses={starting:"починаємо стежити за новими оголошеннями",watching:"моніторинг працює",
      checking:"оцінюємо нові авто",catching_up:"дочитуємо нові оголошення · черга збережена",
      coverage_changed:"список AUTO.RIA змінюється · повторюємо перевірку",
      quota_exceeded:"пауза: ліміт запитів AUTO.RIA",busy:"очікуємо завершення іншого запиту",
      search_limit:"перевірку продовжимо наступним циклом",
      unsupported_filter:"фільтр не підтверджено AUTO.RIA: зміни пошук",invalid_response:"пауза: некоректна відповідь AUTO.RIA",
      connection_error:"AUTO.RIA не відповідає · повторимо перевірку",upstream_error:"помилка AUTO.RIA · повторимо перевірку",
      key_rejected:"AUTO.RIA відхилила API-ключ",access_denied:"AUTO.RIA обмежила доступ"};
    let status=item.enabled?
      (notificationState?.available?(statuses[item.monitor_status]||"очікуємо перевірку джерела"):"Моніторинг тимчасово недоступний"):"На паузі · сповіщення вимкнені";
    if(item.enabled&&notificationState?.available&&item.pending_count>0) status+=" · у черзі: "+item.pending_count;
    if(item.enabled&&notificationState?.available&&item.unvalued_count>0) status+=" · бракує даних для оцінки: "+item.unvalued_count;
    const reasons={missing_details:"неповні характеристики авто",insufficient_comparables:"замало схожих авто",
      comparison_limit:"не вистачило перевірених аналогів у межах ліміту",mixed_sample:"ціни аналогів надто різняться",
      stale_details:"потрібні свіжі ціни"};
    if(item.enabled&&notificationState?.available&&item.unvalued_count>0&&reasons[item.latest_valuation_reason])
      status+=" · остання оцінка: "+reasons[item.latest_valuation_reason];
    return subscriptionCard({...item,filters},{cloud:true,status,active:item.enabled&&notificationState?.available,
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
  cloudMessage(items.length?"Список оновлено.":"В акаунті ще немає підписок. Натисни «+ Нова підписка» та обери фільтри.");
}
async function loadSettings() {
  notificationState=null;
  notificationState=await window.AutoDealCloud.notificationStatus();
  renderNotificationStatus();
  cloudMessage("Стан підключення оновлено.");
}
window.AutoDealCloudSearches={refresh:()=>cloudAction(loadCloud),refreshSettings:()=>cloudAction(loadSettings),isBusy:()=>cloudBusy};
$("refreshSettings").addEventListener("click",()=>cloudAction(loadSettings));
$("refreshCloud").addEventListener("click",()=>cloudAction(loadCloud));
$("testNotification").addEventListener("click",()=>cloudAction(async()=>{
  const result=await window.AutoDealCloud.testNotification();
  await loadSettings();
  cloudMessage(result.state==="sent"?"Тест надіслано. Відкрий чат бота та перевір отримання.":
    "Доставку тесту не підтверджено. Перевір чат; автоматично повторювати повідомлення не будемо.");
}));
$("saveCloudSearch").addEventListener("click",()=>{
  if(!draftFilters||editingSearch?.scope==="local"||!$("saveSearchForm").reportValidity()) return;
  const name=$("savedName").value.trim();
  const filters=savedAPI.normalize(draftFilters);
  const savingSession=managerSession;
  const editing=editingSearch;
  if(!name) {cloudMessage("Введи назву пошуку.");return;}
  return cloudAction(async()=>{
    const result=editing?await window.AutoDealCloud.update(editing.id,name,filters):await window.AutoDealCloud.save(name,filters);
    if(managerSession===savingSession) {resetSubscriptionEditor();draftFilters=null;savedScope="cloud";renderSavedView();}
    toast(result?.enabled?"Зміни збережено · сповіщення ввімкнені":"Збережено в акаунті · підписка на паузі");
    await loadCloud();
  });
});
