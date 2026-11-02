/** Isolated browser reader for RIA's real date-sorted search, never a bot.
 * No login, paid API calls, stealth plugins, proxy rotation or CAPTCHA handling.
 * Browser navigation is used because /car/ silently ignores sort parameters.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const SORT = 'dates.created.desc';
export function accessDenied({status,body,visibleCaptcha,validContent,challengeHeader}) {
  if([401,403,429].includes(status)||challengeHeader==='challenge')return true;
  // A widget or quoted phrase in listing text is not an access challenge.
  return !validContent && (visibleCaptcha || /verify you are human|access denied|just a moment|підтверд.*не робот/i.test(body));
}
export function limitedSourceRequest(responseUrl,kind,status,challengeHeader) {
  return new URL(responseUrl).origin==='https://auto.ria.com' &&
    ['document','fetch','xhr'].includes(kind) && (status===429 || challengeHeader==='challenge');
}
function retryAfterSeconds(retryAfter, now) {
  const text=String(retryAfter??'').trim();
  const retry=/^\d+$/.test(text)?Number(text):Math.max(0,Date.parse(text)/1000-now);
  return Number.isFinite(retry)?retry:0;
}
export function sourceHoldSeconds(retryAfter, failures, now=Date.now()/1000) {
  return Math.max(3600*Math.pow(2,Math.min(4,Math.max(0,failures-1))),retryAfterSeconds(retryAfter,now));
}
export function serverErrorHoldSeconds(retryAfter, failures, now=Date.now()/1000) {
  return Math.max(Math.min(3600,300*2**Math.min(4,Math.max(0,failures-1))),retryAfterSeconds(retryAfter,now));
}
export function searchIdentity(value) {
  const u = new URL(value);
  if (u.origin !== 'https://auto.ria.com' || u.pathname !== '/uk/search/' || u.hash || u.username || u.password)
    throw Error('unsupported_sorted_search');
  const entries = [...u.searchParams];
  if (new Set(entries.map(([k])=>k)).size !== entries.length) throw Error('duplicate_query');
  // One observed, unambiguous representation; no guessing between new/old schemas.
  const allowed = new Set(['indexName','s_yers[0]','po_yers[0]','marka_id[0]','model_id[0]',
    'order_by','abroad','custom','page','countpage','category_id','currency','price_ot','price_do','raceFrom','raceTo']);
  const arrayKey=/^(state|city|bodystyle|type|gearbox)\[\d{1,2}\]$/;
  if (entries.some(([k])=>!allowed.has(k)&&!arrayKey.test(k)) || u.searchParams.get('order_by') !== '7')
    throw Error('unsupported_sort_or_filter');
  if (!['10','20','50'].includes(u.searchParams.get('countpage'))) throw Error('invalid_page_size');
  for (const [key,value] of entries)
    if(key!=='indexName' && !/^\d+$/.test(value)) throw Error('invalid_'+key);
  for(const key of ['page','abroad','custom'])if(!u.searchParams.has(key))throw Error('missing_'+key);
  u.searchParams.delete('page');
  u.searchParams.sort();
  return u.href;
}

/** DOM-only extraction, also usable with the computer-use browser's read-only evaluate. */
export function readSortedDOM() {
  // Nested OFTD promotional blocks are extra placements outside pagination.
  const items = [...document.querySelectorAll('main .items-list > a[data-car-id]')];
  const sortConfirmed = [...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='Дата додавання');
  const totalText = document.querySelector('#SortButtonContentCount')?.textContent.trim() ?? '';
  const countMatch = totalText.match(/^([0-9\s]+)\s+пропозиці[яїй]$/);
  return {url:location.href, sort:sortConfirmed?'dates.created.desc':null,
    total:countMatch?Number(countMatch[1].replace(/\s/g,'')):null,
    cards:items.map(e=>({id:e.getAttribute('data-car-id'),url:new URL(e.getAttribute('href'),location.href).href,
      age_text:e.querySelector('.jc-between.wrap.ai-center.gap-12.mt-8')?.textContent.trim() ?? null,
      promoted:[...e.querySelectorAll('span')].some(s=>/^ТОП(?:\s+\d+)?$/.test(s.textContent.trim()))}))};
}

