const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const id='a'.repeat(32),gen='b'.repeat(32);
const car=(id,extra={})=>({id:String(id),title:'Passat '+id,price_usd:8700,year:2012,mileage:244000,
  fuel:'Дизель',body:'Універсал',transmission:'Автомат',region:'Київ',market:11700,discount:25.6,
  comparables:5,valuation:'sample_median',checked_at:Date.now()/1000,...extra});
const data=(extra={})=>({scan_id:id,generation:gen,status:'queued',cars:[],after:0,inspected:0,
  source_total:5473,discovered:50,deals_found:0,unavailable:0,warnings:[],more_results:false,...extra});
function setup(api) {
  function node(){return {children:[],textContent:'',classList:{add(){}},append(...items){this.children.push(...items)},
    replaceChildren(...items){this.children=items},addEventListener(t,fn){this[t]=fn},scrollIntoView(){throw Error('unexpected scroll')}};}
  const ids=Object.fromEntries(['resultsList','count','sourceNote','results','searchBtn','resultsTitle','resultsCriteria',
    'loadMore','scanProgress','scanControl','scanBar','scanStatus','scanTitle'].map(id=>[id,node()]));
  const timers=new Map();let next=0;
  const window={AutoDealCloud:api,setTimeout:fn=>{timers.set(++next,fn);return next;},clearTimeout:id=>timers.delete(id)};
  vm.runInNewContext(fs.readFileSync(__dirname+'/live-search.js','utf8'),{window,URL,document:{createElement:node,getElementById:id=>ids[id]}});
  return {window,ids,async tick(){const [id,fn]=timers.entries().next().value||[];assert.ok(fn);timers.delete(id);await fn();}};
}
test('all-scan polls automatically beyond eight, retains filters and shows progress',async()=>{
  const calls=[];
  const s=setup({startScan:async f=>{calls.push(f);return data({cars:[car(1)],after:1,inspected:8});},
    scan:async (scanId,after,only)=>{assert.equal(scanId,id);assert.equal(after,1);assert.equal(only,true);
      return data({cars:[car(60)],after:60,inspected:65,deals_found:2,source_total:65,status:'completed',complete:true});}});
  const filters={brand:'Volkswagen',onlyDeals:true};
  await s.window.AutoDealLive.search(filters);filters.brand='Audi';
  assert.equal(s.ids.scanProgress.hidden,false);assert.equal(s.ids.scanBar.value,8);
  assert.match(s.ids.scanStatus.textContent,/8 із 5473/);
  await s.tick();
  assert.equal(calls[0].brand,'Volkswagen');assert.equal(s.ids.scanBar.value,65);
  assert.equal(s.ids.resultsList.children.length,2);assert.equal(s.ids.scanTitle.textContent,'Перевірку завершено');
});
test('quota wait keeps results, pause and resume are explicit owner requests',async()=>{
  let status='waiting';const controls=[];
  const s=setup({startScan:async()=>data({status,error:'quota_exceeded',retry_after_seconds:3610,cars:[car(1)],after:1}),
    scan:async()=>data({status,after:1,cars:[],inspected:8}),
    controlScan:async(scanId,enabled)=>{controls.push([scanId,enabled]);status=enabled?'queued':'paused';}});
  await s.window.AutoDealLive.search({onlyDeals:true});
  assert.match(s.ids.scanStatus.textContent,/61 хв/);
  await s.ids.scanControl.click();
  assert.equal(s.ids.scanTitle.textContent,'Перевірку призупинено');
  assert.equal(s.ids.resultsList.children.length,1);
  await s.ids.scanControl.click();assert.deepEqual(controls,[[id,false],[id,true]]);
});
test('late response from old filters cannot replace the new scan',async()=>{
  let resolve;
  const s=setup({startScan:async filters=>data({scan_id:filters.brand==='Audi'?'c'.repeat(32):id,cars:[car(filters.brand)]}),
    scan:()=>new Promise(r=>resolve=r)});
  await s.window.AutoDealLive.search({brand:'Volkswagen',onlyDeals:true});
  const old=s.tick();
  await s.window.AutoDealLive.search({brand:'Audi',onlyDeals:true});
  resolve(data({cars:[car('old')],status:'completed'}));await old;
  assert.equal(s.ids.resultsList.children[0].children[0].children[0].textContent,'Passat Audi');
});
test('restarted generation refetches from zero and never mixes old results',async()=>{
  const offsets=[];let calls=0;
  const s=setup({startScan:async()=>data({cars:[car('old')],after:99}),scan:async(scanId,after)=>{
    offsets.push(after);return data({generation:'c'.repeat(32),cars:[car('new')],after:1,status:++calls===1?'running':'completed'});}});
  await s.window.AutoDealLive.search({onlyDeals:true});await s.tick();await s.tick();
  assert.deepEqual(offsets,[99,0]);assert.equal(s.ids.resultsList.children.length,1);
  assert.equal(s.ids.resultsList.children[0].children[0].children[0].textContent,'Passat new');
});
test('historical matches stay identifiable without advertising an old discount as current',async()=>{
  const s=setup({startScan:async()=>data({status:'completed',cars:[car(1,{checked_at:1700000000,
    stale:true,historical_match:true,market:null,discount:null,comparables:0,valuation:'stale'})]})});
  await s.window.AutoDealLive.search({onlyDeals:true});
  const text=s.ids.resultsList.children[0].children[0].children.map(n=>n.textContent).join(' ');
  assert.match(text,/під час перевірки/);assert.match(text,/2023/);assert.doesNotMatch(text,/25.6|11,700/);
});
test('large result sets display fifty at a time while all cards remain available',async()=>{
  const s=setup({startScan:async()=>data({status:'completed',complete:true,cars:Array.from({length:120},(_,i)=>car(i))}),
    scan:async()=>data({status:'completed',complete:true})});
  await s.window.AutoDealLive.search({onlyDeals:true});
  assert.equal(s.ids.resultsList.children.length,50);assert.equal(s.ids.loadMore.hidden,false);
  await s.window.AutoDealLive.loadMore();assert.equal(s.ids.resultsList.children.length,100);
  await s.window.AutoDealLive.loadMore();assert.equal(s.ids.resultsList.children.length,120);
});

