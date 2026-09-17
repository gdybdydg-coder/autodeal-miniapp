const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(api={}) {
  const nodes=[],html=fs.readFileSync(__dirname+'/index.html','utf8');
  function element(tag='div') {
    const classes=new Set();
    const n={tag,value:'',checked:false,style:{},validity:{badInput:false},children:[],listeners:{},attributes:{},textContent:'',
      classList:{add:c=>classes.add(c),contains:c=>classes.has(c),remove:c=>classes.delete(c),
        toggle(c,on){if(on??!classes.has(c))classes.add(c);else classes.delete(c)}},
      add(o){this.children.push(o)},append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},
      addEventListener(t,f){this.listeners[t]=f},setAttribute(k,v){this.attributes[k]=v},
      dispatchEvent(e){this.listeners[e.type]?.(e)},scrollIntoView(){this.scrolled=true},focus(){this.focused=true},
      querySelector(){return this.switchNode ||= element()},showModal(){this.open=true},
      close(){this.open=false;this.listeners.close?.()},reportValidity(){return true}};
    nodes.push(n);return n;
  }
  const ids=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],element()]));
  const nav=Object.fromEntries(['search','deals','saved','settings'].map(tab=>{const n=element('button');n.dataset={tab};return [tab,n]}));
  nav.search.classList.add('active');
  let localRaw=null,searchBusy=false;
  const searches=[],cloud={list:async()=>[],save:async()=>{},remove:async()=>{},...api};
  const window={AutoDealCloud:cloud,AutoDealLive:{isBusy:()=>searchBusy,dismissAutoScroll(){},search:async f=>{searches.push(f)}},
    addEventListener(){},localStorage:{getItem:()=>localRaw,setItem:(k,v)=>{localRaw=v}}};
  const document={getElementById:id=>{assert.ok(ids[id],id);return ids[id]},createElement:element,
    querySelectorAll(s){if(s==='.nav')return Object.values(nav);const m=s.match(/^input\[name="([^"]+)"\](:checked)?$/);assert.ok(m,s);
      return nodes.filter(n=>n.tag==='input'&&n.name===m[1]&&(!m[2]||n.checked))}};
  const context=vm.createContext({window,document,Option:function(text,value){return{text,value}},
    Event:function(type){this.type=type},setTimeout:()=>0,clearTimeout(){}});
  for(const f of ['data/cars.js','saved-searches.js','app.js','search-manager.js','cloud-searches.js'])
    vm.runInContext(fs.readFileSync(__dirname+'/'+f,'utf8'),context);
  ids.brand.value='Volkswagen';ids.model.value='Golf';ids.region.value='Хмельницька область';
  ids.priceTo.value='20000';ids.mileageTo.value='180';
  nodes.find(n=>n.name==='fuel'&&n.value==='Дизель').checked=true;
  const run=s=>vm.runInContext(s,context);
  const filters=()=>run('readCurrentFilters()');
  return {ids,nav,run,filters,window,searches,setBusy:v=>{searchBusy=v},local:()=>localRaw};
}

test('opening My searches reads account once, keeps local cards and restores navigation on close',async()=>{
  let lists=0,resolve;
  const ui=setup({list:()=>{lists++;return new Promise(r=>{resolve=r})},save:()=>assert.fail('unexpected save')});
  ui.run('savedAPI.save(window.localStorage,readCurrentFilters(),"Локальний",false)');
  ui.run('setSearchTab("deals")');
  const pending=ui.nav.saved.listeners.click();ui.nav.saved.listeners.click();
  assert.equal(lists,1);assert.equal(ui.ids.savedDialog.hidden,false);
  assert.equal(ui.ids.searchFilters.hidden,true);assert.equal(ui.ids.results.hidden,true);
  assert.equal(ui.nav.saved.attributes['aria-current'],'page');
  assert.equal(ui.ids.localTitle.textContent,'На цьому пристрої (1)');
  assert.equal(ui.ids.savedSearchList.children.length,1);
  resolve([{id:7,name:'Мій Golf',filters:ui.filters(),enabled:false}]);await pending;
  assert.equal(ui.ids.cloudTitle.textContent,'В акаунті Telegram (1)');
  assert.equal(ui.ids.cloudSearchList.children[0].children[0].textContent,'Мій Golf');
  assert.equal(ui.searches.length,0);
  ui.ids.closeSaved.listeners.click();assert.equal(ui.nav.deals.attributes['aria-current'],'page');
});

test('account failure does not erase device searches and refresh recovers',async()=>{
  let fail=true;
  const ui=setup({list:async()=>{if(fail)throw Error('Сервер не відповідає');return[]}});
  ui.run('savedAPI.save(window.localStorage,readCurrentFilters(),"На телефоні",false)');
  const before=ui.local();await ui.nav.saved.listeners.click();
  assert.match(ui.ids.cloudStatus.textContent,/не відповідає/);
  assert.equal(ui.ids.savedSearchList.children[0].children[0].textContent,'На телефоні');
  assert.equal(ui.local(),before);assert.equal(ui.ids.refreshCloud.disabled,false);
  fail=false;await ui.ids.refreshCloud.listeners.click();
  assert.match(ui.ids.cloudStatus.textContent,/ще немає/);
});

