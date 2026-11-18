(function(root){
  "use strict";
  const PRICES=Object.freeze([
    {id:1,min:0,max:2000,label:"0–2 000 $"},{id:2,min:2000,max:5000,label:"2 000–5 000 $"},
    {id:3,min:5000,max:10000,label:"5 000–10 000 $"},{id:4,min:10000,max:15000,label:"10 000–15 000 $"},
    {id:5,min:15000,max:20000,label:"15 000–20 000 $"},{id:6,min:20000,max:null,label:"Від 20 000 $"}
  ]);
  const PROFITS=Object.freeze([5,10,15,20,30,40,50]);
  const STATUS={active:"Активний",paused:"На паузі",subscription_expired:"Пауза: підписка завершилася"};
  const copy=value=>JSON.parse(JSON.stringify(value));
  const integer=value=>Number.isSafeInteger(value)&&value>0;
  const radarId=id=>typeof id==="string"&&/^[a-f0-9]{32}$/.test(id);
  function draftFrom(radar,slot=1,year=new Date().getFullYear()){
    if(!radar) return {name:"Радар "+slot,all_brands:false,brands:[],price_category:null,region_ids:[],year_from:2000,year_to:year,min_profit:10};
    return {name:radar.name,all_brands:radar.all_brands===true,brands:radar.brands.map(b=>({brand_id:b.brand_id,model_ids:[...(b.model_ids||b.models?.map(m=>m.model_id)||[])]})),
      price_category:radar.price_category,region_ids:[...radar.region_ids],year_from:radar.year_from,year_to:radar.year_to,min_profit:radar.min_profit};
  }
  function validate(draft,catalog,step=null){
    const checks=[];
    if(step===null||step===0){
      if(typeof draft.name!=="string"||!draft.name.trim()||draft.name.trim().length>60) checks.push("Введіть назву радара: від 1 до 60 символів.");
      if(draft.all_brands!==undefined&&typeof draft.all_brands!=="boolean") checks.push("Оберіть режим: усі марки або окремі марки.");
      if(!Array.isArray(draft.brands)) checks.push("Перевірте вибрані марки авто.");
      else if(draft.all_brands===true){if(draft.brands.length)checks.push("Для режиму «Усі марки» приберіть окремі марки й моделі.");}
      else if(draft.brands.length===0) checks.push("Оберіть хоча б одну марку авто або «Усі марки».");
      else {
        if(draft.brands.length>30) checks.push("В одному радарі можна вибрати до 30 марок.");
        const selected=new Set();
        for(const brand of draft.brands){
          if(!integer(brand.brand_id)||selected.has(brand.brand_id)||!catalog.brands.some(b=>b.id===brand.brand_id)) checks.push("Перевірте вибрані марки авто.");
          selected.add(brand.brand_id);
          if(!Array.isArray(brand.model_ids)){checks.push("Перевірте список моделей цієї марки.");continue;}
          if(new Set(brand.model_ids).size!==brand.model_ids.length||brand.model_ids.some(id=>!integer(id))) checks.push("Моделі не повинні повторюватися.");
          if(brand.model_ids.length>100) checks.push("Для однієї марки можна вибрати до 100 моделей.");
          const known=catalog.modelsByBrand?.[brand.brand_id];
          if(brand.model_ids.length&&(!known||brand.model_ids.some(id=>!known.some(m=>m.id===id&&m.brand_id===brand.brand_id)))) checks.push("Дочекайтеся завантаження моделей і перевірте їхню відповідність марці.");
        }
        if(draft.brands.reduce((sum,b)=>sum+Math.max(1,b.model_ids?.length||0),0)>200) checks.push("Оберіть не більше 200 поєднань марок і моделей.");
      }
    }
    if(step===null||step===1){
      if(!PRICES.some(p=>p.id===draft.price_category)) checks.push("Оберіть одну категорію ціни.");
      if(!Array.isArray(draft.region_ids)||new Set(draft.region_ids).size!==draft.region_ids.length||draft.region_ids.some(id=>!integer(id)||!catalog.regions.some(r=>r.id===id))) checks.push("Перевірте вибрані області.");
    }
    if(step===null||step===2){
      const min=catalog.year_min||1900,max=catalog.year_max||new Date().getFullYear()+1;
      if(!Number.isInteger(draft.year_from)||!Number.isInteger(draft.year_to)||draft.year_from<min||draft.year_to>max||draft.year_from>draft.year_to) checks.push("Оберіть коректні роки: «Рік до» не може бути раніше за «Рік від».");
      if(!PROFITS.includes(draft.min_profit)) checks.push("Оберіть один із запропонованих відсотків вигоди.");
    }
    return [...new Set(checks)];
  }
  function canonical(draft){
    return {...copy(draft),all_brands:draft.all_brands===true,name:draft.name.trim(),brands:draft.brands.map(b=>({brand_id:b.brand_id,model_ids:[...b.model_ids].sort((a,b)=>a-b)})).sort((a,b)=>a.brand_id-b.brand_id),region_ids:[...draft.region_ids].sort((a,b)=>a-b)};
  }
  function selectAllBrands(draft,checked){
    draft.all_brands=checked===true;
    if(draft.all_brands)draft.brands=[];
  }
  function toggleBrand(draft,id,checked){
    if(checked)draft.all_brands=false;
    if(checked&&!draft.brands.some(b=>b.brand_id===id)) draft.brands.push({brand_id:id,model_ids:[]});
    if(!checked) draft.brands=draft.brands.filter(b=>b.brand_id!==id);
  }
  function toggleModel(draft,brandId,modelId,checked){
    const brand=draft.brands.find(b=>b.brand_id===brandId);if(!brand) return;
    brand.model_ids=checked?[...new Set([...brand.model_ids,modelId])]:brand.model_ids.filter(id=>id!==modelId);
  }
  function toggleRegion(draft,id,checked){draft.region_ids=checked?[...new Set([...draft.region_ids,id])]:draft.region_ids.filter(value=>value!==id);}
  function slots(radars){
    if(!Array.isArray(radars)||radars.length>3||radars.some(r=>!radarId(r.id)||!Number.isInteger(r.slot)||r.slot<1||r.slot>3)||new Set(radars.map(r=>r.slot)).size!==radars.length) throw Error("Не вдалося прочитати список радарів. Оновіть сторінку.");
    return [1,2,3].map(slot=>({slot,radar:radars.find(r=>r.slot===slot)||null}));
  }
  function priceMatches(category,price){const range=PRICES.find(r=>r.id===category);return !!range&&Number.isFinite(price)&&price>=range.min&&(range.max===null||price<range.max);}
  function describeTrace(trace){
    if(trace.state==="not_observed")return "Запису про це оголошення у ваших радарах немає. Це не доводить, що оголошення не публікували або що його не пропущено. Передайте ID підтримці.";
    if(trace.telegram_accepted)return "Telegram прийняв повідомлення для вашого чату. Це не підтверджує доставку на телефон або прочитання.";
    const delivery={pending:"Повідомлення у черзі.",sending:"Надсилання розпочато, результат ще невідомий.",uncertain:"Результат надсилання невідомий; автоматичного повтору немає.",cancelled:"Надсилання скасовано.",failed:"Сталася помилка надсилання."};
    if(delivery[trace.delivery_state])return delivery[trace.delivery_state]+" Якщо потрібна допомога, передайте ID оголошення підтримці.";
    const states={inactive_subscription:"Радар зупинено або змінено; старі оголошення не надсилаються",checking:"Перевірка триває",listing_unavailable:"Оголошення недоступне для перевірки",cancelled:"Перевірку скасовано",matched:"Авто відповідає умовам, але підтвердження надсилання немає",source_exclusion:"Категорію оголошення виключено з моніторингу",unresolved_filter:"Відповідність умовам не підтверджено",filter_mismatch:"Авто не відповідає умовам радара",below_min_discount:"Вигода нижча за вибраний поріг",checked_without_match:"Перевірено, але відповідність радару не підтверджено",checked_without_evidence:"Запис про перевірку є, але причину не підтверджено"};
    const reasons={quota_exceeded:"ліміт AUTO.RIA",busy:"очікування черги",search_limit:"ліміт перевірки",connection_error:"проблема з’єднання",upstream_error:"помилка AUTO.RIA",listing_unavailable:"оголошення недоступне"};
    const rows=Array.isArray(trace.subscriptions)?trace.subscriptions:[];
    const messages=[...new Set(rows.map(row=>(states[row.state]||"Стан потребує перевірки")+(reasons[row.reason]?" ("+reasons[row.reason]+")":"")))];
    return (messages.length?messages.join("; "):"Підтвердженого стану немає")+". Перевірка не робила нових запитів до AUTO.RIA.";
  }
  function createStore(api){
    let snapshot={radars:[],limit:3,access:{active:false}},generation=0;
    return {
      get snapshot(){return copy(snapshot);},
      async load(){const ticket=++generation;const result=await api.radars();slots(result.radars);if(ticket===generation) snapshot=result;return copy(snapshot);},
      async save(draft,existing,requestId){
        const payload=canonical(draft);
        const result=existing?await api.updateRadar(existing.id,{...payload,revision:existing.revision}):await api.createRadar({...payload,...(requestId?{client_request_id:requestId}:{})});
        generation++;snapshot.radars=snapshot.radars.filter(r=>r.id!==result.id);snapshot.radars.push(result);slots(snapshot.radars);return result;
      },
      async status(radar,active){const result=await api.radarStatus(radar.id,active,radar.revision);generation++;snapshot.radars=snapshot.radars.map(r=>r.id===result.id?result:r);return result;},
      async remove(radar,confirmed){if(confirmed!==true) throw Error("Підтвердьте видалення радара.");await api.deleteRadar(radar.id,radar.revision);generation++;snapshot.radars=snapshot.radars.filter(r=>r.id!==radar.id);}
    };
  }
  function mount(document,api){
    const $=id=>document.getElementById(id),store=createStore(api);
    let page="radars",busy=false,loaded=false,draft=null,editing=null,step=0,deleting=null,toastTimer=null,createRequestId=null,pendingNavigation=null;
    let catalog={brands:[],regions:[],year_min:1900,year_max:new Date().getFullYear()+1,modelsByBrand:{}};
    let catalogReady=false,catalogPromise=null,modelPromises=new Map(),modelErrors=new Map(),modelQueries=new Map(),modelSerial=Promise.resolve();
    const node=(tag,text,className)=>{const el=document.createElement(tag);if(text!==undefined) el.textContent=text;if(className) el.className=className;return el;};
    const message=(id,text)=>{$(id).textContent=text||"";$(id).hidden=!text;};
    function announce(text){$("toast").textContent=text;$("toast").classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("toast").classList.remove("show"),4000);}
    function button(text,handler,className="secondary-button"){const el=node("button",text,className);el.type="button";el.addEventListener("click",handler);el.disabled=busy;return el;}
    function showPage(next){if(busy)return;page=next;$("radarPage").hidden=next!=="radars";$("radarEditor").hidden=next!=="editor";$("settingsPage").hidden=next!=="settings";for(const nav of document.querySelectorAll("[data-page]")){const active=nav.dataset.page===(next==="editor"?"radars":next);nav.classList.toggle("active",active);if(active)nav.setAttribute("aria-current","page");else nav.removeAttribute("aria-current");}if(next==="settings") void refreshSettings();root.scrollTo?.({top:0,behavior:"auto"});}
    function setBusy(value){busy=value;$("refreshRadars").disabled=value;for(const el of document.querySelectorAll("#radarEditor input,#radarEditor select,#radarEditor button,#deleteRadarDialog button"))el.disabled=value;for(const el of document.querySelectorAll(".radar-card-actions button,.create-button"))el.disabled=value;}
    function brandLabel(brand){return brand.brand_name||catalog.brands.find(b=>b.id===brand.brand_id)?.name||"Марка недоступна";}
    function facts(radar){
      const facts=node("dl",undefined,"radar-facts");
      const add=(label,value,wide=false)=>{const wrapper=node("div",undefined,wide?"wide":undefined);wrapper.append(node("dt",label),node("dd",value));facts.append(wrapper);};
      add("Марки й моделі",radar.all_brands===true?"Усі марки · усі моделі":radar.brands.map(b=>{const ids=b.model_ids||b.models?.map(m=>m.model_id)||[];const names=ids.map(id=>b.models?.find(m=>m.model_id===id)?.name||catalog.modelsByBrand[b.brand_id]?.find(m=>m.id===id)?.name||"Модель недоступна");return brandLabel(b)+" · "+(ids.length?names.join(", "):"усі моделі");}).join("; "));
      add("Ціна",PRICES.find(p=>p.id===radar.price_category)?.label||"Не вибрано");add("Роки випуску",radar.year_from+"–"+radar.year_to);
      add("Регіони",radar.region_ids.length?radar.region_ids.map(id=>radar.regions?.find(r=>r.id===id)?.name||catalog.regions.find(r=>r.id===id)?.name||"Область недоступна").join(", "):"Уся Україна",true);
      add("Мінімальна вигода",radar.min_profit+"%");return facts;
    }
    function renderDashboard(){
      const snapshot=store.snapshot,items=slots(snapshot.radars);$("radarOccupied").textContent="Зайнято місць: "+snapshot.radars.length+"/3";$("radarActive").textContent="Активні радари: "+snapshot.radars.filter(r=>r.status==="active").length+"/3 · Вільно: "+(3-snapshot.radars.length);
      document.querySelectorAll(".slot-dots i").forEach((dot,i)=>dot.classList.toggle("occupied",!!items[i].radar));
      $("radarAccess").textContent=snapshot.access?.active?"Підписка активна. Вмикайте потрібні радари — повідомлення надходитимуть у Telegram.":"Моніторинг потребує активної підписки. Створювати, редагувати й зберігати радари можна вже зараз.";$("radarAccess").classList.toggle("expired",!snapshot.access?.active);
      const cards=items.map(({slot,radar})=>{
        const card=node("article",undefined,"radar-card "+(radar?(radar.status==="active"?"active-card":""):"empty-radar"));card.dataset.slot=slot;
        if(!radar){const text=node("div");text.append(node("h2","Місце "+slot+" вільне"),node("p","Окремий радар для інших авто чи бюджету."),button("+ Створити радар",()=>openEditor(null,slot),"secondary-button create-button"));card.append(node("span","+","radar-icon"),text);return card;}
        const head=node("div",undefined,"radar-card-head"),title=node("div",undefined,"radar-card-heading");title.append(node("p","РАДАР "+slot,"radar-slot-number"),node("h2",radar.name),node("span",STATUS[radar.status]||"Стан потребує перевірки","status-badge "+(Object.hasOwn(STATUS,radar.status)?radar.status:"")));head.append(node("span","◉","radar-icon"),title);card.append(head,facts(radar));
        const actions=node("div",undefined,"radar-card-actions");actions.append(button("Редагувати",()=>openEditor(radar,slot)),button(radar.status==="active"?"На паузу":"Увімкнути",()=>changeStatus(radar),radar.status==="active"?"secondary-button":"primary-button"));const remove=button("×",()=>askDelete(radar),"secondary-button delete-button");remove.setAttribute("aria-label","Видалити радар «"+radar.name+"»");actions.append(remove);card.append(actions);return card;
      });$("radarSlots").replaceChildren(...cards);$("radarSlots").setAttribute("aria-busy","false");
    }
    async function refresh(){if(busy)return;setBusy(true);message("radarError","");try{await store.load();loaded=true;renderDashboard();}catch(error){message("radarError",error.message);if(!loaded)$("radarSlots").replaceChildren(node("p","Не вдалося завантажити радари. Натисніть «Оновити радари».","loading-state"));}finally{setBusy(false);$("radarSlots").setAttribute("aria-busy","false");}}
    async function loadCatalog(){
      if(catalogReady)return;if(catalogPromise)return catalogPromise;
      catalogPromise=(async()=>{const response=await api.radarCatalog();if(!Array.isArray(response.brands)||!response.brands.length||!Array.isArray(response.regions)||!response.regions.length||[...response.brands,...response.regions].some(v=>!integer(v.id)||typeof v.name!=="string"))throw Error("Довідник авто тимчасово недоступний. Спробуйте оновити його.");catalog={...catalog,...response,modelsByBrand:catalog.modelsByBrand};catalogReady=true;message("catalogError","");$("retryRadarCatalog").hidden=true;})().finally(()=>{catalogPromise=null;});return catalogPromise;
    }
    function loadModels(brandId){
      if(catalog.modelsByBrand[brandId])return Promise.resolve();if(modelPromises.has(brandId))return modelPromises.get(brandId);
      const promise=modelSerial.catch(()=>{}).then(async()=>{if(!draft||draft.all_brands||!draft.brands.some(b=>b.brand_id===brandId))return;const data=await api.radarCatalog(brandId);if(!Array.isArray(data.models)||data.models.some(m=>!integer(m.id)||m.brand_id!==brandId||typeof m.name!=="string"))throw Error("Не вдалося прочитати моделі цієї марки.");catalog.modelsByBrand[brandId]=data.models;modelErrors.delete(brandId);}).catch(error=>{modelErrors.set(brandId,error.message);throw error;}).finally(()=>{modelPromises.delete(brandId);if(draft)renderModels();});modelSerial=promise;modelPromises.set(brandId,promise);return promise;
    }
    async function openEditor(radar,slot){
      if(busy||!loaded)return;editing=radar?copy(radar):null;draft=draftFrom(radar,slot);createRequestId=radar?null:(root.crypto?.randomUUID?.()||("radar-"+Date.now().toString(36)+"-"+Math.random().toString(36).slice(2)));step=0;$("radarName").value=draft.name;$("brandSearch").value="";$("editorTitle").textContent=radar?"Редагування радара":"Новий радар";$("editorKicker").textContent="МІСЦЕ "+slot+" ІЗ 3";message("editorError","");message("catalogError","");showPage("editor");renderStep();
      try{await loadCatalog();if(!draft)return;renderControls();for(const b of draft.brands)void loadModels(b.brand_id).catch(()=>{});}catch(error){message("catalogError",error.message);$("retryRadarCatalog").hidden=false;}
    }
    function checkbox(label,checked,onChange){const wrapper=node("label",undefined,"check-choice"),input=node("input");input.type="checkbox";input.checked=checked;input.addEventListener("change",()=>onChange(input.checked));wrapper.append(input,node("span",label));return wrapper;}
    function syncBrandMode(){$("radarAllBrands").checked=draft.all_brands===true;$("allBrandsHint").hidden=draft.all_brands!==true;}
    function renderBrands(){syncBrandMode();const query=$("brandSearch").value.trim().toLocaleLowerCase("uk-UA");const brands=catalog.brands.filter(b=>b.name.toLocaleLowerCase("uk-UA").includes(query));$("radarBrands").replaceChildren(node("legend","Марки авто","sr-only"),...brands.map(b=>checkbox(b.name,draft.brands.some(s=>s.brand_id===b.id),checked=>{toggleBrand(draft,b.id,checked);syncBrandMode();renderModels(checked?b.id:null);if(checked)void loadModels(b.id).catch(()=>{});})));$("brandSearchEmpty").hidden=brands.length!==0;}
    function renderModels(autoOpen){
      if(!draft)return;if(draft.all_brands){$("radarModels").replaceChildren();return;}const focused=document.activeElement?.dataset?.modelSearch;const cursor=focused?document.activeElement.selectionStart:null;const open=new Set([...$("radarModels").querySelectorAll("details[open]")].map(el=>Number(el.dataset.brandId)));
      const groups=draft.brands.map(selected=>{const id=selected.brand_id,group=node("details",undefined,"model-group");group.dataset.brandId=id;group.open=open.has(id)||draft.brands.length===1||autoOpen===id;const summary=node("summary",brandLabel(selected));summary.append(node("small",selected.model_ids.length?"обрано: "+selected.model_ids.length:"усі моделі"));group.append(summary);
        const all=checkbox("Усі моделі цієї марки",selected.model_ids.length===0,()=>{selected.model_ids=[];renderModels();});all.className="models-all";group.append(all);
        const models=catalog.modelsByBrand[id];if(!models){group.append(node("p",modelErrors.get(id)||"Завантажуємо моделі…","filter-help"));if(modelErrors.has(id))group.append(button("Повторити",()=>{modelErrors.delete(id);renderModels();void loadModels(id).catch(()=>{});}));return group;}
        if(!models.length){group.append(node("p","Довідник моделей поки порожній. Можна шукати всі моделі цієї марки.","filter-help"));return group;}
        const search=node("input"),choices=node("div",undefined,"radar-choices");search.className="input";search.type="search";search.placeholder="Знайти модель";search.dataset.modelSearch=String(id);search.value=modelQueries.get(id)||"";search.setAttribute("aria-label","Знайти модель "+brandLabel(selected));
        function fill(){modelQueries.set(id,search.value);const query=search.value.trim().toLocaleLowerCase("uk-UA");const list=models.filter(m=>m.name.toLocaleLowerCase("uk-UA").includes(query));choices.replaceChildren(...list.map(m=>checkbox(m.name,selected.model_ids.includes(m.id),checked=>{toggleModel(draft,id,m.id,checked);all.querySelector("input").checked=selected.model_ids.length===0;summary.querySelector("small").textContent=selected.model_ids.length?"обрано: "+selected.model_ids.length:"усі моделі";})));if(!list.length)choices.append(node("p","Модель не знайдено.","filter-help"));}search.addEventListener("input",fill);fill();group.append(search,choices);return group;});$("radarModels").replaceChildren(...groups);if(focused){const input=$("radarModels").querySelector('[data-model-search="'+focused+'"]');input?.focus();if(input&&cursor!==null)input.setSelectionRange?.(cursor,cursor);}
    }
    function renderRegions(){const all=draft.region_ids.length===0;$("radarAllUkraine").checked=all;$("radarRegions").replaceChildren(...catalog.regions.map(r=>checkbox(r.name,draft.region_ids.includes(r.id),checked=>{toggleRegion(draft,r.id,checked);$("radarAllUkraine").checked=draft.region_ids.length===0;})));}
    function renderChoices(id,options,current,select){$(id).replaceChildren(...options.map(option=>{const choice=button(option.label,()=>{select(option.id);renderChoices(id,options,option.id,select);},"choice-button");choice.setAttribute("aria-pressed",String(current===option.id));return choice;}));}
    function renderYears(){for(const [id,value] of [["radarYearFrom",draft.year_from],["radarYearTo",draft.year_to]]){const options=[];for(let year=catalog.year_max;year>=catalog.year_min;year--){const option=node("option",String(year));option.value=year;options.push(option);}$(id).replaceChildren(...options);$(id).value=String(value);}updateYearBounds();}
    function updateYearBounds(){for(const option of $("radarYearTo").options)option.disabled=Number(option.value)<draft.year_from;for(const option of $("radarYearFrom").options)option.disabled=Number(option.value)>draft.year_to;}
    function renderControls(){renderBrands();renderModels();renderRegions();renderYears();renderChoices("radarPrices",PRICES,draft.price_category,id=>draft.price_category=id);renderChoices("radarProfits",PROFITS.map(id=>({id,label:id+"%"})),draft.min_profit,id=>draft.min_profit=id);}
    function renderStep(){document.querySelectorAll(".editor-step").forEach(el=>el.hidden=Number(el.dataset.step)!==step);document.querySelectorAll("#radarSteps li").forEach((el,i)=>{el.classList.toggle("current",i===step);el.classList.toggle("complete",i<step);if(i===step)el.setAttribute("aria-current","step");else el.removeAttribute("aria-current");});$("previousRadarStep").hidden=step===0;$("nextRadarStep").hidden=step===3;$("saveRadar").hidden=step!==3;if(step===3){$("radarReview").replaceChildren(node("h3",draft.name),facts(draft));$("radarSaveHint").textContent=editing?"Зміни збережуться в цьому самому радарі. Після зміни критеріїв перевірте його статус і за потреби ввімкніть знову.":"Новий радар збережеться на паузі. Для ввімкнення потрібна активна підписка.";}}
    function advance(){if(busy||!draft)return;draft.name=$("radarName").value;const errors=validate(draft,catalog,step);message("editorError",errors.join(" "));if(errors.length)return;step=Math.min(3,step+1);renderStep();document.querySelector('.editor-step[data-step="'+step+'"] h2')?.focus();}
    async function save(event){event.preventDefault();if(busy||!draft)return;if(step!==3){advance();return;}draft.name=$("radarName").value;const errors=validate(draft,catalog);if(errors.length){message("editorError",errors.join(" "));return;}setBusy(true);message("editorError","");try{await store.save(draft,editing,createRequestId);draft=null;editing=null;setBusy(false);showPage("radars");renderDashboard();announce("Радар збережено");void refresh();}catch(error){message("editorError",error.message);if(error.status===409)await store.load().then(renderDashboard).catch(()=>{});}finally{setBusy(false);}}
    async function changeStatus(radar){if(busy)return;if(radar.status!=="active"&&!store.snapshot.access?.active){message("radarError","Для ввімкнення потрібна активна підписка. Відкрийте «Налаштування», щоб перевірити доступ. Радар збережено.");return;}setBusy(true);message("radarError","");try{await store.status(radar,radar.status!=="active");renderDashboard();announce(radar.status==="active"?"Радар на паузі":"Радар увімкнено");}catch(error){message("radarError",error.message);if(error.status===409||error.status===402)await store.load().then(renderDashboard).catch(()=>{});}finally{setBusy(false);}}
    function askDelete(radar){if(busy)return;deleting=copy(radar);$("deleteRadarTitle").textContent="Видалити «"+radar.name+"»?";message("deleteRadarError","");$("deleteRadarDialog").showModal();$("cancelRadarDelete").focus();}
    async function confirmDelete(){if(busy||!deleting)return;setBusy(true);try{await store.remove(deleting,true);deleting=null;$("deleteRadarDialog").close();renderDashboard();announce("Радар видалено. Місце вільне.");}catch(error){message("deleteRadarError",error.message);}finally{setBusy(false);}}
    async function refreshSettings(){const button=$("refreshSettings");if(button.disabled)return;button.disabled=true;try{const state=await api.notificationStatus();$("notificationStatus").textContent=!state.telegram_ready?"Підключіть чат: надішліть /start у боті.":state.available?"Чат підключено. Повідомлення доступні.":"Чат підключено. Моніторинг тимчасово недоступний.";$("testNotification").disabled=!(state.telegram_ready&&(state.test_available??state.available));}catch(error){$("notificationStatus").textContent=error.message;}finally{button.disabled=false;}}
    function leaveEditor(next){draft=null;editing=null;showPage(next);if(next==="radars")void refresh();}
    function navigate(next){if(busy)return;if(page==="editor"&&draft){pendingNavigation=next;$("discardRadarDialog").showModal();$("keepRadarEditing").focus();}else leaveEditor(next);}
    $("refreshRadars").addEventListener("click",refresh);document.querySelectorAll("[data-page]").forEach(el=>el.addEventListener("click",()=>navigate(el.dataset.page)));document.querySelector(".logo").addEventListener("click",event=>{event.preventDefault();navigate("radars");});
    $("keepRadarEditing").addEventListener("click",()=>{pendingNavigation=null;$("discardRadarDialog").close();});$("discardRadarChanges").addEventListener("click",()=>{const next=pendingNavigation||"radars";pendingNavigation=null;$("discardRadarDialog").close();leaveEditor(next);});$("discardRadarDialog").addEventListener("cancel",()=>{pendingNavigation=null;});
    $("cancelRadar").addEventListener("click",()=>navigate("radars"));$("brandSearch").addEventListener("input",()=>{if(draft)renderBrands();});$("radarName").addEventListener("input",()=>{if(draft)draft.name=$("radarName").value;});$("radarAllUkraine").addEventListener("change",()=>{draft.region_ids=[];renderRegions();});
    $("radarAllBrands").addEventListener("change",()=>{if(!draft)return;selectAllBrands(draft,$("radarAllBrands").checked);renderBrands();renderModels();});
    $("radarYearFrom").addEventListener("change",()=>{draft.year_from=Number($("radarYearFrom").value);updateYearBounds();});$("radarYearTo").addEventListener("change",()=>{draft.year_to=Number($("radarYearTo").value);updateYearBounds();});$("nextRadarStep").addEventListener("click",advance);$("previousRadarStep").addEventListener("click",()=>{if(busy)return;step=Math.max(0,step-1);message("editorError","");renderStep();});$("radarForm").addEventListener("submit",save);
    $("retryRadarCatalog").addEventListener("click",async()=>{try{await loadCatalog();if(draft)renderControls();}catch(error){message("catalogError",error.message);}});$("cancelRadarDelete").addEventListener("click",()=>{if(busy)return;deleting=null;$("deleteRadarDialog").close();});$("confirmRadarDelete").addEventListener("click",confirmDelete);$("deleteRadarDialog").addEventListener("cancel",event=>{if(busy)event.preventDefault();else deleting=null;});
    $("refreshSettings").addEventListener("click",refreshSettings);$("testNotification").addEventListener("click",async()=>{const button=$("testNotification");button.disabled=true;try{await api.testNotification();message("settingsStatus","Тестове повідомлення надіслано. Перевірте чат бота.");}catch(error){message("settingsStatus",error.message);}finally{void refreshSettings();}});
    $("listingTraceForm").addEventListener("submit",async event=>{event.preventDefault();const id=$("listingTraceId").value.trim();if(!/^[1-9][0-9]{0,11}$/.test(id)){message("listingTraceResult","Введіть числовий ID оголошення AUTO.RIA.");return;}const button=$("listingTraceCheck");button.disabled=true;try{const result=await api.traceListing(id);message("listingTraceResult",describeTrace(result));}catch(error){message("listingTraceResult",error.message);}finally{button.disabled=false;}});
    root.Telegram?.WebApp?.ready();root.Telegram?.WebApp?.expand();root.addEventListener?.("pageshow",()=>{if(loaded&&page!=="editor")void refresh();});document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible"&&page!=="editor")void refresh();});void refresh();
    const timer=root.setInterval?.(()=>{if(loaded&&page==="radars"&&!busy&&document.visibilityState==="visible")void refresh();},60000);
    return {store,refresh,destroy(){if(timer)root.clearInterval(timer);clearTimeout(toastTimer);}};
  }
  const exported={PRICES,PROFITS,STATUS,draftFrom,validate,canonical,selectAllBrands,toggleBrand,toggleModel,toggleRegion,slots,priceMatches,describeTrace,createStore,mount};
  if(typeof module!=="undefined"&&module.exports)module.exports=exported;
  else {root.AutoDealRadars=exported;mount(root.document,root.AutoDealCloud);}
})(typeof window!=="undefined"?window:globalThis);