test('cached cars appear before any scan progress and refresh removes invalid matches',async()=>{
  const s=setup({startScan:async()=>data({cache:{cars:[car(1)],after:17,total:1,more:false}}),
    scan:async(scanId,after,only,cacheAfter)=>{
      assert.equal(after,0);assert.equal(cacheAfter,17);
      return data({removed:[{id:'1',checked_at:Date.now()/1000+1}],after:1,inspected:1,status:'completed',complete:true,
        cache:{cars:[],after:17,total:0,more:false}});
    }});
  await s.window.AutoDealLive.search({onlyDeals:true});
  assert.equal(s.ids.resultsList.children[0].className,'car');
  assert.equal(s.ids.scanBar.value,0);assert.match(s.ids.sourceNote.textContent,/ще не охоплює всі/);
  await s.tick();
  assert.equal(s.ids.resultsList.children[0].className,'empty');
  assert.equal(s.ids.count.textContent,'Показано: 0');
});

test('older cached copies cannot overwrite a fresher result or create duplicates',async()=>{
  const fresh=car(1,{checked_at:Date.now()/1000,price_usd:8500});
  const old=car(1,{checked_at:fresh.checked_at-100,price_usd:8700});
  const s=setup({startScan:async()=>data({cars:[fresh],cache:{cars:[old],after:1,total:1}}),
    scan:async()=>data({status:'completed',removed:[{id:'1',checked_at:old.checked_at}],cache:{cars:[old],after:1,total:1}})});
  await s.window.AutoDealLive.search({onlyDeals:true});await s.tick();
  assert.equal(s.ids.resultsList.children.length,1);
  assert.equal(s.ids.resultsList.children[0].children[0].children[1].textContent,'$8,500');
});