test('saved card restores full criteria, waits for active search and starts one explicit search',async()=>{
  let stored;
  const ui=setup({list:async()=>[{id:9,name:'Golf',filters:stored,enabled:false}]});stored=ui.filters();
  ui.ids.brand.value='BMW';ui.ids.model.value='X5';
  await ui.nav.saved.listeners.click();
  const open=ui.ids.cloudSearchList.children[0].children[3].children[0];
  ui.setBusy(true);await open.listeners.click();
  assert.equal(ui.ids.savedDialog.hidden,false);assert.match(ui.ids.savedError.textContent,/ще виконується/);
  assert.equal(ui.ids.brand.value,'BMW');assert.equal(ui.searches.length,0);
  ui.setBusy(false);await open.listeners.click();
  assert.equal(ui.ids.savedDialog.hidden,true);assert.equal(ui.searches.length,1);
  assert.equal(ui.searches[0].brand,'Volkswagen');assert.equal(ui.searches[0].model,'Golf');
  assert.equal(ui.searches[0].region,'Хмельницька область');assert.equal(ui.searches[0].price.to,20000);
  assert.equal(ui.searches[0].mileage.to,180);assert.deepEqual(Array.from(ui.searches[0].fuel),['Дизель']);
  assert.equal(ui.nav.search.attributes['aria-current'],'page');
});

test('new search goes to filters without sending or deleting anything',async()=>{
  let writes=0;const ui=setup({save:async()=>writes++,remove:async()=>writes++});
  await ui.nav.saved.listeners.click();ui.ids.newSavedSearch.listeners.click();
  assert.equal(ui.ids.savedDialog.hidden,true);assert.equal(ui.nav.search.attributes['aria-current'],'page');
  assert.equal(ui.ids.searchFilters.hidden,false);assert.equal(ui.ids.searchTitle.focused,true);
  assert.equal(writes,0);assert.equal(ui.searches.length,0);
});

test('settings tab and header gear open a separate screen without loading searches',async()=>{
  let lists=0,statusReads=0;
  const ui=setup({list:async()=>{lists++;return[]},notificationStatus:async()=>{
    statusReads++;return{available:false,telegram_ready:false,test_sent:false};}});
  await ui.nav.settings.listeners.click();
  assert.equal(ui.ids.settingsPage.hidden,false);assert.equal(ui.ids.savedDialog.hidden,true);
  assert.equal(ui.ids.searchFilters.hidden,true);assert.equal(ui.ids.results.hidden,true);
  assert.equal(ui.nav.settings.attributes['aria-current'],'page');
  assert.equal(lists,0);assert.equal(statusReads,1);
  ui.nav.search.listeners.click();
  await ui.ids.settingsBtn.listeners.click();
  assert.equal(ui.ids.settingsPage.hidden,false);assert.equal(lists,0);
  await ui.ids.settingsSearches.listeners.click();
  assert.equal(ui.ids.settingsPage.hidden,true);assert.equal(ui.ids.savedDialog.hidden,false);
  ui.ids.closeSaved.listeners.click();assert.equal(ui.ids.settingsPage.hidden,false);
});

test('late completion of saving never hides a new draft',async()=>{
  let done;
  const ui=setup({save:()=>new Promise(resolve=>{done=resolve})});
  ui.ids.saveSearchBtn.listeners.click();ui.ids.savedName.value='Перший';
  ui.ids.saveCloudSearch.listeners.click();
  ui.ids.closeSaved.listeners.click();
  ui.ids.saveSearchBtn.listeners.click();ui.ids.savedName.value='Новий чернетковий пошук';
  done();await tick();
  assert.equal(ui.ids.saveSearchForm.hidden,false);
  assert.equal(ui.ids.savedName.value,'Новий чернетковий пошук');
});

test('notification controls require readiness and send only on an explicit tap',async()=>{
  let state={available:true,telegram_ready:false,test_sent:false},enabled=false,tests=0;
  const updates=[];
  const ui=setup({list:async()=>[{id:8,name:'Golf',filters:ui.filters(),enabled,monitor_status:'watching'}],
    notificationStatus:async()=>state,testNotification:async()=>{tests++;state={...state,test_sent:true};return{state:'sent'}},
    enable:async(id,value)=>{updates.push([id,value]);enabled=value}});
  await ui.nav.saved.listeners.click();
  const toggle=()=>ui.ids.cloudSearchList.children[0].children[3].children[2];
  assert.equal(ui.ids.testNotification.disabled,true);assert.equal(toggle().disabled,true);
  assert.equal(tests,0);assert.deepEqual(updates,[]);
  state={...state,telegram_ready:true};await ui.ids.refreshCloud.listeners.click();
  assert.equal(ui.ids.testNotification.disabled,false);assert.equal(toggle().disabled,true);
  await ui.ids.testNotification.listeners.click();
  assert.equal(tests,1);assert.equal(toggle().disabled,false);
  await toggle().listeners.click();
  assert.deepEqual(updates,[[8,true]]);assert.match(toggle().textContent,/Вимкнути/);
  // A source failure must not prevent the owner from revoking consent.
  state={...state,available:false};await ui.ids.refreshCloud.listeners.click();
  assert.equal(toggle().disabled,false);
  await toggle().listeners.click();assert.deepEqual(updates,[[8,true],[8,false]]);
});