export function validatePage(page, identity, index) {
  if (page.sort !== SORT || searchIdentity(page.url) !== identity || Number(new URL(page.url).searchParams.get('page')) !== index)
    throw Error('sort_scope_or_page_changed');
  const size=Number(new URL(page.url).searchParams.get('countpage'));
  if (!Number.isSafeInteger(page.total) || page.total<0 || !Array.isArray(page.cards)) throw Error('unproven_result_count');
  if (page.cards.length!==Math.min(size,Math.max(0,page.total-index*size))) throw Error('page_not_fully_rendered');
  const ids=new Set();
  for (const card of page.cards) {
    if (!/^[1-9]\d{0,11}$/.test(card.id) || ids.has(card.id)) throw Error('invalid_or_duplicate_card');
    const u=new URL(card.url);
    if (u.origin!=='https://auto.ria.com' || !u.pathname.endsWith('_'+card.id+'.html')) throw Error('card_identity_mismatch');
    ids.add(card.id);
  }
  return {size,terminal:(index*size+page.cards.length)>=page.total};
}

export function bootstrapCatalogue(report, head, identity) {
  const scan=report?.scan;
  if (!scan?.enumerated || !scan.scope_verified || !scan.pages?.length) return null;
  const q=new URL(identity).searchParams, source=scan.pages[0].scope;
  for (const [oldKey,newKey] of [['marka_id','marka_id[0]'],['model_id','model_id[0]'],
    ['s_yers[0]','s_yers[0]'],['po_yers[0]','po_yers[0]'],['custom','custom'],['abroad','abroad']])
    if (source[oldKey]!==q.get(newKey)) return null;
  const allowed=new Set(['lang_id','marka_id','model_id','s_yers[0]','po_yers[0]','custom','abroad']);
  if (Object.keys(source).some(k=>!allowed.has(k))) return null;
  const known=new Set(scan.cards.map(c=>c.listing_id));
  if (head.cards.some(c=>!known.has(c.id)) || head.total!==scan.unique_count) return null;
  return {version:1,identity,known_ids:[...known],head_ids:head.cards.filter(c=>!c.promoted).map(c=>c.id),
    checked_at:scan.completed_at,last_full_at:scan.completed_at};
}

/** Inspect full pages; never stop at the first old/promoted ID.
 * Source ordering is asserted by both actual URL and rendered sort control.
 * Reconciliation after long gaps/daily is deliberate, to recover late indexing.
 */
export async function pollSorted(readPage, url, previous=null, {
  baselineReport=null,maxPages=30,overlap=5,now=Date.now()/1000,maxGap=1800,auditAfter=86400,
}={}) {
  const identity=searchIdentity(url);
  if (Number(new URL(url).searchParams.get('page'))!==0 || !Number.isInteger(maxPages) || maxPages<1 || maxPages>100 || !Number.isInteger(overlap) || overlap<2)
    throw Error('invalid_poll_bounds');
  if (previous && (previous.version!==1 || previous.identity!==identity)) throw Error('checkpoint_scope_changed');
  if (previous && (!Array.isArray(previous.known_ids) || !Array.isArray(previous.head_ids)
      || !Number.isFinite(previous.checked_at) || !Number.isFinite(previous.last_full_at)))
    throw Error('invalid_checkpoint');
  let state=previous, known=new Set(state?.known_ids ?? []), anchors=new Set(state?.head_ids ?? []);
  let full=!state || now-state.checked_at>maxGap || now-state.last_full_at>auditAfter || now<state.checked_at;
  const all=[], pages=[], observed=new Set();
  let terminal=false,boundary=false,reason=full?'initial_or_reconciliation':'head_overlap',total=null,duplicates=false;
  for (let index=0;index<maxPages;index++) {
    const next=new URL(url);next.searchParams.set('page',String(index));
    const page=await readPage(next.href);
    const checks=validatePage(page,identity,index);
    if (index===0 && !state && baselineReport) {
      state=bootstrapCatalogue(baselineReport,page,identity);
      if (state) {
        known=new Set(state.known_ids);anchors=new Set(state.head_ids);
        full=now-state.checked_at>maxGap || now-state.last_full_at>auditAfter || now<state.checked_at;
        reason=full?'reconciliation':'catalogue_bootstrap';
      }
    }
    if (total!==null && total!==page.total) throw Error('total_changed_during_poll');
    total=page.total;
    let consecutive=0;
    for (const card of page.cards) {
      if (observed.has(card.id)) duplicates=true;
      else {observed.add(card.id);all.push(card);}
      if (card.promoted) continue;
      consecutive=anchors.has(card.id)?consecutive+1:0;
    }
    pages.push({url:page.url,ids:page.cards.map(c=>c.id),total:page.total,sort:page.sort});
    terminal=checks.terminal;
    // A known suffix is required: an unknown car below a known card extends work.
    boundary=!full && consecutive>=overlap && !duplicates;
    if (terminal || boundary) break;
  }
  const complete=(boundary || terminal && observed.size===total) && !duplicates;
  const newly=state?all.filter(c=>!known.has(c.id)):[];
  const newState=complete?{version:1,identity,known_ids:[...new Set([...known,...observed])],
    head_ids:all.filter(c=>!c.promoted).slice(0,30).map(c=>c.id),checked_at:now,
    last_full_at:terminal?now:state.last_full_at}:null;
  return {complete,reason,terminal,boundary,pages_read:pages.length,pages,observed_cards:all,
    newly_observed:newly,candidates_only:!complete,checkpoint:newState,
    publication_guarantee:false,paid_api_calls:0};
}

