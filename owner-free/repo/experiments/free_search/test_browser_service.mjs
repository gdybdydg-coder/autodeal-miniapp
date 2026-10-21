import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {validLease,permittedRequest,responseHold,DiskStore,atomicJson,serve,tabAdapter} from './owner_browser_service.mjs';
const root=await fs.mkdtemp(path.join(os.tmpdir(),'autodeal-browser-'));
let checks=0;
try {
  const navigation=[];
  const adapter=tabAdapter({goto:async(u,o)=>navigation.push(o.waitUntil),waitForLoadState:async(s,o)=>{assert.equal(o.timeout,15000);navigation.push(s);}},async()=>navigation.push('guard'));
  await adapter.goto('https://auto.ria.com/uk/auto_x_123.html');
  assert.deepEqual(navigation,['guard','load','networkidle','guard']);checks++;
  const collisionFile=path.join(root,'collision.json');
  await atomicJson(collisionFile,{enabled:false});
  const realRename=fs.rename;let renameAttempts=0;
  fs.rename=async(...args)=>{
    if(++renameAttempts<3){assert.equal(JSON.parse(await fs.readFile(collisionFile)).enabled,false);throw Object.assign(Error('reader sharing'),{code:'EPERM'});}
    return realRename(...args);
  };
  try {await atomicJson(collisionFile,{enabled:true});}
  finally {fs.rename=realRename;}
  assert.equal(renameAttempts,3);assert.equal(JSON.parse(await fs.readFile(collisionFile)).enabled,true);checks++;
  const lease={version:1,token:'test',enabled:true,heartbeat:100,access_at:100};
  assert(validLease(lease,'test',110));checks++;
  for(const altered of [{token:'wrong'},{enabled:false},{heartbeat:80},{heartbeat:111},{access_at:10}]) {
    assert(!validLease({...lease,...altered},'test',110));checks++;
  }
  assert(permittedRequest('https://auto.ria.com/uk/auto_x_123.html'));checks++;
  assert.equal(responseHold('auto.ria.com','/notepad/api/bookmark/list',401),false);checks++;
  for(const [pathname,status] of [['/notepad/api/bookmark/list',403],['/notepad/api/bookmark/list',429],['/uk/auto_x_123.html',401],['/bff/market',401]]) {
    assert.equal(responseHold('auto.ria.com',pathname,status),true);checks++;
  }
  for(const u of ['https://developers.ria.com/auto/search','https://auto.ria.com.evil.com/x','http://auto.ria.com/x','https://user:password@auto.ria.com/x']) {
    assert(!permittedRequest(u));checks++;
  }
  const box=path.join(root,'continuous-browser-outbox');await fs.mkdir(box);
  const store=new DiskStore(box),job={envelope:{ticket:{listing_id:'123',expires_at:200}}};
  await fs.writeFile(path.join(box,'123.job.json'),JSON.stringify(job));
  await store.finish(job,{listing_id:'123',state:'captured'});
  assert.equal((await store.pending()).length,0);checks++;
  await assert.rejects(()=>store.finish(job,{listing_id:'123',state:'changed'}));checks++;
  assert.equal(JSON.parse(await fs.readFile(path.join(box,'123.result.json'))).state,'captured');checks++;
  await atomicJson(path.join(root,'owner-service-control.json'),lease);
  let launched=0,closed=0;
  const launch=async()=>{launched++;return {route:async()=>{},on:()=>{},newPage:async()=>({}),close:async()=>{closed++;}}};
  await serve({root,token:'test',clock:()=>110,launch,sleep:async()=>{},maxCycles:2});
  assert.equal(launched,1);assert.equal(closed,1);checks++;
  await assert.rejects(()=>fs.access(path.join(box,'browser-service.lock')));checks++;
  await fs.writeFile(path.join(box,'browser-service.lock'),'someone_else');
  await assert.rejects(()=>serve({root,token:'test',clock:()=>110,launch,maxCycles:1}));
  assert.equal(await fs.readFile(path.join(box,'browser-service.lock'),'utf8'),'someone_else');checks++;
  await fs.unlink(path.join(box,'browser-service.lock'));
  await atomicJson(path.join(root,'owner-browser-hold.json'),{reason:'403'});
  await assert.rejects(()=>serve({root,token:'test',clock:()=>110,launch,maxCycles:1}));
  assert.equal(launched,1);checks++;
  console.log('PASS browser service',checks,'checks; browser launches/network = 0 (fake context only)');
}finally{await fs.rm(root,{recursive:true,force:true});}
