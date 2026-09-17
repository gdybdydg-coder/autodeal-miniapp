"use strict";
const savedAPI=window.AutoDealSaved;
let draftFilters=null,savedScope="cloud",menuOpener=null;
let managerReturnTab="search",managerSession=0;
function summarizeFilters(f) {
  const parts=[f.brand||"Всі марки",f.model,f.region||"Вся Україна"];
  for(const [key,label,unit] of [["price","Ціна","$"],["year","Рік",""],["mileage","Пробіг","тис. км"]]) {
    const r=f[key];
    if(r.from!==null||r.to!==null) parts.push(label+": "+(r.from??"…")+"–"+(r.to??"…")+" "+unit);
  }
  for(const key of ["body","fuel","transmission"]) if(f[key].length) parts.push(f[key].join(", "));
  parts.push(f.onlyDeals?"Від 15% нижче ринку":"Без обмеження вигоди");
  return parts.filter(Boolean).join(" · ");
}
function storageError() {
  $("savedError").hidden=false;
  $("savedError").textContent="Не вдалося прочитати або зберегти пошуки. Перевір доступ браузера до сховища. Наявні дані не очищалися.";
}
function managerButton(text,action) {
  const button=document.createElement("button");
  button.type="button";button.className="manager-button";button.textContent=text;
  button.addEventListener("click",action);return button;
}
function subscriptionSummary(filters,name) {
  const parts=[],vehicle=[filters.brand,filters.model].filter(Boolean).join(" ");
  if(vehicle&&!name.toLowerCase().includes(vehicle.toLowerCase())) parts.push(vehicle);
  parts.push(...filters.body,filters.region.replace(" область"," обл.")||"Вся Україна");
  for(const [key,unit] of [["price"," $"],["year"," р."],["mileage"," тис. км"]]) {
    const range=filters[key],format=n=>key==="year"?String(n):n.toLocaleString("uk-UA");
    if(range.from===null&&range.to===null) continue;
    const text=range.from===null?"до "+format(range.to):range.to===null?"від "+format(range.from):format(range.from)+"–"+format(range.to);
    parts.push((key==="mileage"?"Пробіг ":"")+text+unit);
  }
  parts.push(...filters.fuel,...filters.transmission,filters.onlyDeals?"Від 15% нижче ринку":"Усі ціни");
  return parts.join(", ");
}
function setSavedScope(scope) {
  savedScope=scope;
  for(const [id,panel,value] of [["localTab","localPanel","local"],["subscriptionsTab","cloudPanel","cloud"]]) {
    const selected=scope===value;
    $(id).setAttribute("aria-selected",String(selected));
    $(id).tabIndex=selected?0:-1;
    $(panel).hidden=!selected||!!draftFilters;
  }
  $("refreshCloud").hidden=scope!=="cloud"||!!draftFilters;
}
function renderSavedView() {
  const compose=!!draftFilters;
  $("savedTitle").textContent=compose?"Зберегти пошук":"Мої пошуки";
  $("subscriptionTabs").hidden=compose;
  $("newSavedSearch").hidden=compose;
  $("saveSearchForm").hidden=!compose;
  setSavedScope(savedScope);
}
function closeSubscriptionActions() {
  if($("subscriptionMenu").open) $("subscriptionMenu").close();
}
function showSubscriptionActions(item,options,opener) {
  menuOpener=opener;
  const sheet=$("subscriptionMenu");
  const render=()=>{
    $("subscriptionMenuTitle").textContent=item.name;
    $("subscriptionMenuSummary").textContent=subscriptionSummary(item.filters,item.name);
    $("subscriptionMenuStatus").textContent=options.status;
    $("subscriptionMenuError").hidden=true;
    $("openNotificationSettings").hidden=!options.cloud;
    const actions=$("subscriptionMenuActions");
    actions.replaceChildren();
    function action(label,run,disabled=false,danger=false) {
      const button=managerButton(label,()=>{if(!button.disabled)return run();});
      button.className="subscription-menu-button"+(danger?" destructive":"");
      button.disabled=disabled;actions.append(button);return button;
    }
    action("Відкрити пошук",()=>{closeSubscriptionActions();return openSavedSearch(item.filters);});
    if(options.toggle) action(item.enabled?"Вимкнути сповіщення":"Увімкнути сповіщення",options.toggle,!options.canToggle);
    action(options.cloud?"Видалити підписку":"Видалити пошук",()=>{
      actions.replaceChildren();
      $("subscriptionMenuTitle").textContent=options.cloud?"Видалити підписку?":"Видалити пошук?";
      $("subscriptionMenuStatus").textContent="«"+item.name+"» буде видалено зі списку.";
      $("openNotificationSettings").hidden=true;
      action("Так, видалити",options.remove,false,true);
      action("Скасувати",render);
    },false,true);
  };
  render();
  if(!sheet.open) sheet.showModal();
}
function subscriptionCard(item,options) {
  const card=document.createElement("article");card.className="subscription-card";
  const open=managerButton("",()=>openSavedSearch(item.filters));open.className="subscription-open";
  const icon=document.createElement("img");icon.className="subscription-icon";icon.src="assets/subscription-car.svg";icon.alt="";
  const copy=document.createElement("span");copy.className="subscription-copy";
  const name=document.createElement("span");name.className="subscription-name";name.textContent=item.name;
  const summary=document.createElement("span");summary.className="subscription-summary";summary.textContent=subscriptionSummary(item.filters,item.name);
  copy.append(name,summary);open.append(icon,copy);
  open.setAttribute("aria-label","Відкрити пошук «"+item.name+"». "+summary.textContent);
  const aside=document.createElement("div");aside.className="subscription-aside";
  const more=managerButton("⋯",()=>showSubscriptionActions(item,options,more));more.className="subscription-more";
  more.setAttribute("aria-label","Дії для пошуку «"+item.name+"»");more.setAttribute("aria-haspopup","dialog");
  aside.append(more);
  if(item.enabled) {
    const bell=document.createElement("img");bell.className="subscription-bell";bell.src="assets/subscription-bell.svg";
    bell.alt=options.status;bell.title=options.status;aside.append(bell);
  }
  card.append(open,aside);return card;
}
function renderSavedSearches() {
  const list=$("savedSearchList");
  list.replaceChildren();
  $("savedError").hidden=true;
  $("localTitle").textContent="На цьому пристрої";
  let items;
  try { items=savedAPI.read(window.localStorage); }
  catch { storageError();return; }
  $("localTitle").textContent="На цьому пристрої ("+items.length+")";
  $("localCount").textContent=String(items.length);
  if(!items.length) {
    const empty=document.createElement("p");empty.className="filter-help";
    empty.textContent="Ще немає збережених пошуків. Вибери фільтри та натисни «Зберегти пошук».";
    list.append(empty);return;
  }
  for(const item of items) {
    list.append(subscriptionCard(item,{status:"Лише на цьому пристрої · без сповіщень",remove:()=>{
      try {savedAPI.remove(window.localStorage,item.id);closeSubscriptionActions();renderSavedSearches();}
      catch {$("subscriptionMenuError").hidden=false;$("subscriptionMenuError").textContent="Не вдалося видалити пошук. Спробуй ще раз.";}
    }}));
  }
}
async function openSavedSearch(filters) {
  function explain(message) {$("savedError").hidden=false;$("savedError").textContent=message;}
  if(window.AutoDealLive?.isBusy?.()) {
    explain("Пошук ще виконується. Зачекай, щоб відкрити збережений.");return;
  }
  try {
    if(window.AutoDealCatalog) await window.AutoDealCatalog.ensureFilters(filters);
    applySavedFilters(filters);
    return searchCars();
  } catch(error) { explain(error.message); }
}
function applySavedFilters(raw) {
  const f=savedAPI.normalize(raw);
  if(f.brand && !catalog[f.brand]) throw Error("Ця марка більше не доступна");
  if(f.model && !(catalog[f.brand]||[]).includes(f.model)) throw Error("Ця модель більше не доступна");
  if(f.region && !regions.includes(f.region)) throw Error("Ця область більше не доступна");
  for(const group of advancedGroups) if(f[group.name].some(v=>!group.options.includes(v))) throw Error("Деякі параметри пошуку більше не доступні");
  brand.value=f.brand;brand.dispatchEvent(new Event("change"));model.value=f.model;region.value=f.region;
  for(const key of ["price","year","mileage"]) {
    $(key+"From").value=f[key].from??"";$(key+"To").value=f[key].to??"";
  }
  for(const group of advancedGroups) document.querySelectorAll('input[name="'+group.name+'"]').forEach(input=>input.checked=f[group.name].includes(input.value));
  onlyDeals=f.onlyDeals;
  marketButton.querySelector(".switch").classList.toggle("active",onlyDeals);
  marketButton.setAttribute("aria-checked",String(onlyDeals));
  updateAdvancedCount();
  $("advancedFilters").open=!!$("advancedCount").textContent;
  setSearchTab("search");
}
function openSearchManager(compose) {
  draftFilters=null;
  if(compose) {
    try { draftFilters=savedAPI.normalize(readCurrentFilters()); }
    catch(error) { toast(error.message);return; }
    $("savedName").value=[draftFilters.brand||"Мій пошук",draftFilters.model].filter(Boolean).join(" ");
    $("draftSummary").textContent=summarizeFilters(draftFilters);
  }
  if($("savedDialog").hidden) {
    const active=[...document.querySelectorAll(".nav")].find(item=>item.classList.contains("active"));
    managerReturnTab=["deals","settings"].includes(active?.dataset.tab)?active.dataset.tab:"search";
  }
  managerSession++;
  setSearchTab("saved");
  window.AutoDealLive?.dismissAutoScroll?.();
  renderSavedView();
  renderSavedSearches();
  $("savedTitle").focus({preventScroll:true});
  if(!compose) return window.AutoDealCloudSearches?.refresh();
}
$("saveSearchBtn").addEventListener("click",()=>openSearchManager(true));
function openSettings() {
  setSearchTab("settings");
  $("settingsTitle").focus({preventScroll:true});
  return window.AutoDealCloudSearches?.refreshSettings?.();
}
$("settingsBtn").addEventListener("click",openSettings);
$("openNotificationSettings").addEventListener("click",openSettings);
$("closeSubscriptionMenu").addEventListener("click",closeSubscriptionActions);
$("subscriptionMenu").addEventListener("click",event=>{if(event.target===$("subscriptionMenu"))closeSubscriptionActions();});
$("subscriptionMenu").addEventListener("close",()=>{
  if(menuOpener?.isConnected) menuOpener.focus({preventScroll:true});
  menuOpener=null;
});
for(const [id,scope] of [["localTab","local"],["subscriptionsTab","cloud"]]) {
  $(id).addEventListener("click",()=>setSavedScope(scope));
  $(id).addEventListener("keydown",event=>{
    if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
    event.preventDefault();
    const next=event.key==="Home"?"local":event.key==="End"?"cloud":savedScope==="local"?"cloud":"local";
    setSavedScope(next);$(next==="local"?"localTab":"subscriptionsTab").focus();
  });
}
$("settingsSearches").addEventListener("click",()=>openSearchManager(false));
$("closeSaved").addEventListener("click",()=>setSearchTab(managerReturnTab));
$("newSavedSearch").addEventListener("click",()=>{
  showSearchForm();
  toast("Обери фільтри та натисни «Зберегти пошук».");
});
$("saveSearchForm").addEventListener("submit",event=>{
  event.preventDefault();
  if(!draftFilters) return;
  try {
    savedAPI.save(window.localStorage,draftFilters,$("savedName").value,false);
    draftFilters=null;savedScope="local";renderSavedView();renderSavedSearches();
    toast("Пошук збережено на пристрої");
  } catch(error) {
    storageError();
    if(error.message.includes("Назва")||error.message.includes("20 пошуків")) $("savedError").textContent=error.message;
  }
});
document.querySelectorAll(".nav").forEach(item=>item.addEventListener("click",()=>{
  if(item.dataset.tab==="saved") return openSearchManager(false);
  if(item.dataset.tab==="settings") return openSettings();
  if(item.dataset.tab==="deals") return searchCars({deals:true});
  showSearchForm();
}));
window.addEventListener("storage",event=>{
  if((event.key===savedAPI.KEY||event.key===null)&&!$("savedDialog").hidden) renderSavedSearches();
});
