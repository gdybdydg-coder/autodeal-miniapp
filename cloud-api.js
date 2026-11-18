(function(root) {
  "use strict";
  const BASE="https://autodeal-api.onrender.com";
  const radarId=id=>typeof id==="string"&&/^[a-f0-9]{32}$/.test(id);
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
          if(path.startsWith("/api/radars")) {
            let detail="",code="";
            try {const data=await response.json();detail=data.detail;code=data.code||"";}catch(_){}
            const messages={401:"Сесія Telegram закінчилася. Закрийте застосунок і відкрийте його знову через бота.",
              402:"Для ввімкнення радара потрібна активна підписка. Налаштування збережені.",
              403:"Немає доступу до цього радара.",404:"Радар уже видалено. Оновіть список.",
              409:"Дані змінилися або всі три місця зайняті. Оновіть список перед повторною дією.",
              422:"Перевірте налаштування радара: марки, моделі, категорію ціни, області, роки й відсоток.",
              429:"Забагато дій поспіль. Зачекайте трохи й повторіть.",503:"Сервіс тимчасово недоступний. Спробуйте пізніше."};
            // Never expose raw validation payloads or English framework messages.
            const message=typeof detail==="string"&&/[А-Яа-яІіЇїЄєҐґ]/.test(detail)&&detail.length<500
              ?detail:messages[response.status]||"Не вдалося виконати дію з радаром. Спробуйте ще раз.";
            const error=Error(message);error.status=response.status;error.code=code;throw error;
          }
          if(response.status===402) throw Error("Підключи доступ у налаштуваннях або через /subscription у боті. Фільтри збережені.");
          if((path.startsWith("/api/cars/")||path.startsWith("/api/catalog")) && response.status!==401) {
            const reasons={unsupported_filter:"AUTO.RIA не підтвердила один із фільтрів. Зміни вибір: фільтр не буде проігноровано.",
              full_scan_disabled:"AUTODeal переходить на сповіщення про нові вигідні авто. Онови застосунок і збережи фільтри як підписку.",
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
          if((path.startsWith("/api/subscriptions") || path.startsWith("/api/notifications/")) && response.status!==401) {
            let code="";try {code=(await response.json()).detail;}catch(_){}
            const reasons={"Send /start to the bot first":"Відкрий чат бота, натисни «Розпочати» або надішли /start, потім онови список.",
              "Send a test notification first":"Спочатку натисніть «Надіслати тест» у налаштуваннях.",
              "Wait ten minutes before another test":"Наступне тестове повідомлення можна надіслати через 10 хвилин.",
              "Subscription filters already exist":"Підписка з такими фільтрами вже є. Відкрий її у списку або зміни фільтри.",
              "Name is required":"Введи назву підписки.",
              "Delivery/source not connected":"Моніторинг ще не підключено. Налаштування радарів збережено.",
              "Monitor is offline":"Моніторинг тимчасово недоступний. Налаштування радарів збережено."};
            if(reasons[code]) throw Error(reasons[code]);
            if(path.startsWith("/api/notifications/")) {
              const notices={403:"Немає доступу до цього повідомлення.",404:"Запис про повідомлення не знайдено.",
                409:"Стан повідомлень змінився. Перевірте підключення перед повторною дією.",
                422:"Перевірте введені дані повідомлення.",429:"Забагато перевірок поспіль. Зачекайте й повторіть.",
                503:"Сервер повідомлень тимчасово недоступний. Спробуйте пізніше."};
              throw Error(notices[response.status]||"Не вдалося перевірити повідомлення. Спробуйте ще раз.");
            }
          }
          const messages={401:"Сесія Telegram закінчилася або не підтверджена. Закрий Mini App і відкрий знову через бота.",
            409:"Ліміт: до 20 серверних пошуків. Видали непотрібний пошук.",422:"Перевір назву та фільтри пошуку.",
            404:"Пошук уже видалено. Онови список.",503:"Сервер тимчасово недоступний. Спробуй пізніше."};
          throw Error(messages[response.status]||"Сервер не виконав запит. Спробуй пізніше.");
        }
        return response.status===204?null:await response.json();
      } catch(error) {
        if(error.name==="SyntaxError")
          throw Error("Сервер повернув некоректну відповідь. Оновіть сторінку перед повторною дією.");
        if(error.name==="AbortError"||error instanceof TypeError)
          throw Error(path.startsWith("/api/cars/search")?
            "Немає відповіді сервера. Зачекай близько хвилини та повтори пошук.":
            "Немає відповіді сервера. Онови список перед повторною дією.");
        throw error;
      } finally {clearTimeout(timer);}
    }
    return {
      radars:()=>request("/api/radars"),
      radarCatalog:(brandId)=>{
        if(brandId!=null&&(!Number.isSafeInteger(brandId)||brandId<=0)) return Promise.reject(Error("Некоректна марка авто."));
        return request("/api/radars/catalog"+(brandId==null?"":"?brand_id="+brandId));
      },
      createRadar:payload=>request("/api/radars","POST",payload),
      updateRadar:(id,payload)=>radarId(id)?request("/api/radars/"+id,"PUT",payload):Promise.reject(Error("Некоректний радар.")),
      radarStatus:(id,active,revision)=>{
        if(!radarId(id)||typeof active!=="boolean"||!Number.isSafeInteger(revision)||revision<1) return Promise.reject(Error("Оновіть список радарів."));
        return request("/api/radars/"+id+"/status","PATCH",{active,revision});
      },
      deleteRadar:(id,revision)=>{
        if(!radarId(id)||!Number.isSafeInteger(revision)||revision<1) return Promise.reject(Error("Оновіть список радарів."));
        return request("/api/radars/"+id+"?revision="+revision,"DELETE");
      },
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
      billingStatus:()=>request("/api/billing/status"),
      list:()=>request("/api/subscriptions"),
      notificationStatus:()=>request("/api/notifications/status"),
      traceListing:id=>/^[1-9][0-9]{0,11}$/.test(id)?request("/api/notifications/trace/"+id):Promise.reject(Error("Введи числовий ID оголошення AUTO.RIA.")),
      testNotification:()=>request("/api/notifications/test","POST"),
      enable:(id,enabled)=>{
        if(!Number.isSafeInteger(id)||id<=0||typeof enabled!=="boolean") return Promise.reject(Error("Некоректний пошук"));
        return request("/api/subscriptions/"+id,"PATCH",{enabled});
      },
      save:(name,filters)=>request("/api/subscriptions","POST",{name,filters,enabled:false}),
      update:(id,name,filters)=>{
        if(!Number.isSafeInteger(id)||id<=0) return Promise.reject(Error("Некоректна підписка"));
        return request("/api/subscriptions/"+id,"PUT",{name,filters});
      },
      remove:id=>{
        if(!Number.isSafeInteger(id)||id<=0) return Promise.reject(Error("Некоректний пошук"));
        return request("/api/subscriptions/"+id,"DELETE");
      }
    };
  }
  if(typeof module!=="undefined"&&module.exports) module.exports={create};
  else root.AutoDealCloud=create(root.fetch.bind(root),()=>root.Telegram?.WebApp?.initData||"");
})(typeof window!=="undefined"?window:globalThis);