async function save(file,value) {
  await fs.mkdir(path.dirname(file),{recursive:true});
  await fs.writeFile(file+'.tmp',JSON.stringify(value,null,2));await fs.rename(file+'.tmp',file);
}

/** Notifications-only activation. This is explicitly NOT a full market census.
 * Record the current head, then follow new arrivals until an old overlap.
 * Never turn an unbridged gap into a batch of purportedly new notifications.
 */
export async function pollHead(readPage,url,previous=null,{maxPages=10,now=Date.now()/1000,overlap=5}={}) {
  const identity=searchIdentity(url);
  if(Number(new URL(url).searchParams.get('page'))!==0 || !Number.isInteger(maxPages) || maxPages<1 || maxPages>100)throw Error('invalid_poll_bounds');
  if(previous && (previous.mode!=='head' || previous.identity!==identity || !Array.isArray(previous.known_ids)
    || !Array.isArray(previous.head_ids) || !Number.isFinite(previous.checked_at)))throw Error('checkpoint_scope_changed');
  const known=new Set(previous?.known_ids??[]),anchors=new Set(previous?.head_ids??[]);
  const pages=[],cards=[],unique=new Set(),newly=[],unclassified=[];
  let boundary=false,initial=!previous,run=0,sourceChanged=false,lastTotal=null;
  for(let i=0;i<maxPages;i++) {
    const next=new URL(url);next.searchParams.set('page',String(i));
    const page=await readPage(next.href),check=validatePage(page,identity,i);
    if(lastTotal!==null && page.total!==lastTotal)sourceChanged=true;
    lastTotal=page.total;
    pages.push({url:page.url,ids:page.cards.map(c=>c.id),total:page.total,sort:page.sort});
    for(const c of page.cards) {
      if(unique.has(c.id))continue;
      unique.add(c.id);cards.push(c);
      if(!boundary && previous && !known.has(c.id))newly.push(c);
      else if(boundary && previous && !known.has(c.id))unclassified.push(c);
      if(!c.promoted){run=anchors.has(c.id)?run+1:0;if(run>=overlap)boundary=true;}
    }
    if(initial || boundary || check.terminal)break;
  }
  const complete=initial || boundary;
  return {complete,reason:initial?'head_baseline':boundary?'head_overlap':'overlap_not_found',
    full_catalogue:false,source_changed:sourceChanged,pages_read:pages.length,pages,observed_cards:cards,
    newly_observed:complete?newly:[],unclassified_after_overlap:unclassified,candidates_only:!complete,paid_api_calls:0,publication_guarantee:false,
    checkpoint:complete?{version:1,mode:'head',identity,known_ids:[...new Set([...known,...[...unique].filter(id=>!unclassified.some(c=>c.id===id))])],
      head_ids:cards.filter(c=>!c.promoted&&!unclassified.some(u=>u.id===c.id)).slice(0,50).map(c=>c.id),checked_at:now}:null};
}

