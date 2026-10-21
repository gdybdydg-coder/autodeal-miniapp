// Standalone product worker, NOT a CUA session or a hidden browser connection.
// Activation is separate. No imported cookies, stealth flags, API client or retry.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {processAvailable} from './owner_browser_consumer.mjs';
import {openListingRange,captureRange} from './owner_browser_capture.mjs';

export function validLease(value, token, now) {
  return value?.version===1 && value.token===token && value.enabled===true &&
    Number.isFinite(value.heartbeat) && now>=value.heartbeat && now-value.heartbeat<=15 &&
    Number.isFinite(value.access_at) && now>=value.access_at && now-value.access_at<=90;
}

export function permittedRequest(raw) {
  try {
    const u=new URL(raw);
    return u.protocol==='https:' && !u.username && !u.password &&
      (u.hostname==='auto.ria.com' || u.hostname.endsWith('.riastatic.com') ||
       u.hostname==='riastatic.com');
  } catch { return false; }
}

export function responseHold(host,pathname,status) {
  // An anonymous page asks for the signed-in user's bookmarks. A 401 here
  // conveys no listing/valuation data and is never retried or authenticated.
  if(host==='auto.ria.com' && pathname==='/notepad/api/bookmark/list' && status===401)return false;
  return host==='auto.ria.com' && [401,403,429].includes(status);
}

export async function atomicJson(filename, value) {
  const temp=filename+'.'+process.pid+'.tmp';
  try {
    await fs.writeFile(temp,JSON.stringify(value));
    for(let attempt=0;attempt<10;attempt++) {
      try {await fs.rename(temp,filename);break;}
      catch(error) {
        if(!['EPERM','EACCES'].includes(error.code)||attempt===9)throw error;
        await new Promise(resolve=>setTimeout(resolve,20*(attempt+1)));
      }
    }
  } finally {await fs.unlink(temp).catch(error=>{if(error.code!=='ENOENT')throw error;});}
}

export class DiskStore {
  constructor(root) { this.root=root; }
  async pending() {
    const names=await fs.readdir(this.root), jobs=[];
    for (const name of names.filter(n=>/^[1-9][0-9]{0,11}\.job\.json$/.test(n))) {
      if(names.includes(name.replace('.job.','.result.')))continue;
      jobs.push(JSON.parse(await fs.readFile(path.join(this.root,name),'utf8')));
    }
    return jobs.sort((a,b)=>a.envelope.ticket.expires_at-b.envelope.ticket.expires_at);
  }
  async record(value) {
    await fs.appendFile(path.join(this.root,'browser-http.jsonl'),JSON.stringify(value)+'\n');
  }
  async finish(job,value) {
    const id=job.envelope.ticket.listing_id;
    if (!/^[1-9][0-9]{0,11}$/.test(id) || value.listing_id!==id)throw Error('result_identity');
    // A committed result can never be overwritten on restart.
    const temp=path.join(this.root,id+'.result.pending.'+process.pid);
    await fs.writeFile(temp,JSON.stringify(value),{flag:'wx'});
    try { await fs.link(temp,path.join(this.root,id+'.result.json')); }
    finally { await fs.unlink(temp); }
  }
}

// Adapt native Playwright to the existing tested capture contract. Only visible
// DOM text is read. No hidden application state or direct native BFF requests.
export function tabAdapter(page, guard) {
  function locator(value) {
    return new Proxy(value,{get(target,key) {
      if(key==='waitFor')return opts=>target.waitFor({state:opts.state,timeout:opts.timeoutMs});
      if(key==='filter')return opts=>locator(target.filter(opts));
      if(key==='click')return async opts=>{await guard();return target.click(opts);};
      const v=target[key]; return typeof v==='function'?v.bind(target):v;
    }});
  }
  return {url:()=>page.url(),goto:async url=>{
    await guard();await page.goto(url,{waitUntil:'load',timeout:20000});
    // SSR exposes a clickable price label before the site's event handlers
    // attach. Wait for initial hydration traffic to settle, bounded and with
    // no retry, before clicking the visible native control.
    await page.waitForLoadState('networkidle',{timeout:15000});await guard();
  },
    playwright:{getByText:(text,opts)=>locator(page.getByText(text,opts)),
      domSnapshot:()=>page.locator('body').innerText()}};
}

