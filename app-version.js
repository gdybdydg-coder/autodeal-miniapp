(function(root) {
  "use strict";
  const current=document.querySelector('meta[name="autodeal-version"]').content;
  const button=document.getElementById("reloadApp");
  let latest=current,checking=false;
  async function check() {
    if(checking) return;
    checking=true;
    try {
      const url=new URL("release.json",root.location.href);
      url.searchParams.set("check",String(Date.now()));
      const response=await root.fetch(url.href,{cache:"no-store",credentials:"omit",referrerPolicy:"no-referrer",signal:AbortSignal.timeout(8000)});
      if(!response.ok) return;
      const value=(await response.json()).version;
      if(typeof value==="string"&&/^[a-z0-9-]{1,40}$/.test(value)) {
        latest=value;
        if(latest!==current) button.textContent="Завантажити нову версію";
      }
    } catch(_) {} finally {checking=false;}
  }
  button.addEventListener("click",async()=>{
    button.disabled=true;
    await check();
    const url=new URL(root.location.href);
    // Preserve the Telegram fragment; never put initData in query parameters.
    url.searchParams.set("v",latest);
    url.searchParams.set("reload",String(Date.now()));
    root.location.replace(url.href);
  });
  root.addEventListener("pageshow",check);
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")check();});
  check();
})(window);
