(function(root) {
  "use strict";
  const data=root.AUTO_DEAL_DATA,loaded=new Set(),requests=new Map();
  Object.setPrototypeOf(data.catalog,null);
  let initial=null,ready=false,pendingBrand=null,modelQueue=Promise.resolve();
  const status=document.getElementById("catalogStatus"),retry=document.getElementById("retryCatalog");
  function names(items) {
    if(!Array.isArray(items)||!items.every(item=>typeof item?.name==="string"&&item.name.trim()&&item.name.length<=150))
      throw Error("Не вдалося прочитати список AUTO.RIA.");
    return [...new Set(items.map(item=>item.name))].sort((a,b)=>a.localeCompare(b,"uk"));
  }
  function fill(select,values,title,current=select.value) {
    const options=[new Option(title,"")];
    if(current&&!values.includes(current)) values=[current,...values];
    options.push(...values.map(value=>new Option(value,value)));
    select.replaceChildren(...options);select.value=current;
  }
  function message(text,error=false) {status.textContent=text;status.hidden=!text;retry.hidden=!error;}
  async function modelsFor(value) {
    if(!value||loaded.has(value)) return;
    if(!requests.has(value)) {
      modelQueue=modelQueue.catch(()=>{}).then(async()=>{
        if(initial) await initial;
        const result=await root.AutoDealCloud.catalog(value);
        const models=names(result.models);
        data.catalog[value]=models;loaded.add(value);
      });
      requests.set(value,modelQueue.finally(()=>requests.delete(value)));
    }
    return requests.get(value);
  }
  async function changeModel() {
    const value=brand.value;
    if(!value) {pendingBrand=null;model.disabled=true;message("");return;}
    if(loaded.has(value)) {pendingBrand=null;return;}
    pendingBrand=value;model.disabled=true;message("Завантажуємо моделі "+value+"…");
    try {
      await modelsFor(value);
      if(brand.value!==value) return;
      fill(model,data.catalog[value],"Всі моделі");
      model.disabled=!data.catalog[value].length;
      message(data.catalog[value].length?"":"Для цієї марки моделей у довіднику немає.");
    } catch(error) {if(brand.value===value) message(error.message,true);}
    finally {if(pendingBrand===value) pendingBrand=null;}
  }
  function init() {
    if(initial) return initial;
    initial=(async()=>{
      const result=await root.AutoDealCloud.catalog();
      const brands=names(result.brands),states=names(result.regions).map(name=>/ська$|цька$/.test(name)?name+" область":name);
      if(!brands.length||!states.length) throw Error("Список AUTO.RIA поки недоступний. Спробуй оновити його.");
      // Validate the entire response before changing any selected controls.
      const choices=Object.fromEntries(["body","fuel","transmission"].map(key=>[key,names(result[key])]));
      for(const value of brands) if(!Object.hasOwn(data.catalog,value)) data.catalog[value]=[];
      fill(brand,brands,"Всі марки");
      root.AutoDealBrandPicker?.refresh();
      data.regions.splice(0,data.regions.length,"Вся Україна",...states);
      fill(region,states,"Вся Україна");
      for(const group of advancedGroups) {
        const selected=selectedValues(group.name);
        // Preserve a saved/selected option; the server will reject an obsolete choice.
        group.options.splice(0,group.options.length,...new Set([...choices[group.name],...selected]));
      }
      renderAdvancedOptions();ready=true;message("");
    })().catch(error=>{message(error.message,true);throw error;}).finally(()=>{initial=null;});
    return initial;
  }
  brand.addEventListener("change",changeModel);
  retry.addEventListener("click",async()=>{
    retry.disabled=true;
    try {await init();await changeModel();}catch(_){}finally{retry.disabled=false;}
  });
  root.AutoDealCatalog={
    isBusy:()=>pendingBrand===brand.value&&!!pendingBrand,
    async ensureFilters(filters) {
      if(!ready) await init();
      await modelsFor(filters.brand);
    }
  };
  init().then(()=>{if(brand.value)return changeModel();}).catch(()=>{});
})(window);