export async function serve({root,token,launch,clock=()=>Date.now()/1000,sleep=ms=>new Promise(r=>setTimeout(r,ms)),maxCycles=Infinity,probeUrl=null}) {
  const outbox=path.join(root,'continuous-browser-outbox'),store=new DiskStore(outbox);
  await fs.mkdir(outbox,{recursive:true});
  const control=path.join(root,'owner-service-control.json');
  const hold=path.join(root,'owner-browser-hold.json');
  const lock=path.join(outbox,'browser-service.lock');
  let handle,context,chain=Promise.resolve(),halt=false,requestId=0,cycles=0;
  const record=value=>{chain=chain.then(()=>store.record(value));chain.catch(()=>{halt=true;});};
  async function guard() {
    if(halt)throw Error('browser_source_hold');
    try {await fs.access(hold);throw Error('browser_source_hold');}
    catch(e){if(e.code!=='ENOENT')throw e;}
    const lease=JSON.parse(await fs.readFile(control,'utf8'));
    if(!validLease(lease,token,clock()))throw Error('browser_lease_not_current');
  }
  try {
    await guard();
    handle=await fs.open(lock,'wx');await handle.writeFile(JSON.stringify({token,pid:process.pid,at:clock()}));
    context=await launch();
    await context.route('**/*',async route=>{
      const request=route.request(),raw=request.url();
      // Prevent paid endpoints and unrelated requests. Native site UI still
      // performs its own public popup requests; these are logged, never forged.
      if(halt||!permittedRequest(raw)) {
        const u=new URL(raw);
        record({kind:'http_blocked',at:clock(),host:u.hostname,path:u.pathname,resource:request.resourceType(),reason:halt?'source_hold':'host_allowlist'});
        return route.abort();
      }
      try {await guard();}catch {return route.abort();}
      record({kind:'http_started',seq:++requestId,at:clock(),method:request.method(),
        host:new URL(raw).hostname,path:new URL(raw).pathname,resource:request.resourceType(),paid_api_calls:0});
      await route.continue();
    });
    context.on('response',response=>{
      const u=new URL(response.url());
      record({kind:'http_response',at:clock(),host:u.hostname,path:u.pathname,status:response.status(),bytes:null});
      if(responseHold(u.hostname,u.pathname,response.status())){
        halt=true;
        chain=chain.then(()=>atomicJson(hold,{at:clock(),reason:'source_status_'+response.status(),path:u.pathname,resume_requires_review:true}));
      }
    });
    const page=await context.newPage();
    page.on?.('pageerror',error=>record({kind:'page_error',at:clock(),message:String(error.message).slice(0,240)}));
    const tab=tabAdapter(page,guard);
    if(probeUrl) {
      const match=/^https:\/\/auto\.ria\.com\/uk\/auto_[A-Za-z0-9_-]+_([1-9][0-9]{0,11})\.html$/.exec(probeUrl);
      if(!match)throw Error('invalid_probe_url');
      let action,capture;
      try {
        await tab.goto(probeUrl);
        await fs.writeFile(path.join(root,'owner-browser-probe-before.txt'),await page.locator('body').innerText());
        action=await openListingRange(tab,match[1]);
        capture=await captureRange(tab,match[1]);
      } catch(error) {
        await fs.writeFile(path.join(root,'owner-browser-probe-after.txt'),await page.locator('body').innerText());
        await page.screenshot({path:path.join(root,'owner-browser-probe.png')});
        await atomicJson(path.join(root,'owner-browser-probe.json'),{kind:'native_browser_smoke_only',
          state:'blocked',reason:String(error.message).slice(0,240),at:clock(),paid_api_calls:0,telegram_calls:0});
        throw error;
      }
      await page.screenshot({path:path.join(root,'owner-browser-probe.png')});
      await atomicJson(path.join(root,'owner-browser-probe.json'),{
        kind:'native_browser_smoke_only',at:clock(),action,capture,request_count_session:requestId,
        first_seen:null,publication_at:null,paid_api_calls:0,telegram_calls:0,send_authorized:false});
      return;
    }
    store.review=async job=>{
      await guard();
      const text=await page.locator('body').innerText({timeout:5000});
      if(text.length>200000)throw Error('visible_body_oversize');
      return {kind:'public_visible_body',listing_id:job.envelope.ticket.listing_id,
        listing_url:page.url(),observed_at:clock(),text};
    };
    while(cycles++<maxCycles) {
      await guard();
      const results=await processAvailable(tab,store,{limit:1,clock});
      record({kind:'browser_cycle',at:clock(),processed:results.length});
      await atomicJson(path.join(root,'owner-browser-status.json'),{at:clock(),token,state:'running',cycles,request_count_session:requestId});
      if(results.some(r=>r.state==='blocked')) {
        await atomicJson(hold,{at:clock(),reason:'native_capture_blocked',resume_requires_review:true});
        break; // Do not hammer a changed/challenged UI with subsequent jobs.
      }
      await sleep(results.length?4000:500);
    }
  } finally {
    try {if(context)await context.close();await chain;}
    finally {if(handle) {await handle.close();await fs.unlink(lock);}}
  }
}

if(process.argv[1] && import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href) {
  const [root,token,probeFlag,probeUrl]=process.argv.slice(2);
  if(!root||!token)throw Error('workspace_and_lease_token_required');
  if(probeFlag && (probeFlag!=='--probe'||!probeUrl))throw Error('invalid_probe_option');
  process.env.PLAYWRIGHT_BROWSERS_PATH=process.env.AUTODEAL_OWNER_BROWSER_BINARIES || path.join(path.resolve(root),'owner-browser-binaries');
  // Separate profile; never connects to or copies a user's existing browser.
  const {chromium}=await import('playwright');
  await serve({root:path.resolve(root),token,probeUrl:probeFlag?probeUrl:null,launch:()=>chromium.launchPersistentContext(
    path.join(root,'owner-browser-profile'),{headless:true,serviceWorkers:'block',acceptDownloads:false})});
}
