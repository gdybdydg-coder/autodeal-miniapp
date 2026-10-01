(function(root,factory){
  const api=factory();
  if(typeof module==='object' && module.exports) module.exports=api;
  else root.AutoDealOwnerTransport=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function create(fetchLocal,role,onLost=()=>{}) {
    if(!['client','admin'].includes(role)) throw Error('Невідома тестова роль');
    let session=null;
    async function request(path,body,extra={}) {
      const headers={'X-AutoDeal-Local':'1',...extra.headers};
      if(session) headers['X-Local-Session']=session.token;
      const options={headers};
      if(body!==undefined) {
        options.method='POST';
        if(extra.raw) options.body=body;
        else {headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
      }
      const response=await fetchLocal(path,options);
      if(!response.ok) {
        let error='Локальний тест недоступний';
        try {error=(await response.json()).error || error;}catch(_){}
        if(response.status===401 && session) {session=null;onLost();}
        throw Error(error);
      }
      return response;
    }
    async function json(path,body) {return (await request(path,body)).json();}
    return {
      async login(invitation) {
        const result=await json('/api/login',{invitation});
        if(result.role!==role) throw Error('Цей код належить іншій ролі. Відкрий відповідний екран.');
        session=result;return {...result,token:undefined};
      },
      async rotate() {
        session=await json('/api/rotate',{});return {...session,token:undefined};
      },
      async logout() {
        try {await json('/api/logout',{});}finally{session=null;onLost();}
      },
      state:()=>json('/api/state'),
      action:(_role,body)=>json('/api/action',body),
      async upload(orderId,file) {
        if(!file || !Number.isInteger(file.size) || file.size<=0 || file.size>2*1024*1024)
          throw Error('Додай файл до 2 МБ.');
        return (await request('/api/receipt',file,{raw:true,
          headers:{'Content-Type':file.type,'X-Order-ID':orderId}})).json();
      },
      async download(receiptId) {
        if(!/^RCPT-[a-f0-9]{32}$/.test(receiptId)) throw Error('Квитанцію не знайдено');
        const response=await request('/api/receipts/'+receiptId);
        const match=response.headers.get('Content-Disposition')?.match(/filename="(receipt\.(?:png|jpg|pdf))"/);
        if(!match) throw Error('Формат завантаження недійсний');
        return {blob:await response.blob(),filename:match[1]};
      },
    };
  }
  return {create};
});
