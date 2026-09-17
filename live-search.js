(function(root) {
  "use strict";
  let busy=false,scrollOnFinish=true,current=null,pollTimer=null;
  const more=document.getElementById("loadMore");
  const scanPanel=document.getElementById("scanProgress"),scanControl=document.getElementById("scanControl");
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
    if(stale) return car.historical_match?
      "Відповідало фільтру під час перевірки. Поточна ціна потребує оновлення на AUTO.RIA.":"Оцінка потребує оновлення";
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
    const minDiscount=filters.minDiscount??15,percentLabel=String(minDiscount).replace(".",",")+"%";
    const visibleCars=filters.onlyDeals?data.cars.filter(car=>(options.fullScan&&car.stale&&car.historical_match)||(!data.stale&&!car.stale&&car.valuation==="sample_median"&&
      car.comparables>=5&&Number.isFinite(car.market)&&car.market>0&&Number.isFinite(car.price_usd)&&
      car.price_usd>0&&car.price_usd*100<=car.market*(100-minDiscount))):data.cars;
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
      const valued=!data.stale&&!car.stale&&Number.isFinite(car.market)&&car.market>0;
      body.append(node("p","market-price",valued?
        "Медіана вибірки ≈ $"+car.market.toLocaleString("en-US")+" · "+car.comparables+" схожих авто":
        valuationMessage(car,data.stale||car.stale)));
      if(options.fullScan&&Number.isFinite(car.checked_at)) body.append(node("p","car-meta",
        "Перевірено: "+new Date(car.checked_at*1000).toLocaleString("uk-UA")));
      if(valued) body.append(node("p","car-meta",car.discount>=0?
        "На "+car.discount+"% нижче медіани вибірки":"На "+Math.abs(car.discount)+"% вище медіани вибірки"));
      const href=safeLink(car.url);
      if(href) {const link=node("a","car-link","ВІДКРИТИ НА AUTO.RIA");link.href=href;link.target="_blank";link.rel="noopener";body.append(link);}
      article.append(body);list.append(article);
    }
    if(!visibleCars.length&&options.fullScan) {
      list.append(node("div","empty",data.complete?
        (filters.onlyDeals?"Серед отриманих оголошень не підтверджено авто на "+percentLabel+" нижче медіани. Частині авто може бракувати даних для оцінки.":"Отримані оголошення не пройшли перевірку вибраних фільтрів або вже недоступні."):
        (["paused","budget_exhausted","incomplete","error"].includes(data.status)?
          "Серед уже перевірених оголошень відповідних авто поки немає. Повну перевірку ще не завершено.":
          "Перевірка триває. Автомобілі з’являтимуться тут, щойно пройдуть перевірку.")));
    } else if(!visibleCars.length) list.append(node("div","empty",filters.onlyDeals?
      (data.stale?"Потрібна свіжа перевірка цін, щоб показати вигідні авто. ":"У перевіреній частині оголошень не підтверджено пропозицій на "+percentLabel+" нижче медіани. ")+
      (options.dealsView?"Повернися до «Пошук» та вимкни «Тільки вигідні авто», щоб переглянути авто без оцінки. ":"Вимкни цей фільтр, щоб бачити авто без оцінки. ")+
      "Це не означає, що вигідних авто на AUTO.RIA немає.":
      "У перевіреній частині оголошень немає авто, які відповідають усім фільтрам."));
  }
  async function search(filters,options={}) {
    if(busy) return;
    if(root.AutoDealCloud.startScan) return startFullScan(filters,options);
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
    if(current?.scanId) {
      current.displayLimit+=50;
      showScan({...current.data,cars:[]},current);
      return pollScan(current);
    }
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
  const activeStatus=status=>["queued","running","waiting"].includes(status);
  function scheduleScan(state,delay=5000) {
    if(pollTimer!==null) root.clearTimeout(pollTimer);
    if(current===state) pollTimer=root.setTimeout(()=>pollScan(state),delay);
  }
  function showScan(data,state) {
    if(state.generation&&state.generation!==data.generation) {
      state.cars.clear();state.after=0;state.cacheAfter=0;
      // The response may have used an offset from a previous run. Fetch the new
      // generation from zero instead of silently skipping its first results.
      state.generation=data.generation;scheduleScan(state,0);return;
    }
    state.generation=data.generation;state.scanId=data.scan_id;state.after=data.after;
    state.cacheAfter=data.cache?.after??state.cacheAfter??0;
    state.data=data;
    let changed=false;
    for(const car of [...(data.cache?.cars||[]),...data.cars]) {
      const previous=state.cars.get(car.id);
      if(!previous||car.checked_at>=previous.checked_at) {
        state.cars.set(car.id,car);changed=true;
      }
    }
    for(const removed of data.removed||[]) {
      if((state.cars.get(removed.id)?.checked_at??-Infinity)<=removed.checked_at)
        changed=state.cars.delete(removed.id)||changed;
    }
    const now=Date.now()/1000;
    for(const [id,car] of state.cars) if(now-car.checked_at>=900&&!car.stale) {
      changed=true;
      state.cars.set(id,{...car,stale:true,historical_match:!!(car.market&&car.price_usd<=car.market*.85),
        market:null,discount:null,comparables:0,valuation:"stale"});
    }
    if(changed||state.renderedLimit!==state.displayLimit||(!state.cars.size&&state.renderedStatus!==data.status))
      renderResults({...data,cars:[...state.cars.values()].slice(0,state.displayLimit)},state.filters,{...state.options,fullScan:true});
    state.renderedLimit=state.displayLimit;state.renderedStatus=data.status;
    const titles={completed:"Перевірку завершено",paused:"Перевірку призупинено",waiting:"Очікуємо продовження",
      budget_exhausted:"Досягнуто межу запитів",incomplete:"Перевірку завершено не повністю",error:"Перевірка потребує уваги"};
    if(scanPanel) {
      scanPanel.hidden=false;
      document.getElementById("scanTitle").textContent=titles[data.status]||
        (data.phase==="discovering"?"Збираємо та перевіряємо оголошення":"Перевіряємо всі оголошення");
      const bar=document.getElementById("scanBar");bar.max=Math.max(1,data.source_total,data.discovered);bar.value=data.inspected;
      let status="Перевірено "+data.inspected+" із "+(data.source_total||data.discovered||"…")+" · Вигідних знайдено: "+data.deals_found+".";
      if(data.phase==="discovering") status="Отримано оголошень: "+data.discovered+" із "+(data.source_total||"…")+". Уже перевірено: "+data.inspected+".";
      if(Number.isInteger(data.valued)) status+=" Оцінено ціну: "+data.valued+".";
      if(data.status==="waiting") status+=data.error==="quota_exceeded"?
        " Продовжимо автоматично приблизно через "+Math.max(1,Math.ceil(data.retry_after_seconds/60))+" хв після відновлення ліміту.":
        " Джерело тимчасово недоступне. Сервер повторить спробу автоматично.";
      else if(activeStatus(data.status)) status+=" Можна закрити мініап — сервер продовжить перевірку.";
      else if(data.status==="budget_exhausted") status+=" Прогрес збережено. Потрібно перевірити залишок пакета AUTO.RIA.";
      else if(data.status==="incomplete") status+=" AUTO.RIA повернула неповні або змінені сторінки. Результат не охоплює всю вибірку.";
      else if(data.status==="paused") status+=" Прогрес збережено. Натисни «Продовжити», щоб відновити перевірку.";
      else if(data.status==="error") status+=" Перевір фільтри та доступ до AUTO.RIA перед повторною спробою.";
      document.getElementById("scanStatus").textContent=status;
      scanControl.textContent=activeStatus(data.status)?"Призупинити":
        ["completed","incomplete"].includes(data.status)?"Перевірити знову":"Продовжити";
    }
    document.getElementById("sourceNote").textContent=
      (data.cache?.total?"Показуємо також раніше перевірені авто за твоїми фільтрами. База ще не охоплює всі оголошення AUTO.RIA. ":"")+
      "Оголошення перевіряються поступово за всіма сторінками AUTO.RIA. "+
      "Оцінка — медіана цін щонайменше 5 схожих авто; це ціни пропозицій, не продажів. "+
      (data.unavailable?"Недоступних або некоректних оголошень: "+data.unavailable+". ":"")+
      "Час перевірки вказаний у картці. Ціна та наявність могли змінитися.";
    const hasMore=data.more_results||data.cache?.more;
    more.hidden=!hasMore&&state.cars.size<=state.displayLimit;more.textContent="Показати ще знайдені авто";
    if(hasMore||activeStatus(data.status)) scheduleScan(state,hasMore?200:5000);
  }
  async function startFullScan(filters,options={},restart=false) {
    if(pollTimer!==null) root.clearTimeout(pollTimer);
    const state={filters:JSON.parse(JSON.stringify(filters)),options,cars:new Map(),after:0,cacheAfter:0,scanId:null,displayLimit:50};
    current=state;busy=true;scrollOnFinish=false;
    const button=document.getElementById("searchBtn"),label=button.textContent;
    button.disabled=true;more.hidden=true;
    if(scanPanel) scanPanel.hidden=true;
    document.getElementById("results").classList.add("show");
    document.getElementById("resultsTitle").textContent=options.dealsView?"Вигідні авто":"Результати пошуку";
    document.getElementById("resultsCriteria").textContent=options.criteria||"";
    document.getElementById("count").textContent="";
    document.getElementById("resultsList").replaceChildren();
    document.getElementById("sourceNote").textContent="Шукаємо серед отриманих авто та запускаємо оновлення…";
    try {showScan(await root.AutoDealCloud.startScan(state.filters,restart),state);}
    catch(error) {document.getElementById("sourceNote").textContent=error.message;}
    finally {busy=false;button.disabled=false;button.textContent=label;}
  }
  async function pollScan(state) {
    if(current!==state||!state.scanId||state.polling) return;
    state.polling=true;more.disabled=true;
    try {
      const data=await root.AutoDealCloud.scan(state.scanId,state.after,state.filters.onlyDeals,state.cacheAfter);
      if(current===state) showScan(data,state);
    } catch(error) {
      if(current===state) {
        document.getElementById("sourceNote").textContent=error.message+" Прогрес перевірки зберігається на сервері.";
        scheduleScan(state,15000);
      }
    } finally {state.polling=false;if(current===state) more.disabled=false;}
  }
  async function controlScan() {
    const state=current;if(!state?.scanId||state.controlling||busy) return;
    state.controlling=true;scanControl.disabled=true;
    try {
      if(["completed","incomplete"].includes(state.data.status)) return await startFullScan(state.filters,state.options,true);
      await root.AutoDealCloud.controlScan(state.scanId,!activeStatus(state.data.status));
      if(current===state) await pollScan(state);
    } catch(error) {if(current===state) document.getElementById("sourceNote").textContent=error.message;}
    finally {state.controlling=false;scanControl.disabled=false;}
  }
  if(scanControl) scanControl.addEventListener("click",controlScan);
  if(document.addEventListener) document.addEventListener("visibilitychange",()=>{
    if(!document.hidden&&current?.scanId) pollScan(current);
  });
  root.AutoDealLive={search,loadMore,isBusy:()=>busy,dismissAutoScroll:()=>{scrollOnFinish=false;}};
})(window);
