const {test}=require('node:test');
const assert=require('node:assert/strict');
const {create}=require('./cloud-api.js');
test('outside Telegram never calls server',async()=>{
  const api=create(()=>{throw Error('must not fetch');},()=> '');
  await assert.rejects(api.list(),/Telegram/);
});
test('save sends raw authentication only in header and always disables delivery',async()=>{
  const api=create(async(url,opts)=>{
    assert.equal(url,'https://autodeal-api.onrender.com/api/subscriptions');
    assert.equal(opts.headers['X-Telegram-Init-Data'],'fake-signed-session');
    assert.equal(opts.redirect,'error');assert.equal(opts.credentials,'omit');
    assert.deepEqual(JSON.parse(opts.body),{name:'Test',filters:{brand:'BMW'},enabled:false});
    return {ok:true,status:200,json:async()=>({id:1})};
  },()=> 'fake-signed-session');
  assert.deepEqual(await api.save('Test',{brand:'BMW'}),{id:1});
});
test('expired authentication is actionable and does not reflect server body',async()=>{
  const api=create(async()=>({ok:false,status:401}),()=> 'fake');
  await assert.rejects(api.list(),/відкрий знову/);
});
test('delete handles empty 204 and rejects unsafe IDs',async()=>{
  let calls=0;
  const api=create(async(url,opts)=>{calls++;assert.equal(opts.method,'DELETE');return {ok:true,status:204};},()=> 'fake');
  assert.equal(await api.remove(1),null);
  await assert.rejects(api.remove('../2'));assert.equal(calls,1);
});
test('network errors give recovery guidance',async()=>{
  const api=create(async()=>{throw new TypeError('network');},()=> 'fake');
  await assert.rejects(api.list(),/Онови список/);
});
test('timeout aborts pending request',async()=>{
  const api=create((url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>{
    const e=new Error();e.name='AbortError';reject(e);
  })),()=> 'fake',5);
  await assert.rejects(api.list(),/Немає відповіді/);
});
test('search quota waits distinguish temporary and total limits',async()=>{
  const api=create(async()=>({ok:false,status:429,json:async()=>({detail:'quota_exceeded',
    quota:{reason:'hourly',retry_after_seconds:61}})}),()=> 'fake');
  await assert.rejects(api.search({}),/через 2 хв/);
  const total=create(async()=>({ok:false,status:429,json:async()=>({detail:'quota_exceeded',
    quota:{reason:'total',retry_after_seconds:null}})}),()=> 'fake');
  await assert.rejects(total.search({}),/залишок пакета/);
});
