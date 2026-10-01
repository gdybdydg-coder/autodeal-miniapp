'use strict';
(function(){
  const byId=id=>document.getElementById(id);
  let root=byId('autodeal-payment-trial');
  const template=root.cloneNode(true),login=byId('owner-login');
  const role=window.location.pathname==='/admin'?'admin':'client';
  let app=null,busy=false;
  function resetRoot(){const replacement=template.cloneNode(true);root.replaceWith(replacement);root=replacement;}
  function lost(){resetRoot();root.hidden=true;login.hidden=false;byId('owner-session').hidden=true;app=null;}
  const transport=window.AutoDealOwnerTransport.create(window.fetch.bind(window),role,lost);
  function deadline(value){byId('owner-deadline').textContent=
    (role==='admin'?'Адміністратор':'Клієнт')+' · сесія до '+new Intl.DateTimeFormat('uk-UA',{
      timeZone:'Europe/Kyiv',hour:'2-digit',minute:'2-digit'}).format(value*1000);}
  function error(value){
    byId('owner-error').textContent=login.hidden?'':value;
    byId('owner-session-error').textContent=login.hidden?value:'';
  }
  async function locked(fn){
    if(busy)return;busy=true;error('');
    for(const id of ['owner-enter','owner-rotate','owner-logout'])byId(id).disabled=true;
    try{await fn();}catch(e){error(e.message);}
    finally{busy=false;for(const id of ['owner-enter','owner-rotate','owner-logout'])byId(id).disabled=false;}
  }
  function downloadFile(result){
    const url=URL.createObjectURL(result.blob),link=document.createElement('a');
    link.href=url;link.download=result.filename;link.rel='noopener';
    document.body.appendChild(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  byId('owner-enter').addEventListener('click',()=>locked(async()=>{
    const session=await transport.login(byId('owner-code').value.trim());
    byId('owner-code').value='';deadline(session.expires_at);
    resetRoot();root.hidden=false;login.hidden=true;byId('owner-session').hidden=false;
    const api={...transport,download:async id=>downloadFile(await transport.download(id))};
    // Replace DOM before remount: old handlers/sensitive view do not survive login.
    app=window.AutoDealPaymentUI.mount(root,api,{role});
    await app.ready;
  }));
  byId('owner-rotate').addEventListener('click',()=>locked(async()=>deadline((await transport.rotate()).expires_at)));
  byId('owner-logout').addEventListener('click',()=>locked(()=>transport.logout()));
})();
