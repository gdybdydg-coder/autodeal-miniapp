"use strict";
const savedAPI=window.AutoDealSaved;
let draftFilters=null,pendingDelete=null;
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
function renderSavedSearches() {
  const list=$("savedSearchList");
  list.replaceChildren();
  $("savedError").hidden=true;
  $("localTitle").textContent="На цьому пристрої";
  let items;
  try { items=savedAPI.read(window.localStorage); }
  catch { storageError();return; }
  $("localTitle").textContent="На цьому пристрої ("+items.length+")";
  if(!items.length) {
    const empty=document.createElement("p");empty.className="filter-help";
    empty.textContent="Ще немає збережених пошуків. Вибери фільтри та натисни «Зберегти пошук».";
    list.append(empty);return;
  }
  for(const item of items) {
    const article=document.createElement("article");article.className="saved-search-card";
    const name=document.createElement("h3");name.textContent=item.name;
    const summary=document.createElement("p");summary.className="filter-help";summary.textContent=summarizeFilters(item.filters);
    const status=document.createElement("p");status.className="filter-help";
    status.textContent="Лише на цьому пристрої · без сповіщень";
    const actions=document.createElement("div");actions.className="saved-actions";
    actions.append(managerButton("Відкрити пошук",()=>openSavedSearch(item.filters)));
    if(pendingDelete===item.id) {
      actions.append(managerButton("Так, видалити",()=>{
        try { savedAPI.remove(window.localStorage,item.id);pendingDelete=null;renderSavedSearches(); }
        catch { storageError(); }
      }),managerButton("Скасувати",()=>{pendingDelete=null;renderSavedSearches();}));
    } else actions.append(managerButton("Видалити",()=>{pendingDelete=item.id;renderSavedSearches();}));
    article.append(name,summary,status,actions);list.append(article);
  }
}
function openSavedSearch(filters) {
  function explain(message) {$("savedError").hidden=false;$("savedError").textContent=message;}
  if(window.AutoDealLive?.isBusy?.()) {
    explain("Пошук ще виконується. Зачекай, щоб відкрити збережений.");return;
  }
  try {
    applySavedFilters(filters);
    $("savedDialog").close();
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
  draftFilters=null;pendingDelete=null;
  if(compose) {
    try { draftFilters=savedAPI.normalize(readCurrentFilters()); }
    catch(error) { toast(error.message);return; }
    $("savedName").value=[draftFilters.brand||"Мій пошук",draftFilters.model].filter(Boolean).join(" ");
    $("draftSummary").textContent=summarizeFilters(draftFilters);
  }
  if(!$("savedDialog").open) {
    const active=[...document.querySelectorAll(".nav")].find(item=>item.classList.contains("active"));
    managerReturnTab=active?.dataset.tab==="deals"?"deals":"search";
  }
  managerSession++;
  setSearchTab("saved");
  window.AutoDealLive?.dismissAutoScroll?.();
  $("savedTitle").textContent=compose?"Зберегти пошук":"Мої пошуки";
  $("newSavedSearch").hidden=compose;
  $("saveSearchForm").hidden=!compose;
  renderSavedSearches();
  if(!$("savedDialog").open) $("savedDialog").showModal();
  $("savedDialog").scrollTop=0;
  $("closeSaved").focus();
  if(!compose) return window.AutoDealCloudSearches?.refresh();
}
$("saveSearchBtn").addEventListener("click",()=>openSearchManager(true));
$("settingsBtn").addEventListener("click",()=>openSearchManager(false));
$("closeSaved").addEventListener("click",()=>$("savedDialog").close());
$("savedDialog").addEventListener("close",()=>{
  if([...document.querySelectorAll(".nav")].some(item=>item.dataset.tab==="saved"&&item.classList.contains("active")))
    setSearchTab(managerReturnTab);
});
$("newSavedSearch").addEventListener("click",()=>{
  $("savedDialog").close();showSearchForm();
  toast("Обери фільтри та натисни «Зберегти пошук».");
});
$("saveSearchForm").addEventListener("submit",event=>{
  event.preventDefault();
  if(!draftFilters) return;
  try {
    savedAPI.save(window.localStorage,draftFilters,$("savedName").value,false);
    $("saveSearchForm").hidden=true;draftFilters=null;renderSavedSearches();
    toast("Пошук збережено на пристрої");
  } catch(error) {
    storageError();
    if(error.message.includes("Назва")||error.message.includes("20 пошуків")) $("savedError").textContent=error.message;
  }
});
document.querySelectorAll(".nav").forEach(item=>item.addEventListener("click",()=>{
  if(item.dataset.tab==="saved"||item.dataset.tab==="settings") return openSearchManager(false);
  if(item.dataset.tab==="deals") return searchCars({deals:true});
  showSearchForm();
}));
window.addEventListener("storage",event=>{
  if((event.key===savedAPI.KEY||event.key===null)&&$("savedDialog").open) renderSavedSearches();
});
