const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
function setup(search) {
  function node(){return {children:[],textContent:'',classList:{add(){}},append(...items){this.children.push(...items)},
    replaceChildren(...items){this.children=items},scrollIntoView(){}};}
  const ids=Object.fromEntries(['resultsList','count','sourceNote','results','searchBtn','resultsTitle','resultsCriteria'].map(id=>[id,node()]));
  ids.searchBtn.textContent='Шукати';
  const window={AutoDealCloud:{search}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/live-search.js','utf8'),{window,URL,
    document:{createElement:node,getElementById:id=>ids[id]}});
  return {window,ids};
}
test('live results use safe text/links and never invent valuation',async()=>{
  const {window,ids}=setup(async()=>({cars:[{title:'<img onerror=alert(1)>',price_usd:10000,
    year:2017,mileage:100000,fuel:'Дизель',body:'Седан',transmission:'Автомат',region:'Київ',
    image:'https://evil.test/x',url:'javascript:alert(1)',market:null}],warnings:[],inspected:1,source_total:20}));
  await window.AutoDealLive.search({onlyDeals:false});
  const article=ids.resultsList.children[0],body=article.children[0];
  assert.equal(body.children[0].textContent,'<img onerror=alert(1)>');
  assert.ok(body.children.some(n=>/Недостатньо/.test(n.textContent)));
  assert.equal(article.children.length,1);
  assert.equal(body.children.filter(n=>n.href).length,0);
  assert.equal(ids.searchBtn.disabled,false);
});
test('pending search cannot duplicate requests and clears old results on failure',async()=>{
  let reject,calls=0;
  const {window,ids}=setup(()=>{calls++;return new Promise((a,b)=>{reject=b});});
  ids.resultsList.children=[{textContent:'old listing'}];
  const first=window.AutoDealLive.search({});
  await window.AutoDealLive.search({});
  assert.equal(calls,1);assert.equal(ids.resultsList.children.length,0);
  reject(Error('Ліміт'));await first;
  assert.equal(ids.resultsList.children[0].textContent,'Ліміт');
  assert.equal(ids.searchBtn.disabled,false);
});
test('empty deal subset is not presented as a complete market search',async()=>{
  const {window,ids}=setup(async()=>({cars:[],warnings:[],inspected:3,source_total:250}));
  await window.AutoDealLive.search({onlyDeals:true});
  assert.match(ids.resultsList.children[0].textContent,/не означає/);
  assert.match(ids.sourceNote.textContent,/до 3/);
});
test('historical results show observation time and suppress old valuation',async()=>{
  const {window,ids}=setup(async()=>({cars:[{title:'Golf',price_usd:10000,year:2017,mileage:100000,
    fuel:'Дизель',body:'Хетчбек',transmission:'Автомат',region:'Хмельницька',market:15000,discount:33.3,comparables:5}],
    warnings:['quota_exceeded'],inspected:1,source_total:20,checked_at:1700000000,cached:true,stale:true,
    quota:{retry_after_seconds:600}}));
  await window.AutoDealLive.search({onlyDeals:false});
  assert.match(ids.sourceNote.textContent,/Раніше отримані дані/);
  assert.match(ids.sourceNote.textContent,/2023/);
  assert.match(ids.sourceNote.textContent,/10 хв/);
  const contents=ids.resultsList.children[0].children[0].children.map(n=>n.textContent).join(' ');
  assert.match(contents,/потребує оновлення/);
  assert.doesNotMatch(contents,/33.3|15,000/);
});
