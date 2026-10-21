// Synthetic documented-CUA adapter contract only; no browser or network.
import assert from 'node:assert/strict';
import {captureHandoff} from './owner_browser_capture.mjs';
const originalNow=Date.now;
let now=1000000;
Date.now=()=>now;
const url='https://auto.ria.com/uk/auto_example_model_12345678.html';
const handoff={ticket:{version:1,listing_id:'12345678',listing_url:url,
  issued_at:999,expires_at:1002},ticket_sha256:'fixture'};
function tab({target=url,ambiguous=false,slow=false}={}) {
  return {url:async()=>target,playwright:{getByText(pattern){
    return {innerText:async()=>'ID авто 12345678',filter(){return {
      allTextContents:async()=>{
        if(slow)now=1003000;
        if(ambiguous)return [];
        return [pattern.source.startsWith('^від')?'від 15 167 $':'до 16 764 $'];
      }}}};
  }}};
}
try {
  const result=await captureHandoff(tab(),handoff);
  assert.equal(result.capture.listing_id,'12345678');
  assert.equal(result.capture.lower_text[0],'від 15 167 $');
  assert.deepEqual(result.ticket,handoff.ticket);
  await assert.rejects(captureHandoff(tab({target:url+'?other'}),handoff),/url_mismatch/);
  await assert.rejects(captureHandoff(tab({ambiguous:true}),handoff),/range_ambiguous/);
  await assert.rejects(captureHandoff(tab({slow:true}),handoff),/expired_during_capture/);
  await assert.rejects(captureHandoff(tab(),handoff),/expired_or_future/);
  console.log(JSON.stringify({status:'passed',checks:5,browser_actions:0,network_calls:0,
    evidence:'synthetic CUA tab adapter; not live site verification'}));
} finally {Date.now=originalNow;}
