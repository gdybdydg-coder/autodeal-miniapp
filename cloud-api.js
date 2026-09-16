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
          const messages={401:"Сесія Telegram закінчилася або не підтверджена. Закрий Mini App і відкрий знову через бота.",
            409:"Ліміт: до 20 серверних пошуків. Видали непотрібний пошук.",422:"Перевір назву та фільтри пошуку.",
            404:"Пошук уже видалено. Онови список.",503:"Сервер тимчасово недоступний. Спробуй пізніше."};
          throw Error(messages[response.status]||"Сервер не виконав запит. Спробуй пізніше.");
        }
        return response.status===204?null:await response.json();
      } catch(error) {
        if(error.name==="AbortError"||error instanceof TypeError)
          throw Error("Немає відповіді сервера. Free-сервер може прокидатися близько хвилини. Онови список перед повторним збереженням.");
        throw error;
      } finally {clearTimeout(timer);}
    }
    return {
      list:()=>request("/api/subscriptions"),
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
