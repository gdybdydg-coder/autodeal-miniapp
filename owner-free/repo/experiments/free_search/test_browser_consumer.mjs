import assert from 'node:assert/strict';
import {processAvailable} from './owner_browser_consumer.mjs';
const original=Date.now;Date.now=()=>1000000;
const url='https://auto.ria.com/uk/auto_example_model_12345678.html';
const job=()=>({envelope:{ticket:{version:1,listing_id:'12345678',listing_url:url,
  issued_at:999,expires_at:1002},ticket_sha256:'fixture'},search_ids:[4]});
function store(j){let done=false;return {results:[],records:[],pending:async()=>done?[]:[j],
  finish:async function(_,r){done=true;this.results.push(r)},record:async function(r){this.records.push(r)}}}
function tab(fail=false){let current='',opened=false;return {actions:[],url:async()=>current,
  goto:async function(u){this.actions.push('navigate');current=u},playwright:{
    domSnapshot:async()=>'',getByText(p){
      if(typeof p==='string')return {filter(){return {count:async()=>1,click:async()=>{if(fail)throw Error('unavailable');opened=true}}}};
      return {innerText:async()=> 'ID авто 12345678',filter(){return {
        count:async()=>opened?1:0,waitFor:async()=>{},allTextContents:async()=>[p.source.startsWith('^від')?'від 8 000 $':'до 9 000 $']}}};
    }}}}
try {
 const s=store(job()),t=tab();const r=await processAvailable(t,s);
 assert.equal(r[0].state,'captured');assert.deepEqual(t.actions,['navigate']);
 assert.deepEqual(await processAvailable(t,s),[]);assert.equal(t.actions.length,1);
 const expired=job();expired.envelope.ticket.expires_at=999;
 const e=tab();assert.equal((await processAvailable(e,store(expired)))[0].state,'expired');assert.deepEqual(e.actions,[]);
 const foreign=job();foreign.envelope.ticket.listing_url='https://evil.invalid/';
 const f=tab();await assert.rejects(processAvailable(f,store(foreign)),/invalid_browser_job_url/);assert.deepEqual(f.actions,[]);
 const blocked=store(job()),b=tab(true);assert.equal((await processAvailable(b,blocked))[0].state,'blocked');
 assert.deepEqual(await processAvailable(b,blocked),[]);assert.equal(b.actions.length,1);
 console.log(JSON.stringify({status:'passed',checks:5,network_calls:0}));
} finally {Date.now=original}
