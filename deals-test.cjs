const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
function setup(fetchSearch,mode='legacy') {
  const html=fs.readFileSync(__dirname+'/index.html','utf8'),nodes=[];
  function element(tag='div') {
    const classes=new Set();
    const node={tag,value:'',checked:false,style:{},validity:{badInput:false},children:[],listeners:{},attributes:{},textContent:'',
      classList:{add(c){classes.add(c)},remove(c){classes.delete(c)},contains:c=>classes.has(c),
        toggle(c,on){if(on??!classes.has(c))classes.add(c);else classes.delete(c)}},
      add(o){this.children.push(o)},append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},
      addEventListener(t,f){this.listeners[t]=f},setAttribute(k,v){this.attributes[k]=v},
      scrollIntoView(){this.scrolls=(this.scrolls||0)+1},focus(){this.focused=true},
      querySelector(){return this.switchNode ||= element()}};
    nodes.push(node);return node;
  }
  const ids=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],element()]));
  const nav=Object.fromEntries(['search','deals','saved','settings'].map(tab=>{const n=element('button');n.dataset={tab};return [tab,n]}));
  nav.search.classList.add('active');
  const document={documentElement:{dataset:{mode}},getElementById:id=>{assert.ok(ids[id],id);return ids[id]},createElement:element,
    querySelectorAll(selector){
      if(selector==='.nav')return Object.values(nav);
      const m=selector.match(/^input\[name="([^"]+)"\](:checked)?$/);assert.ok(m,selector);
      return nodes.filter(n=>n.tag==='input'&&n.name===m[1]&&(!m[2]||n.checked));
    }};
  const window={AutoDealCloud:{search:fetchSearch},addEventListener(){}};
  const context=vm.createContext({window,document,URL,Option:function(text,value){return{text,value}},setTimeout:()=>0,clearTimeout(){}});
  for(const file of ['data/cars.js','saved-searches.js','app.js','search-manager.js','live-search.js'])
    vm.runInContext(fs.readFileSync(__dirname+'/'+file,'utf8'),context);
  ids.brand.value='Volkswagen';ids.model.value='Golf';ids.region.value='Хмельницька область';
  ids.priceTo.value='20000';ids.mileageTo.value='180';
  nodes.find(n=>n.name==='fuel'&&n.value==='Дизель').checked=true;
  vm.runInContext('onlyDeals=false',context);
  return {context,ids,nav};
}
const result=(cars=[],extra={})=>({cars,warnings:[],inspected:3,source_total:500,...extra});
const car=(price=8500,extra={})=>({title:'Golf',price_usd:price,market:10000,discount:15,comparables:5,
  valuation:'sample_median',year:2017,mileage:100000,fuel:'Дизель',body:'Хетчбек',transmission:'Автомат',
  region:'Хмельницька',url:'https://auto.ria.com/auto_volkswagen_golf_123.html',...extra});

test('subscription mode keeps restored filters and never starts a manual catalog scan',async()=>{
  let calls=0;const {context,ids,nav}=setup(async()=>{calls++;return result()},'subscriptions');
  await ids.searchBtn.listeners.click();await nav.deals.listeners.click();
  assert.equal(calls,0);assert.equal(ids.searchFilters.hidden,false);assert.equal(ids.results.hidden,true);
  assert.equal(ids.brand.value,'Volkswagen');assert.equal(ids.priceTo.value,'20000');
  assert.equal(vm.runInContext('readCurrentFilters().onlyDeals',context),true);
  assert.match(ids.toast.textContent,/Створити підписку/);
});

