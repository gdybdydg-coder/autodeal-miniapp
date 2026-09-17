(function(root) {
  "use strict";
  // A curated shortcut list; names and values still come from the car catalog.
  const popular=["Volkswagen","Audi","Mercedes-Benz","BMW","Skoda","Renault","Toyota","Ford",
    "Opel","Nissan","Hyundai","Kia","Peugeot","Volvo","Honda","Mazda"];
  const normalize=value=>value.normalize("NFD").replace(/[\u0300-\u036f]/g,"")
    .toLocaleLowerCase("uk").replace(/[^\p{L}\p{N}]/gu,"");
  const ranks=new Map(popular.map((name,index)=>[normalize(name),index]));
  const aliases={volkswagen:["vw","фольксваген","фольцваген"],audi:["ауді"],mercedesbenz:["мерседес"],
    bmw:["бмв"],skoda:["шкода"],renault:["рено"],toyota:["тойота"],ford:["форд"],opel:["опель"],
    nissan:["ніссан"],hyundai:["хюндай","хундай"],kia:["кіа"],peugeot:["пежо"],volvo:["вольво"],
    honda:["хонда"],mazda:["мазда"]};
  function order(values) {
    return [...new Set(values)].sort((a,b)=>(ranks.get(normalize(a))??Infinity)-(ranks.get(normalize(b))??Infinity)
      ||a.localeCompare(b,"uk",{sensitivity:"base",numeric:true}));
  }
  function matches(name,query) {
    const key=normalize(name),text=normalize(query);
    return key.includes(text)||(aliases[key]||[]).some(alias=>normalize(alias).includes(text));
  }
  const doc=root.document;
  if(!doc) return;
  const select=doc.getElementById("brand"),trigger=doc.getElementById("brandPickerButton"),
    label=doc.getElementById("brandPickerValue"),dialog=doc.getElementById("brandDialog"),
    search=doc.getElementById("brandSearch"),list=doc.getElementById("brandOptions"),
    status=doc.getElementById("brandSearchStatus");
  if(!dialog?.showModal) return;
  let brands=[],visible=[];
  function sync() {label.textContent=select.value||"Всі марки";}
  function choose(value) {
    // Typing or closing never changes the applied filters. Confirming a different
    // make uses the existing change event to reset and load its models.
    if(select.value!==value) {
      select.value=value;
      select.dispatchEvent(new Event("change",{bubbles:true}));
    }
    sync();dialog.close();
  }
  function option(value) {
    const button=doc.createElement("button");
    button.type="button";button.className="brand-option";
    button.textContent=value||"Всі марки";
    button.setAttribute("aria-pressed",String(value===select.value));
    button.addEventListener("click",()=>choose(value));
    return button;
  }
  function heading(text) {
    const title=doc.createElement("h3");title.className="brand-group-title";title.textContent=text;return title;
  }
  function render() {
    const query=search.value.trim();
    visible=brands.filter(name=>matches(name,query));
    const nodes=[];
    if(query) {
      status.textContent=visible.length?"Знайдено марок: "+visible.length:"Марку не знайдено. Спробуй іншу назву.";
      nodes.push(...visible.map(option));
    } else {
      status.textContent="Обери марку або знайди її за назвою";
      nodes.push(option(""));
      const top=brands.filter(name=>ranks.has(normalize(name))),rest=brands.filter(name=>!ranks.has(normalize(name)));
      if(top.length) nodes.push(heading("Популярні"),...top.map(option));
      if(rest.length) nodes.push(heading("За алфавітом"),...rest.map(option));
    }
    list.replaceChildren(...nodes);list.scrollTop=0;
  }
  function refresh() {
    const selected=select.value;
    brands=order(Array.from(select.options,entry=>entry.value).filter(Boolean));
    select.replaceChildren(new Option("Всі марки",""),...brands.map(name=>new Option(name,name)));
    select.value=selected;sync();
    if(dialog.open) render();
  }
  function fitViewport() {
    const viewport=root.visualViewport;
    if(!viewport||!dialog.open) return;
    dialog.style.setProperty("--brand-dialog-top",(viewport.offsetTop+viewport.height/2)+"px");
    dialog.style.setProperty("--brand-dialog-height",Math.max(120,viewport.height-24)+"px");
  }
  trigger.addEventListener("click",()=>{
    search.value="";render();dialog.showModal();fitViewport();search.focus({preventScroll:true});
  });
  doc.getElementById("closeBrandDialog").addEventListener("click",()=>dialog.close());
  dialog.addEventListener("close",()=>trigger.focus({preventScroll:true}));
  dialog.addEventListener("click",event=>{
    if(event.target!==dialog) return;
    const rect=dialog.getBoundingClientRect();
    if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom) dialog.close();
  });
  search.addEventListener("input",render);
  search.addEventListener("keydown",event=>{
    if(event.isComposing) return;
    if(event.key==="Enter"&&normalize(search.value)) {
      event.preventDefault();
      const exact=visible.find(name=>normalize(name)===normalize(search.value));
      if(exact||visible.length===1) choose(exact||visible[0]);
    } else if(event.key==="ArrowDown") {
      event.preventDefault();list.querySelector("button")?.focus();
    }
  });
  select.addEventListener("change",sync);
  root.visualViewport?.addEventListener("resize",fitViewport);
  root.visualViewport?.addEventListener("scroll",fitViewport);
  root.AutoDealBrandPicker={refresh};
  refresh();select.hidden=true;trigger.hidden=false;doc.getElementById("brandLabel").htmlFor="brandPickerButton";
})(window);
