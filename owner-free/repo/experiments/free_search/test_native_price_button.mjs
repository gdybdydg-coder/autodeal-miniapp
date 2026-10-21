// Contract tests for documented CUA actions; no browser/network.
import assert from 'node:assert/strict';
import {openHandoffRange} from './owner_browser_capture.mjs';
const originalNow=Date.now;
let now=1000000;
Date.now=()=>now;
const url='https://auto.ria.com/uk/auto_example_model_12345678.html';
const handoff={ticket:{version:1,listing_id:'12345678',listing_url:url,
  issued_at:999,expires_at:1002},ticket_sha256:'fixture'};
function tab({target=url,buttons=1,opened=false,missing=false,slow=false,wrongIdentity=false}={}) {
  const actions=[];
  return {actions,url:async()=>target,playwright:{
    domSnapshot:async()=>{actions.push('snapshot');return 'native range';},
    getByText(pattern) {
      if(typeof pattern==='string') {
        assert.equal(pattern,'Середня ціна');
        return {filter(){return {count:async()=>buttons,click:async()=>{
          actions.push('native_button_click');opened=true;if(slow)now=1003000;
        }}}};
      }
      return {innerText:async()=>wrongIdentity?'ID авто 999':'ID авто 12345678',filter(){return {
        count:async()=>opened&&!missing?1:0,
        waitFor:async()=>{if(missing)throw Error('range_ambiguous');},
        allTextContents:async()=>opened&&!missing?
          [pattern.source.startsWith('^від')?'від 15 167 $':'до 16 764 $']:[]
      }}};
    }
  }};
}
try {
  const fresh=tab();
  const result=await openHandoffRange(fresh,handoff);
  assert.deepEqual(fresh.actions,['native_button_click','snapshot']);
  assert.equal(result.capture.kind,'public_market_range');
  assert.equal(result.action.clicked,true);
  const already=tab({opened:true});
  assert.equal((await openHandoffRange(already,handoff)).action.clicked,false);
  assert.deepEqual(already.actions,[]);
  for(const options of [{target:url+'?other'},{buttons:0},{buttons:2},{wrongIdentity:true}]) {
    const rejected=tab(options);
    await assert.rejects(openHandoffRange(rejected,handoff));
    assert.deepEqual(rejected.actions,[]);
  }
  const missing=tab({missing:true});
  await assert.rejects(openHandoffRange(missing,handoff),/range_ambiguous/);
  assert.deepEqual(missing.actions,['native_button_click','snapshot']);
  await assert.rejects(openHandoffRange(tab({slow:true}),handoff),/expired_or_future/);
  console.log(JSON.stringify({status:'passed',checks:8,browser_actions:0,network_calls:0}));
} finally {Date.now=originalNow;}
