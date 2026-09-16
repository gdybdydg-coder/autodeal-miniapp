"use strict";
// No token, initData persistence, automatic import, webhook or delivery controls.
window.Telegram?.WebApp?.ready();
let cloudBusy=false;
function cloudMessage(message) {$("cloudStatus").textContent=message;}
async function cloudAction(action) {
  if(cloudBusy) return;
  cloudBusy=true;
  $("saveCloudSearch").disabled=true;$("refreshCloud").disabled=true;
  cloudMessage("З’єднуюся із сервером… Перший запуск може тривати близько хвилини.");
  try {await action();} catch(error) {cloudMessage(error.message);}
  finally {cloudBusy=false;$("saveCloudSearch").disabled=false;$("refreshCloud").disabled=false;}
}
async function loadCloud() {
  const items=await window.AutoDealCloud.list();
  if(!Array.isArray(items)) throw Error("Некоректна відповідь сервера.");
  const cards=items.map(item=>{
    const filters=savedAPI.normalize(item.filters);
    const card=document.createElement("article");card.className="saved-search-card";
    const name=document.createElement("h3");name.textContent=item.name;
    const summary=document.createElement("p");summary.className="filter-help";summary.textContent=summarizeFilters(filters);
    const status=document.createElement("p");status.className="filter-help";
    status.textContent="Збережено в акаунті · "+(item.enabled?"підписка активна":"без сповіщень");
    const actions=document.createElement("div");actions.className="saved-actions";
    actions.append(managerButton("Відкрити пошук",()=>{
      try {applySavedFilters(filters);$("savedDialog").close();searchCars();}
      catch(error) {cloudMessage(error.message);}
    }),managerButton("Видалити із сервера",()=>{
      if(cloudBusy) return;
      actions.replaceChildren(managerButton("Так, видалити із сервера",()=>cloudAction(async()=>{
        await window.AutoDealCloud.remove(item.id);await loadCloud();
      })),managerButton("Скасувати",()=>cloudAction(loadCloud)));
    }));
    card.append(name,summary,status,actions);return card;
  });
  $("cloudSearchList").replaceChildren(...cards);
  cloudMessage(items.length?"Список оновлено.":"В акаунті ще немає пошуків.");
}
$("refreshCloud").addEventListener("click",()=>cloudAction(loadCloud));
$("saveCloudSearch").addEventListener("click",()=>{
  if(!draftFilters||!$("saveSearchForm").reportValidity()) return;
  const name=$("savedName").value.trim();
  const filters=savedAPI.normalize(draftFilters);
  if(!name) {cloudMessage("Введи назву пошуку.");return;}
  cloudAction(async()=>{
    await window.AutoDealCloud.save(name,filters);
    $("saveSearchForm").hidden=true;
    toast("Збережено в акаунті · без сповіщень");
    await loadCloud();
  });
});