test('deals navigation uses selected filters, exact threshold and preserves regular search preference',async()=>{
  const calls=[];
  const {context,ids,nav}=setup(async filters=>{calls.push(filters);return result([
    car(8500,{id:'1'}),car(8501,{id:'2'}),car(7000,{id:'3',market:null,valuation:'insufficient_data'}),car(7000,{id:'4',comparables:4})]);});
  await nav.deals.listeners.click();
  assert.equal(calls.length,1);const f=calls[0];
  assert.equal(f.onlyDeals,true);assert.equal(f.brand,'Volkswagen');assert.equal(f.model,'Golf');
  assert.equal(f.region,'Хмельницька область');assert.equal(f.price.to,20000);assert.equal(f.mileage.to,180);
  assert.deepEqual(Array.from(f.fuel),['Дизель']);
  assert.equal(vm.runInContext('onlyDeals',context),false);
  assert.equal(ids.count.textContent,'Показано: 1');assert.equal(ids.resultsTitle.textContent,'Вигідні авто');
  assert.match(ids.resultsCriteria.textContent,/Volkswagen.*Golf.*Хмельницька/);
  assert.equal(nav.deals.attributes['aria-current'],'page');assert.ok(nav.deals.classList.contains('active'));
  assert.equal(ids.searchFilters.hidden,true);assert.equal(ids.searchIntro.hidden,true);
  assert.equal(ids.results.hidden,false);assert.equal(ids.savedDialog.hidden,true);
  assert.equal(ids.results.scrolls,undefined);
  ids.editResultsFilters.listeners.click();
  assert.equal(nav.search.attributes['aria-current'],'page');assert.equal(calls.length,1);
  assert.equal(ids.brand.value,'Volkswagen');assert.equal(ids.searchFilters.hidden,false);
  assert.equal(ids.results.hidden,true);assert.equal(ids.searchTitle.focused,true);
  await ids.searchBtn.listeners.click();
  assert.equal(calls[1].onlyDeals,false);assert.equal(ids.resultsTitle.textContent,'Результати пошуку');
  assert.equal(ids.searchFilters.hidden,true);assert.equal(ids.searchIntro.hidden,true);
  assert.equal(ids.results.hidden,false);assert.equal(ids.results.scrolls,undefined);
  assert.equal(nav.search.attributes['aria-current'],'page');
  ids.editResultsFilters.listeners.click();
  assert.equal(ids.searchFilters.hidden,false);assert.equal(ids.results.hidden,true);
  assert.equal(ids.priceTo.value,'20000');assert.equal(ids.model.value,'Golf');
  assert.equal(calls.length,2);
});

test('rapid taps do not duplicate search and returning to form prevents a late scroll',async()=>{
  let done,calls=0;
  const {ids,nav}=setup(()=>{calls++;return new Promise(resolve=>{done=resolve})});
  const pending=nav.deals.listeners.click();
  nav.deals.listeners.click();assert.equal(calls,1);assert.match(ids.toast.textContent,/ще виконується/);
  nav.search.listeners.click();const scrolls=ids.results.scrolls;
  done(result());await pending;
  assert.equal(ids.results.scrolls,scrolls);assert.equal(nav.search.attributes['aria-current'],'page');
  assert.equal(ids.results.hidden,true);assert.equal(ids.searchFilters.hidden,false);
  assert.equal(ids.searchBtn.disabled,false);
});

test('invalid filters do not navigate or call API',async()=>{
  let calls=0;const {ids,nav}=setup(async()=>{calls++;return result()});
  ids.priceFrom.value='30000';
  await nav.deals.listeners.click();assert.equal(calls,0);
  assert.match(ids.toast.textContent,/Від/);assert.ok(nav.search.classList.contains('active'));
});

test('ordinary results stay off the filter form, including after a late response and tab switches',async()=>{
  let done,calls=0;
  const {ids,nav}=setup(()=>{calls++;return new Promise(resolve=>{done=resolve})});
  assert.equal(ids.results.hidden,true);
  const pending=ids.searchBtn.listeners.click();
  assert.equal(ids.searchFilters.hidden,true);assert.equal(ids.results.hidden,false);
  ids.editResultsFilters.listeners.click();
  assert.equal(ids.searchFilters.hidden,false);assert.equal(ids.results.hidden,true);
  done(result([car()]));await pending;
  assert.equal(ids.results.hidden,true);assert.equal(ids.results.scrolls,undefined);
  await nav.settings.listeners.click();await nav.search.listeners.click();
  assert.equal(ids.searchFilters.hidden,false);assert.equal(ids.results.hidden,true);assert.equal(calls,1);
});

test('deals handles stale data, quota errors and recovery without showing ordinary cars',async()=>{
  let fail=false;
  const {ids,nav}=setup(async()=>{if(fail)throw Error('Ліміт запитів AUTO.RIA');return result([car()],{stale:true});});
  await nav.deals.listeners.click();
  assert.equal(ids.count.textContent,'Показано: 0');assert.match(ids.resultsList.children[0].textContent,/свіжа перевірка/);
  fail=true;await nav.deals.listeners.click();
  assert.equal(ids.resultsTitle.textContent,'Вигідні авто');assert.match(ids.resultsList.children[0].textContent,/Ліміт/);
  assert.equal(ids.searchBtn.disabled,false);
  fail=false;await nav.deals.listeners.click();assert.equal(ids.count.textContent,'Показано: 0');
});
