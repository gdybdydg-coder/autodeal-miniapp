const {catalog,regions,cars,bodyTypes,fuelTypes,transmissionTypes}=window.AUTO_DEAL_DATA;
const $=id=>document.getElementById(id);const brand=$("brand"),model=$("model"),region=$("region"),marketButton=$("marketSwitch");
Object.keys(catalog).forEach(value=>brand.add(new Option(value,value)));regions.forEach((value,index)=>region.add(new Option(value,index?value:"")));
brand.addEventListener("change",()=>{model.innerHTML='<option value="">Всі моделі</option>';const models=catalog[brand.value]||[];models.forEach(value=>model.add(new Option(value,value)));model.disabled=!models.length});
let onlyDeals=true;marketButton.addEventListener("click",()=>{onlyDeals=!onlyDeals;marketButton.querySelector(".switch").classList.toggle("active",onlyDeals);marketButton.setAttribute("aria-checked",String(onlyDeals))});
const numberValue=id=>$(id).value===""?null:Number($(id).value);

const advancedGroups = [
  {name:"body",options:bodyTypes},
  {name:"fuel",options:fuelTypes},
  {name:"transmission",options:transmissionTypes}
];
function selectedValues(name) {
  return [...document.querySelectorAll('input[name="'+name+'"]:checked')].map(input=>input.value);
}
let reducerChoice=null;
function updateAdvancedCount() {
  if (reducerChoice) {
    const electricSelected=selectedValues("fuel").includes("Електро");
    reducerChoice.label.style.display=electricSelected ? "" : "none";
    reducerChoice.input.disabled=!electricSelected;
    if (!electricSelected) reducerChoice.input.checked=false;
  }
  let total=0;
  for (const group of advancedGroups) {
    const count=selectedValues(group.name).length;
    $(group.name+"Count").textContent=count ? String(count) : "";
    total+=count;
  }
  total+=["mileageFrom","mileageTo"].filter(id=>$(id).value!=="").length;
  $("advancedCount").textContent=total ? String(total) : "";
}
for (const group of advancedGroups) {
  for (const value of group.options) {
    const label=document.createElement("label");
    label.className="choice";
    const input=document.createElement("input");
    input.type="checkbox";
    input.name=group.name;
    input.value=value;
    input.addEventListener("change",updateAdvancedCount);
    const span=document.createElement("span");
    span.textContent=value;
    label.append(input,span);
    if (group.name==="transmission" && value==="Редуктор") {
      reducerChoice={label,input};
    }
    $(group.name+"Options").append(label);
  }
}
updateAdvancedCount();
["mileageFrom","mileageTo"].forEach(id=>$(id).addEventListener("input",updateAdvancedCount));
$("resetAdvanced").addEventListener("click",()=>{
  for (const group of advancedGroups) document.querySelectorAll('input[name="'+group.name+'"]').forEach(input=>input.checked=false);
  $("mileageFrom").value="";
  $("mileageTo").value="";
  updateAdvancedCount();
});
function readRange(fromId,toId,label) {
  const from=numberValue(fromId),to=numberValue(toId);
  for (const id of [fromId,toId]) {
    const value=numberValue(id);
    if ($(id).validity.badInput || (value!==null && (!Number.isFinite(value)||value<0))) {
      $(id).focus();
      throw Error(label+": введи невід’ємне число");
    }
  }
  if (from!==null && to!==null && from>to) {
    $(fromId).focus();
    throw Error(label+": «Від» не може бути більшим за «До»");
  }
  return {from,to};
}
function inRange(value,range) {
  if (range.from===null && range.to===null) return true;
  return Number.isFinite(value) && (range.from===null||value>=range.from) && (range.to===null||value<=range.to);
}
function matchesFilters(car,filters) {
  return (!filters.brand||car.brand===filters.brand)
    && (!filters.model||car.model===filters.model)
    && (!filters.region||car.region===filters.region)
    && inRange(car.price,filters.price)
    && inRange(car.year,filters.year)
    && inRange(Number.isFinite(car.mileage)?car.mileage/1000:null,filters.mileage)
    && (!filters.body.length||filters.body.includes(car.body))
    && (!filters.fuel.length||filters.fuel.includes(car.fuel))
    && (!filters.transmission.length||filters.transmission.includes(car.transmission))
    && (!filters.onlyDeals||(Number.isFinite(car.market)&&car.market>0&&car.price<=car.market*.85));
}
function readCurrentFilters() {
  updateAdvancedCount();
  return {
    brand:brand.value,model:model.value,region:region.value,
    price:readRange("priceFrom","priceTo","Ціна"),
    year:readRange("yearFrom","yearTo","Рік"),
    mileage:readRange("mileageFrom","mileageTo","Пробіг"),
    body:selectedValues("body"),fuel:selectedValues("fuel"),
    transmission:selectedValues("transmission"),onlyDeals
  };
}
let resultsTab="search";
function setSearchTab(tab) {
  if($("subscriptionMenu").open) $("subscriptionMenu").close();
  $("searchIntro").hidden=tab!=="search";
  $("searchFilters").hidden=tab!=="search";
  $("results").hidden=tab!==resultsTab;
  $("savedDialog").hidden=tab!=="saved";
  $("settingsPage").hidden=tab!=="settings";
  window.AutoDealLive?.dismissAutoScroll?.();
  window.scrollTo?.({top:0,left:0,behavior:"instant"});
  document.querySelectorAll(".nav").forEach(item=>{
    const active=item.dataset.tab===tab;
    item.classList.toggle("active",active);
    item.setAttribute("aria-current",active?"page":"false");
  });
}
function showSearchForm() {
  setSearchTab("search");
  $("searchTitle").focus({preventScroll:true});
}
function searchCars(options={}) {
  if(window.AutoDealLive?.isBusy?.()) {
    toast("Пошук ще виконується. Зачекай на результат.");return;
  }
  try { const filters=readCurrentFilters();
    if(!window.AutoDealLive) throw Error("Онови Mini App, щоб завантажити пошук AUTO.RIA.");
    const dealsView=options.deals===true;
    if(dealsView) filters.onlyDeals=true;
    resultsTab=dealsView?"deals":"search";
    setSearchTab(dealsView?"deals":"search");
    if(dealsView) $("resultsTitle").focus({preventScroll:true});
    const criteria=typeof summarizeFilters==="function"?summarizeFilters(filters):"";
    return window.AutoDealLive.search(filters,{dealsView,criteria});
  }
  catch (error) { toast(error.message); }
}
$("editResultsFilters").addEventListener("click",showSearchForm);
setSearchTab("search");
function render(list){$("results").classList.add("show");$("count").textContent=`Знайдено: ${list.length}`;$("resultsList").innerHTML=list.length?list.map(car=>{const discount=Math.round((1-car.price/car.market)*100);return `<article class="car"><div class="car-photo-wrap"><img class="car-photo" src="${car.image}" alt="${car.brand} ${car.model}" loading="lazy"><span class="badge">−${discount}% ВІД РИНКУ</span><button class="save" data-save="${car.id}" aria-label="Зберегти">♡</button></div><div class="car-body"><div class="car-top"><div class="car-name">${car.brand} ${car.model}</div><div class="car-price">$${car.price.toLocaleString("en-US")}</div></div><div class="car-meta">${car.year} • ${car.fuel} • ${Math.round(car.mileage/1000)} тис. км</div><div class="car-meta">${car.body || "Кузов не вказаний"} • ${car.transmission || "КПП не вказана"}</div><div class="market-price"><span>Ринок ≈ $${car.market.toLocaleString("en-US")}</span><span class="deal">↓ ${discount}%</span></div><div class="location">📍 ${car.region}</div><a class="car-link" href="${car.url}" data-demo="${car.url==="#"}">ВІДКРИТИ ОГОЛОШЕННЯ</a></div></article>`}).join(""):'<div class="empty">За цими фільтрами авто не знайдено.</div>';$("results").scrollIntoView({behavior:"smooth",block:"start"})}
function toast(message){const element=$("toast");element.textContent=message;element.classList.add("show");clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>element.classList.remove("show"),2200)}
$("searchBtn").addEventListener("click",searchCars);$("resultsList").addEventListener("click",event=>{const save=event.target.closest("[data-save]");if(save){save.textContent=save.textContent==="♡"?"♥":"♡";toast(save.textContent==="♥"?"Авто збережено":"Авто видалено зі збережених")}const demo=event.target.closest('[data-demo="true"]');if(demo){event.preventDefault();toast("Посилання підключимо до реальних оголошень")}});
if(window.Telegram?.WebApp){Telegram.WebApp.ready();Telegram.WebApp.expand();}
