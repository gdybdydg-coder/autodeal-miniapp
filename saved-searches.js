/* Device-local drafts only. No Telegram identity or active subscriptions. */
(function(root) {
  "use strict";
  const KEY="autodeal.savedSearches.v1", LIMIT=20;
  const fail=()=>{throw Error("Некоректні дані збереженого пошуку");};
  function normalize(filters) {
    if (!filters || typeof filters!=="object") return fail();
    const result={};
    for(const key of ["brand","model"]) {
      if(typeof filters[key]!=="string" || filters[key].length>150) return fail();
      result[key]=filters[key];
    }
    const regions=Array.isArray(filters.region)?filters.region:(filters.region===""?[]:[filters.region]);
    if(regions.length>30||!regions.every(v=>typeof v==="string"&&v.length>0&&v.length<=150)) return fail();
    const uniqueRegions=[...new Set(regions)].sort();
    result.region=uniqueRegions.length>1?uniqueRegions:(uniqueRegions[0]||"");
    for(const key of ["price","year","mileage"]) {
      const range=filters[key];
      if(!range || !["from","to"].every(k=>range[k]===null || (Number.isFinite(range[k])&&range[k]>=0))) return fail();
      if(range.from!==null&&range.to!==null&&range.from>range.to) return fail();
      result[key]={from:range.from,to:range.to};
    }
    for(const key of ["body","fuel","transmission"]) {
      if(!Array.isArray(filters[key]) || filters[key].length>50 || !filters[key].every(v=>typeof v==="string"&&v.length<150)) return fail();
      result[key]=[...new Set(filters[key])].sort();
    }
    if(typeof filters.onlyDeals!=="boolean") return fail();
    result.onlyDeals=filters.onlyDeals;
    const percent=filters.minDiscount===undefined?15:filters.minDiscount;
    if(!Number.isFinite(percent)||percent<0||percent>100) return fail();
    if(percent!==15) result.minDiscount=percent;
    if(!result.fuel.includes("Електро")) result.transmission=result.transmission.filter(v=>v!=="Редуктор");
    return result;
  }
  function read(storage) {
    const text=storage.getItem(KEY);
    if(text===null) return [];
    if(text.length>100000) return fail();
    const data=JSON.parse(text);
    if(data.version!==1 || !Array.isArray(data.searches) || data.searches.length>LIMIT) return fail();
    const ids=new Set();
    return data.searches.map(item=>{
      if(!item || typeof item.id!=="string" || !item.id || ids.has(item.id) || typeof item.name!=="string" || !item.name.trim() || item.name.length>60 || typeof item.notificationsWanted!=="boolean") return fail();
      ids.add(item.id);
      return {id:item.id,name:item.name,notificationsWanted:item.notificationsWanted,filters:normalize(item.filters)};
    });
  }
  function write(storage,items) { storage.setItem(KEY,JSON.stringify({version:1,searches:items})); }
  function save(storage,filters,name,notificationsWanted) {
    const normalized=normalize(filters),items=read(storage);
    name=name.trim();
    if(!name || name.length>60) throw Error("Назва має містити від 1 до 60 символів");
    const signature=JSON.stringify(normalized);
    let item=items.find(item=>JSON.stringify(item.filters)===signature);
    if(!item) {
      if(items.length>=LIMIT) throw Error("Можна зберегти до 20 пошуків");
      item={id:Date.now().toString(36)+"-"+Math.random().toString(36).slice(2)};
      items.push(item);
    }
    Object.assign(item,{name,filters:normalized,notificationsWanted:!!notificationsWanted});
    write(storage,items);
    return item;
  }
  function remove(storage,id) { write(storage,read(storage).filter(item=>item.id!==id)); }
  function update(storage,id,filters,name) {
    const normalized=normalize(filters),items=read(storage),item=items.find(item=>item.id===id);
    if(!item) throw Error("Пошук уже видалено. Онови список.");
    name=name.trim();
    if(!name||name.length>60) throw Error("Назва має містити від 1 до 60 символів");
    if(items.some(other=>other.id!==id&&JSON.stringify(other.filters)===JSON.stringify(normalized)))
      throw Error("Пошук із такими фільтрами вже є. Відкрий його у списку.");
    Object.assign(item,{name,filters:normalized,notificationsWanted:false});
    write(storage,items);return item;
  }
  function setWanted(storage,id,value) {
    const items=read(storage),item=items.find(item=>item.id===id);
    if(!item) throw Error("Пошук не знайдений");
    item.notificationsWanted=!!value;write(storage,items);
  }
  const api={KEY,normalize,read,save,update,remove,setWanted};
  if(typeof module!=="undefined"&&module.exports) module.exports=api;
  else root.AutoDealSaved=api;
})(typeof window!=="undefined"?window:globalThis);