export async function pollGroup(readPage, url, checkpoints, options) {
  const pages = new Map();
  const results = {};
  async function sharedRead(pageUrl) {
    if (!pages.has(pageUrl)) pages.set(pageUrl, await readPage(pageUrl));
    return pages.get(pageUrl);
  }
  for (const [key, previous] of Object.entries(checkpoints)) {
    results[key] = await pollHead(sharedRead, url, previous, options);
  }
  return results;
}

async function load(file,fallback=null) {
  try{return JSON.parse(await fs.readFile(file,'utf8'));}catch(e){if(e.code==='ENOENT')return fallback;throw e;}
}

async function main() {
  const args=process.argv.slice(2),options={};
  for(let i=0;i<args.length;i+=2){if(!args[i]?.startsWith('--') || !args[i+1])throw Error('expected --name value');options[args[i].slice(2)]=args[i+1];}
  for(const key of ['url','output','cache-dir'])if(!options[key])throw Error('missing --'+key);
  const detail=options['detail-id'];
  if(detail){
    if(!/^[1-9]\d{0,11}$/.test(detail)||!new RegExp('^https://auto\\.ria\\.com/uk/auto_[A-Za-z0-9_-]+_'+detail+'\\.html$').test(options.url))throw Error('invalid_detail');
  }else{searchIdentity(options.url);if(!options.state)throw Error('missing --state');}
  const cache=path.resolve(options['cache-dir']);await fs.mkdir(cache,{recursive:true});
  // Share the Python client's pacing/holds and exclusive lock.
  const lock=path.join(cache,'client.lock'),transportFile=path.join(cache,'transport.json');
  const handle=await fs.open(lock,'wx');
  let browser;
  try {
    const ledger=await load(transportFile,{next_at:0,hold_until:0,attempts:[]});
    let sourceDenied=false;
    function recordDenial(status,retryAfter,visibleCaptcha=false,validContent=false) {
      const now=Date.now()/1000;
      if(!sourceDenied){
        sourceDenied=true;
        const last=ledger.last_denied_at??ledger.last_failure?.at??0;
        ledger.denials=(now-last<86400?(ledger.denials??0):0)+1;
        ledger.last_denied_at=now;
        ledger.last_failure={at:now,status,visible_captcha:visibleCaptcha,valid_content:validContent};
        ledger.hold_reason='source_denied_or_challenge';
        ledger.hold_until=now+sourceHoldSeconds(retryAfter,ledger.denials);
      }else{
        ledger.hold_until=Math.max(ledger.hold_until,now+retryAfterSeconds(retryAfter,now));
      }
    }
    process.env.PLAYWRIGHT_BROWSERS_PATH=process.env.AUTODEAL_OWNER_BROWSER_BINARIES || '/opt/browsers';
    const {chromium}=await import('playwright');
    browser=await chromium.launch({headless:true});
    const context=await browser.newContext({serviceWorkers:'block'});
    // These routes belong to the paid developer API and are never needed here.
    await context.route('**/*',async route=>{
      const host=new URL(route.request().url()).hostname;
      if(sourceDenied || host==='developers.ria.com' || host==='api.telegram.org' || ['image','media','font'].includes(route.request().resourceType()))await route.abort();else await route.continue();
    });
    const tab=await context.newPage();
    tab.on('response',r=>{
      const headers=r.headers();
      if(limitedSourceRequest(r.url(),r.request().resourceType(),r.status(),headers['cf-mitigated']))
        recordDenial(r.status(),headers['retry-after']);
    });
    let requests=0;
    async function reserveNavigation() {
      const now=Date.now()/1000;
      if(ledger.hold_until>now)throw Error(ledger.hold_reason ?? 'source_hold');
      ledger.attempts=ledger.attempts.filter(t=>now-t<86400);
      const daily=Number(options['daily-limit']??500);
      if(!Number.isInteger(daily)||daily<1||daily>2400)throw Error('invalid_daily_limit');
      const hourly=ledger.attempts.filter(t=>now-t<3600);
      if(ledger.attempts.length>=daily || hourly.length>=120){
        ledger.hold_until=Math.max(now+30,ledger.attempts.length>=daily?ledger.attempts.at(-daily)+86401:0,
          hourly.length>=120?hourly.at(-120)+3601:0);
        ledger.hold_reason='local_navigation_budget';await save(transportFile,ledger);
        throw Error(ledger.hold_reason);
      }
      if(requests>=100)throw Error('per_run_navigation_budget');
      const wait=Math.max(0,ledger.next_at-now);
      if(wait>60)throw Error('wait_until_next_at');
      if(wait)await new Promise(r=>setTimeout(r,wait*1000));
      ledger.attempts.push(Date.now()/1000);requests++;ledger.next_at=Date.now()/1000+30;await save(transportFile,ledger);
    }
    async function readPage(url, comparison=null) {
      await reserveNavigation();
      try {
        let status,retryAfter,body,challengeHeader,visibleCaptcha=false,validContent=false;
        if(comparison) {
          const started=Date.now();
          const response=await fetch(url,{redirect:'manual',signal:AbortSignal.timeout(20000)});
          status=response.status;retryAfter=response.headers.get('retry-after');
          challengeHeader=response.headers.get('cf-mitigated');
          comparison.http_status=status;
          const html=await response.text();
          comparison.http_ms=Date.now()-started;
          const parsed=await tab.evaluate(markup=>{
            const template=document.createElement('template');template.innerHTML=markup;
            const root=template.content;
            root.querySelectorAll('script,style,noscript').forEach(el=>el.remove());
            return {ids:[...root.querySelectorAll('main .items-list > a[data-car-id]')].map(el=>el.dataset.carId),
              title:root.querySelector('title')?.textContent??'',text:(root.textContent??'').slice(0,5000)};
          },html);
          comparison.http_cards=parsed.ids.length;
          comparison.common_ids=parsed.ids.filter(id=>comparison.browser_ids.includes(id)).length;
          validContent=parsed.ids.some(id=>/^[1-9]\d*$/.test(id));
          body=parsed.title+' '+parsed.text;
        } else {
          const response=await tab.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
          status=response?.status();retryAfter=response?.headers()['retry-after'];
          challengeHeader=response?.headers()['cf-mitigated'];
          body=await tab.locator('body').innerText();
          visibleCaptcha=await tab.locator('iframe[src*="captcha"],.g-recaptcha').evaluateAll(es=>es.some(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden'));
          if(detail)validContent=body.replace(/\s+/g,' ').includes('ID авто '+detail);
          else {
            try{validatePage(await tab.evaluate(readSortedDOM),searchIdentity(url),Number(new URL(url).searchParams.get('page')));validContent=true;}catch{}
          }
        }
        if(accessDenied({status,body,visibleCaptcha,validContent,challengeHeader})) {
          recordDenial(status,retryAfter,visibleCaptcha,validContent);
          await save(transportFile,ledger);throw Error(ledger.hold_reason);
        }
        if(sourceDenied)throw Error('source_denied_or_challenge');
        if(detail && [404,410].includes(status))throw Error('listing_gone');
        if(status!==200){
          const now=Date.now()/1000;
          const temporary=[500,502,503,504].includes(status);
          if(temporary)ledger.server_errors=(ledger.server_errors??0)+1;
          const wait=temporary?serverErrorHoldSeconds(retryAfter,ledger.server_errors,now):sourceHoldSeconds(retryAfter,1,now);
          ledger.hold_until=now+wait;ledger.hold_reason='http_'+status;
          ledger.last_failure={at:now,status,retry_after_seconds:retryAfterSeconds(retryAfter,now),visible_captcha:visibleCaptcha,valid_content:validContent};
          await save(transportFile,ledger);throw Error(ledger.hold_reason);
        }
        if(comparison)return;
        ledger.server_errors=0;
        if(detail){
          const {tabAdapter}=await import('./owner_browser_service.mjs');
          const {openListingRange,captureRange}=await import('./owner_browser_capture.mjs');
          // Background traffic can continue after the page is interactive.
          await tab.waitForLoadState('networkidle',{timeout:8000}).catch(e=>{if(e.name!=='TimeoutError')throw e;});
          const adapter=tabAdapter(tab,async()=>{});
          try {
            await openListingRange(adapter,detail);
          } catch(error) {
            const visible=await tab.locator('body').innerText({timeout:2000}).catch(()=>'');
            const hasIdentity=new RegExp('ID\\s+авто\\s+'+detail+'\\b','i').test(visible);
            if(error.message==='listing_identity_not_visible') {
              const info=await tab.evaluate(expected=>{
                const text=document.body.innerText;
                const tags=[...document.querySelectorAll('script[type="application/ld+json"]')];
                const vehicle=tags.some(s=>/"@type"\s*:\s*"(?:Car|Vehicle)"/.test(s.textContent));
                const idFragment=text.match(new RegExp('(?:^|\\n)([^\\n]{0,25})'+expected+'(?=\\s|$)'));
                const title=document.title.trim();
                return {lang:document.documentElement.lang.slice(0,12),vehicle,
                  title_kind:/AUTO.RIA/i.test(title)?'auto_ria':(/^[\p{L}\s.!–-]{1,90}$/u.test(title)?title:'other'),
                  canonical:document.querySelector('link[rel=canonical]')?.href===location.href,
                  has_id:text.includes(expected),id_label:idFragment?.[1]?.replace(/[^\p{L} :№#]/gu,'').slice(0,25),
                  visible_chars:text.length,price_control:/Середня ціна|Средняя цена/i.test(text),
                  h1:!!document.querySelector('h1'),cookie:/cookie|файл[иів]+ cookie/i.test(text),
                  sample:!vehicle&&text.length<2000?text.split('\n').map(s=>s.trim()).filter(s=>/^[\p{L}\s.!?,’'–-]{3,110}$/u.test(s)).slice(0,3):[]};
              },detail);
              console.log(JSON.stringify({event:'detail_layout',...info}));
            }
            const captcha=await tab.locator('iframe[src*="captcha"],.g-recaptcha').filter({visible:true}).count();
            if(accessDenied({status,body:visible,visibleCaptcha:captcha>0,validContent:hasIdentity}))
              recordDenial(status,retryAfter,captcha>0,hasIdentity);
            throw error;
          }
          return {listing_id:detail,url:tab.url(),html:await tab.content(),received_at:Date.now()/1000,
            capture:await captureRange(adapter,detail),paid_api_calls:0};
        }
        await tab.getByRole('button',{name:'Дата додавання',exact:true}).first().waitFor({timeout:20000});
        // Render readiness, not a guessed sleep. Empty searches fail closed here.
        await tab.locator('main .items-list > a[data-car-id]').first().waitFor({timeout:20000});
        const snapshot=await tab.evaluate(readSortedDOM);
        validatePage(snapshot,searchIdentity(url),Number(new URL(url).searchParams.get('page')));
        return snapshot;
      } finally {
        // Spread load rather than synchronizing bursts; no fingerprint spoofing.
        ledger.next_at=Date.now()/1000+10+Math.random()*5;await save(transportFile,ledger);
        if(sourceDenied)throw Error('source_denied_or_challenge');
      }
    }
    if(detail){await save(options.output,await readPage(options.url));console.log(JSON.stringify({detail_id:detail,captured:true}));return;}
    if(options['grouped-heads']==='true'){
      const results=await pollGroup(readPage, options.url, await load(options.state),
        {maxPages:Number(options['max-pages'] ?? 10)});
      await save(options.output, results);
      if(options['compare-http']==='true') {
        const file=path.join(cache,'access-comparison-v1.json');
        if(!await load(file)){
          const ids=Object.values(results)[0].pages[0].ids;
          const summary={browser_status:200,browser_cards:ids.length,state:'started'};
          await save(file,summary);
          summary.browser_ids=ids;
          try {
            await readPage(options.url,summary);
            summary.state='finished';
          } catch(e) {
            summary.state=/^(source_denied_or_challenge|local_navigation_budget|http_\d{3})$/.test(e.message)?e.message:'probe_failed';
          }
          delete summary.browser_ids;
          await save(file,summary);
        }
      }
      return;
    }
    const poll=options['head-only']==='true'?pollHead:pollSorted;
    const result=await poll(readPage,options.url,await load(options.state),{
      baselineReport:options['baseline-report']?await load(options['baseline-report']):null,
      maxPages:Number(options['max-pages'] ?? 30)});
    await save(options.output,result);
    if(result.checkpoint)await save(options.state,result.checkpoint);
    console.log(JSON.stringify({complete:result.complete,pages_read:result.pages_read,newly_observed:result.newly_observed.length,reason:result.reason}));
    if(!result.complete)process.exitCode=2;
  } finally {if(browser)await browser.close();await handle.close();await fs.unlink(lock);}
}

if(process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url))
  main().catch(e=>{console.error(e.message);process.exitCode=1;});
