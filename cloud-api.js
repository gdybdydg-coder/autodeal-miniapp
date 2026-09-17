(function(root) {
  "use strict";
  const BASE="https://autodeal-api.onrender.com";
  function create(fetcher, getInitData, timeoutMs=60000) {
    async function request(path, method="GET", body) {
      const initData=getInitData();
      if(!initData) throw Error("Відкрий Mini App через кнопку бота в Telegram, не звичайне посилання у браузері.");
      const controller=new AbortController();
      const timer=setTimeout(()=>controller.abort(),timeoutMs);
      try {
        const response=await fetcher(BASE+path,{
          method,credentials:"omit",cache:"no-store",redirect:"error",signal:controller.signal,
          headers:{"X-Telegram-Init-Data":initData,...(body?{"Content-Type":"application/json"}:{})},
          ...(body?{body:JSON.stringify(body)}:{})
        });
        if(!response.ok) {
          if((path.startsWith("/api/cars/")||path.startsWith("/api/catalog")) && response.status!==401) {
            const reasons={unsupported_filter:"AUTO.RIA не підтвердила один із фільтрів. Зміни вибір: фільтр не буде проігноровано.",
              busy:"Інший пошук ще виконується. Спробуй за хвилину.",quota_exceeded:"Досягнуто ліміт запитів AUTO.RIA. Спробуй пізніше.",
              search_limit:"Перевірка зайняла забагато часу. Спробуй пізніше.",not_configured:"Джерело AUTO.RIA ще не налаштоване.",
              search_expired:"Результати застаріли. Натисни «Показати авто», щоб почати свіжий пошук."};
            let code="",quota=null;try {const error=await response.json();code=error.detail;quota=error.quota;}catch(_){}
            if(code==="quota_exceeded" && quota?.reason==="total")
              throw Error("Досягнуто загальну межу запитів. Потрібно перевірити залишок пакета AUTO.RIA.");
            if(code==="quota_exceeded" && Number.isFinite(quota?.retry_after_seconds) && quota.retry_after_seconds>0)
              throw Error("Ліміт запитів AUTO.RIA. Спробуй знову приблизно через "+Math.ceil(quota.retry_after_seconds/60)+" хв.");
            if(path.startsWith("/api/cars/scans")&&response.status===404)
              throw Error("Оновлення повної перевірки ще недоступне або цей пошук уже видалено. Онови застосунок і повтори пошук.");
            throw Error(reasons[code]||"AUTO.RIA зараз не відповідає. Спробуй пізніше.");
          }
          if((method==="PATCH" || path.startsWith("/api/notifications/")) && response.status!==401) {
            let code="";try {code=(await response.json()).detail;}catch(_){}
            const reasons={"Send /start to the bot first":"Відкрий чат бота, натисни «Розпочати» або надішли /start, потім онови список.",
              "Send a test notification first":"Спочатку натисни «Надіслати тестове повідомлення».",
              "Pilot allows one active search":"На першому запуску доступний один активний пошук. Вимкни попередній, щоб увімкнути цей.",
              "Wait ten minutes before another test":"Наступне тестове повідомлення можна надіслати через 10 хвилин."};
            if(reasons[code]) throw Error(reasons[code]);
          }
          const messages={401:"Сесія Telegram закінчилася або не підтверджена. Закрий Mini App і відкрий знову через бота.",
            409:"Ліміт: до 20 серверних пошуків. Видали непотрібний пошук.",422:"Перевір назву та фільтри пошуку.",
            404:"Пошук уже видалено. Онови список.",503:"Сервер тимчасово недоступний. Спробуй пізніше."};
          throw Error(messages[response.status]||"Сервер не виконав запит. Спробуй пізніше.");
        }
        return response.status===204?null:await response.json();
      } catch(error) {
        if(error.name==="AbortError"||error instanceof TypeError)
          throw Error(path.startsWith("/api/cars/search")?
            "Немає відповіді сервера. Зачекай близько хвилини та повтори пошук.":
            "Немає відповіді сервера. Онови список перед повторною дією.");
        throw error;
      } finally {clearTimeout(timer);}
    }
    return {
      startScan:(filters,restart=false)=>request("/api/cars/scans"+(restart?"?restart=true":""),"POST",filters),
      scan:(id,after=0,onlyDeals=false,cacheAfter=0)=>{
        if(!/^[a-f0-9]{32}$/.test(id)||!Number.isSafeInteger(after)||after<0||!Number.isSafeInteger(cacheAfter)||cacheAfter<0)
          return Promise.reject(Error("Онови результати пошуку."));
        return request("/api/cars/scans/"+id+"?after="+after+"&only_deals="+!!onlyDeals+"&cache_after="+cacheAfter);
      },
      controlScan:(id,enabled)=>{
        if(!/^[a-f0-9]{32}$/.test(id)||typeof enabled!=="boolean")
          return Promise.reject(Error("Некоректний пошук"));
        return request("/api/cars/scans/"+id,"PATCH",{enabled});
      },
      search:(filters,cursor)=>{
        if(cursor!=null&&!/^[a-f0-9]{32}$/.test(cursor)) return Promise.reject(Error("Онови результати пошуку."));
        return request("/api/cars/search"+(cursor?"?cursor="+cursor:""),"POST",filters);
      },
      catalog:(brand="")=>request("/api/catalog"+(brand?"?brand="+encodeURIComponent(brand):"")),
      list:()=>request("/api/subscriptions"),
      notificationStatus:()=>request("/api/notifications/status"),
      testNotification:()=>request("/api/notifications/test","POST"),
      enable:(id,enabled)=>{
        if(!Number.isSafeInteger(id)||id<=0||typeof enabled!=="boolean") return Promise.reject(Error("Некоректний пошук"));
        return request("/api/subscriptions/"+id,"PATCH",{enabled});
      },
      save:(name,filters)=>request("/api/subscriptions","POST",{name,filters,enabled:false}),
      remove:id=>{
        if(!Number.isSafeInteger(id)||id<=0) return Promise.reject(Error("Некоректний пошук"));
        return request("/api/subscriptions/"+id,"DELETE");
      }
    };
  }
  if(typeof module!=="undefined"&&module.exports) module.exports={create};
  else root.AutoDealCloud=create(root.fetch.bind(root),()=>root.Telegram?.WebApp?.initData||"");
})(typeof window!=="undefined"?window:globalThis);
