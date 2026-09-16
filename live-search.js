(function(root) {
  "use strict";
  let busy=false;
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
  function renderResults(data,filters) {
    const list=document.getElementById("resultsList");list.replaceChildren();
    document.getElementById("count").textContent="Показано: "+data.cars.length;
    document.getElementById("sourceNote").textContent=
      "Перевірено оголошень: "+data.inspected+". На AUTO.RIA за фільтрами: "+data.source_total+
      ". Тестовий режим: до 3 оголошень за пошук, дані кешуються до 15 хв. "+
      (data.warnings.length?"Частину перевірки не завершено через ліміт або недоступність даних. ":"")+
      "Оцінка — медіана цін щонайменше 5 схожих авто; це ціни пропозицій, не продажів.";
    if(data.warnings.includes("quota_exceeded") && Number.isFinite(data.quota?.retry_after_seconds) && data.quota.retry_after_seconds>0)
      document.getElementById("sourceNote").textContent+=" Нові запити можна повторити приблизно через "+Math.ceil(data.quota.retry_after_seconds/60)+" хв.";
    for(const car of data.cars) {
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
      const valued=Number.isFinite(car.market)&&car.market>0;
      body.append(node("p","market-price",valued?
        "Медіана вибірки ≈ $"+car.market.toLocaleString("en-US")+" · "+car.comparables+" схожих авто":
        "Недостатньо схожих авто для оцінки"));
      if(valued) body.append(node("p","car-meta",car.discount>=0?
        "На "+car.discount+"% нижче медіани вибірки":"На "+Math.abs(car.discount)+"% вище медіани вибірки"));
      const href=safeLink(car.url);
      if(href) {const link=node("a","car-link","ВІДКРИТИ НА AUTO.RIA");link.href=href;link.target="_blank";link.rel="noopener";body.append(link);}
      article.append(body);list.append(article);
    }
    if(!data.cars.length) list.append(node("div","empty",filters.onlyDeals?
      "У перевіреній частині оголошень не підтверджено пропозицій на 15% нижче медіани. Вимкни цей фільтр, щоб бачити авто без оцінки. Це не означає, що вигідних авто на AUTO.RIA немає.":
      "У перевіреній частині оголошень немає авто, які відповідають усім фільтрам."));
  }
  async function search(filters) {
    if(busy) return;
    busy=true;
    const button=document.getElementById("searchBtn"),label=button.textContent;
    const section=document.getElementById("results"),list=document.getElementById("resultsList");
    button.disabled=true;button.textContent="Перевіряю AUTO.RIA…";
    section.classList.add("show");
    document.getElementById("count").textContent="";
    document.getElementById("sourceNote").textContent="Завантажую оголошення та перевіряю ціни. Це може тривати близько хвилини.";
    list.replaceChildren();
    try {renderResults(await root.AutoDealCloud.search(filters),filters);}
    catch(error) {
      document.getElementById("sourceNote").textContent="Пошук не завершено.";
      list.replaceChildren(node("div","empty",error.message));
    } finally {
      busy=false;button.disabled=false;button.textContent=label;
      section.scrollIntoView({behavior:"smooth",block:"start"});
    }
  }
  root.AutoDealLive={search};
})(window);
