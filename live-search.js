(function(root) {
  "use strict";
  let busy=false,scrollOnFinish=true,current=null;
  const more=document.getElementById("loadMore");
  const node=(tag,cls,text)=>{
    const el=document.createElement(tag);el.className=cls;
    if(text!==undefined) el.textContent=text;
    return el;
  };
  function safeLink(value,photo=false) {
    try {
      const url=new URL(value);
      return url.protocol==="https:" && (photo?url.hostname.endsWith(".riastatic.com"):url.hostname==="auto.ria.com")?url.href:null;
    } catch(_) {return null;}
  }
  function valuationMessage(car,stale) {
    if(stale) return "Оцінка потребує оновлення";
    if(car.valuation==="pending") return "Оцінку ще не завершено — натисни «Продовжити оцінку»";
    const reasons=Array.isArray(car.valuation_reasons)?car.valuation_reasons:[];
    if(reasons.includes("unverified_condition")) return "Не підтверджено стан або статус авто для оцінки";
    if(reasons.some(reason=>typeof reason==="string"&&reason.startsWith("missing_")))
      return "Не вистачає даних про версію авто для точного порівняння";
    if(car.valuation==="mixed_sample") return "Ціни аналогів надто різняться для надійної оцінки";
    if(Number.isInteger(car.comparables)&&car.comparables>0&&car.comparables<5)
      return "Схожих авто: "+car.comparables+" із потрібних 5";
    return "Недостатньо схожих авто для оцінки";
  }
  function renderResults(data,filters,options) {
    const list=document.getElementById("resultsList");list.replaceChildren();
    const visibleCars=filters.onlyDeals?data.cars.filter(car=>!data.stale&&car.valuation==="sample_median"&&
      car.comparables>=5&&Number.isFinite(car.market)&&car.market>0&&Number.isFinite(car.price_usd)&&
      car.price_usd>0&&car.price_usd<=car.market*.85):data.cars;
    document.getElementById("count").textContent="Показано: "+visibleCars.length;
    const checked=Number.isFinite(data.checked_at)?new Date(data.checked_at*1000).toLocaleString("uk-UA"):null;
    document.getElementById("sourceNote").textContent=
      (data.stale?"Раніше отримані дані. Ціни та наявність авто могли змінитися. ":data.cached?"Повторно показано отримані дані. ":"")+
      (checked?"Перевірено: "+checked+". ":"")+
      "Переглянуто оголошень: "+data.inspected+". На час перевірки за фільтрами: "+data.source_total+
      ". "+(data.next_cursor?"Доступне продовження перевірки. ":"")+
      (data.warnings.length?"Частину перевірки не завершено через ліміт або недоступність даних. ":"")+
      "Оцінка — медіана цін щонайменше 5 схожих авто; це ціни пропозицій, не продажів.";
    if(data.warnings.includes("quota_exceeded") && Number.isFinite(data.quota?.retry_after_seconds) && data.quota.retry_after_seconds>0)
      document.getElementById("sourceNote").textContent+=" Нові запити можна повторити приблизно через "+Math.ceil(data.quota.retry_after_seconds/60)+" хв.";
    for(const car of visibleCars) {
      const article=node("article","car");
      const photo=safeLink(car.image,true);
      if(photo) {
        const img=node("img","car-photo");img.src=photo;img.alt=car.title;img.loading="lazy";
        const wrap=node("div","car-photo-wrap");wrap.append(img);article.append(wrap);
      }
      const body=node("div","car-body");
      body.append(node("div","car-name",car.title),node("div","car-price","$"+car.price_usd.toLocaleString("en-US")),
        node("div","car-meta",car.year+" • "+car.fuel+" • "+Math.round(car.mileage/1000)+" тис. км"),
        node("div","car-meta",car.body+" • "+car.transmission),node("div","location",car.region));
      const valued=!data.stale&&Number.isFinite(car.market)&&car.market>0;
      body.append(node("p","market-price",valued?
        "Медіана вибірки ≈ $"+car.market.toLocaleString("en-US")+" · "+car.comparables+" схожих авто":
        valuationMessage(car,data.stale)));
      if(valued) body.append(node("p","car-meta",car.discount>=0?
        "На "+car.discount+"% нижче медіани вибірки":"На "+Math.abs(car.discount)+"% вище медіани вибірки"));
      const href=safeLink(car.url);
      if(href) {const link=node("a","car-link","ВІДКРИТИ НА AUTO.RIA");link.href=href;link.target="_blank";link.rel="noopener";body.append(link);}
      article.append(body);list.append(article);
    }
    if(!visibleCars.length) list.append(node("div","empty",filters.onlyDeals?
      (data.stale?"Потрібна свіжа перевірка цін, щоб показати вигідні авто. ":"У перевіреній частині оголошень не підтверджено пропозицій на 15% нижче медіани. ")+
      (options.dealsView?"Повернися до «Пошук» та вимкни «Тільки вигідні авто», щоб переглянути авто без оцінки. ":"Вимкни цей фільтр, щоб бачити авто без оцінки. ")+
      "Це не означає, що вигідних авто на AUTO.RIA немає.":
      "У перевіреній частині оголошень немає авто, які відповідають усім фільтрам."));
  }
  async function search(filters,options={}) {
    if(busy) return;
    current={filters:JSON.parse(JSON.stringify(filters)),options,cars:new Map(),data:null,cursor:null};
    more.hidden=true;
    busy=true;scrollOnFinish=!options.dealsView;
    const button=document.getElementById("searchBtn"),label=button.textContent;
    const section=document.getElementById("results"),list=document.getElementById("resultsList");
    button.disabled=true;button.textContent="Перевіряю AUTO.RIA…";
    section.classList.add("show");
    document.getElementById("resultsTitle").textContent=options.dealsView?"Вигідні авто":"Результати пошуку";
    document.getElementById("resultsCriteria").textContent=options.criteria||"";
    document.getElementById("count").textContent="";
    document.getElementById("sourceNote").textContent="Завантажую оголошення та перевіряю ціни. Це може тривати близько хвилини.";
    list.replaceChildren();
    try {accept(await root.AutoDealCloud.search(current.filters));}
    catch(error) {
      document.getElementById("sourceNote").textContent="Пошук не завершено.";
      list.replaceChildren(node("div","empty",error.message));
    } finally {
      busy=false;button.disabled=false;button.textContent=label;
      if(scrollOnFinish) section.scrollIntoView({behavior:"smooth",block:"start"});
    }
  }
  function accept(data) {
    for(const [index,car] of data.cars.entries()) current.cars.set(car.id||car.url||car.title+index,car);
    const previous=current.data;
    current.data={...data,cars:[...current.cars.values()],
      inspected:(previous?.inspected||0)+(data.inspected||0),
      checked_at:previous?.checked_at&&data.checked_at?Math.min(previous.checked_at,data.checked_at):data.checked_at,
      stale:!!(previous?.stale||data.stale)};
    current.cursor=data.next_cursor||null;
    renderResults(current.data,current.filters,current.options);
    more.hidden=!current.cursor;more.textContent=data.pending_valuations?"Продовжити оцінку":"Показати ще";
  }
  async function loadMore() {
    if(busy||!current?.cursor) return;
    busy=true;more.disabled=true;
    const button=document.getElementById("searchBtn");button.disabled=true;
    const label=more.textContent;more.textContent="Перевіряємо…";
    try {accept(await root.AutoDealCloud.search(current.filters,current.cursor));}
    catch(error) {
      document.getElementById("sourceNote").textContent=error.message;
      more.textContent=label;
    } finally {busy=false;more.disabled=false;button.disabled=false;}
  }
  more.addEventListener("click",loadMore);
  root.AutoDealLive={search,loadMore,isBusy:()=>busy,dismissAutoScroll:()=>{scrollOnFinish=false;}};
})(window);
