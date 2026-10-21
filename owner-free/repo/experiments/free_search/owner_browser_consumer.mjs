// Active-session CUA consumer only: no Playwright server, HTTP client or daemon.
// The caller provides an existing documented CUA tab and durable job-store I/O.
import {openHandoffRange} from './owner_browser_capture.mjs';

export async function processAvailable(tab, store, {limit=3, clock=()=>Date.now()/1000}={}) {
  if (!Number.isInteger(limit)||limit<1||limit>5) throw Error('invalid_browser_batch');
  const results=[];
  for (const job of (await store.pending()).slice(0,limit)) {
    const ticket=job.envelope?.ticket;
    const ident=ticket?.listing_id;
    if (!/^[1-9][0-9]{0,11}$/.test(ident||'')) throw Error('invalid_browser_job');
    if (!new RegExp(`^https://auto\\.ria\\.com/uk/auto_[A-Za-z0-9_-]+_${ident}\\.html$`).test(ticket.listing_url||''))
      throw Error('invalid_browser_job_url');
    if (!Number.isFinite(ticket.expires_at)||clock()>=ticket.expires_at) {
      const result={listing_id:ident,state:'expired',at:clock()};
      await store.finish(job,result);results.push(result);continue;
    }
    await store.record({action:'native_listing_capture_started',listing_id:ident,at:clock(),http_requests:null,paid_api_calls:0});
    try {
      if (await tab.url()!==ticket.listing_url) await tab.goto(ticket.listing_url);
      const visible_review=store.review ? await store.review(job,tab) : undefined;
      // Navigation verified by the adapter against the listing ID visible in DOM.
      const envelope=await openHandoffRange(tab,job.envelope);
      const result={listing_id:ident,state:'captured',envelope,search_ids:job.search_ids,at:clock(),
        ...(visible_review ? {visible_review} : {})};
      await store.finish(job,result);results.push(result);
    } catch(error) {
      const result={listing_id:ident,state:'blocked',reason:String(error.message).slice(0,240),at:clock()};
      await store.finish(job,result);results.push(result); // no retry/navigation fallback
    }
  }
  return results;
}
