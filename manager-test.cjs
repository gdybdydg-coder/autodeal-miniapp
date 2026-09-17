const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const part=(node,className)=>node.className===className?node:node.children?.map(n=>part(n,className)).find(Boolean);
function setup(api={},mode='legacy') {
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
  const document={documentElement:{dataset:{mode}},getElementById:id=>{assert.ok(ids[id],id);return ids[id]},createElement:element,
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
  assert.equal(ui.ids.cloudCount.textContent,'1');assert.equal(ui.ids.localCount.textContent,'1');
  assert.equal(part(ui.ids.cloudSearchList.children[0],'subscription-name').textContent,'Мій Golf');
  assert.equal(ui.searches.length,0);
  ui.ids.closeSaved.listeners.click();assert.equal(ui.nav.deals.attributes['aria-current'],'page');
});

test('account failure does not erase device searches and refresh recovers',async()=>{
  let fail=true;
  const ui=setup({list:async()=>{if(fail)throw Error('Сервер не відповідає');return[]}});
  ui.run('savedAPI.save(window.localStorage,readCurrentFilters(),"На телефоні",false)');
  const before=ui.local();await ui.nav.saved.listeners.click();
  assert.match(ui.ids.cloudStatus.textContent,/не відповідає/);
  assert.equal(part(ui.ids.savedSearchList.children[0],'subscription-name').textContent,'На телефоні');
  assert.equal(ui.local(),before);assert.equal(ui.ids.refreshCloud.disabled,false);
  fail=false;await ui.ids.refreshCloud.listeners.click();
  assert.match(ui.ids.cloudStatus.textContent,/ще немає/);
});

test('saved card restores full criteria, waits for active search and starts one explicit search',async()=>{
  let stored;
  const ui=setup({list:async()=>[{id:9,name:'Golf',filters:stored,enabled:false}]});stored=ui.filters();
  ui.ids.brand.value='BMW';ui.ids.model.value='X5';
  await ui.nav.saved.listeners.click();
  const open=part(ui.ids.cloudSearchList.children[0],'subscription-open');
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

test('closing saved searches restores the separate results screen without a new search',async()=>{
  const ui=setup();
  await ui.ids.searchBtn.listeners.click();
  assert.equal(ui.ids.searchFilters.hidden,true);assert.equal(ui.ids.results.hidden,false);
  await ui.nav.saved.listeners.click();ui.ids.closeSaved.listeners.click();
  assert.equal(ui.ids.searchFilters.hidden,true);assert.equal(ui.ids.results.hidden,false);
  assert.equal(ui.nav.search.attributes['aria-current'],'page');assert.equal(ui.searches.length,1);
  ui.nav.search.listeners.click();
  assert.equal(ui.ids.results.hidden,true);assert.equal(ui.ids.searchFilters.hidden,false);
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
  const toggle=()=>{
    part(ui.ids.cloudSearchList.children[0],'subscription-more').listeners.click();
    return ui.ids.subscriptionMenuActions.children.find(n=>/сповіщення/.test(n.textContent));
  };
  assert.equal(ui.ids.testNotification.disabled,true);assert.equal(toggle().disabled,true);
  assert.equal(tests,0);assert.deepEqual(updates,[]);
  state={...state,telegram_ready:true};await ui.ids.refreshCloud.listeners.click();
  assert.equal(ui.ids.testNotification.disabled,false);assert.equal(toggle().disabled,true);
  await ui.ids.testNotification.listeners.click();
  await ui.ids.refreshCloud.listeners.click();
  assert.equal(tests,1);assert.equal(toggle().disabled,false);
  await toggle().listeners.click();
  assert.deepEqual(updates,[[8,true]]);assert.match(toggle().textContent,/вимкнути/i);
  // A source failure must not prevent the owner from revoking consent.
  state={...state,available:false};await ui.ids.refreshCloud.listeners.click();
  assert.equal(toggle().disabled,false);
  await toggle().listeners.click();assert.deepEqual(updates,[[8,true],[8,false]]);
});

test('Telegram-only test stays separate from subscriptions and sends only after a tap',async()=>{
  let state={available:false,test_available:true,telegram_ready:false,test_sent:false},tests=0,lists=0;
  const ui=setup({notificationStatus:async()=>state,list:async()=>{lists++;return[]},
    testNotification:async()=>{tests++;state={...state,test_sent:true};return{state:'sent'}},
    enable:async()=>assert.fail('test must not enable a search')});
  await ui.nav.settings.listeners.click();
  assert.equal(ui.ids.testNotification.disabled,true);assert.equal(tests,0);
  assert.match(ui.ids.notificationStatus.textContent,/Підключи чат/);
  state={...state,telegram_ready:true};await ui.ids.refreshSettings.listeners.click();
  assert.equal(ui.ids.testNotification.disabled,false);assert.equal(tests,0);
  await ui.ids.testNotification.listeners.click();
  assert.equal(tests,1);assert.equal(lists,0);
  assert.equal(ui.ids.settingsPage.hidden,false);
  assert.match(ui.ids.notificationStatus.textContent,/моніторинг ще не ввімкнено/);
  assert.match(ui.ids.settingsStatus.textContent,/перевір отримання/);
  state={...state,test_available:false,available:true};await ui.ids.refreshSettings.listeners.click();
  assert.equal(ui.ids.testNotification.disabled,true);
});

test('subscription tabs switch independent lists without searching or reloading the account',async()=>{
  let lists=0;
  const ui=setup({list:async()=>{lists++;return[{id:9,name:'Golf',filters:ui.filters(),enabled:false}]}});
  ui.run('savedAPI.save(window.localStorage,readCurrentFilters(),"На телефоні",false)');
  await ui.nav.saved.listeners.click();
  assert.equal(ui.ids.cloudPanel.hidden,false);assert.equal(ui.ids.localPanel.hidden,true);
  ui.ids.localTab.listeners.click();
  assert.equal(ui.ids.cloudPanel.hidden,true);assert.equal(ui.ids.localPanel.hidden,false);
  assert.equal(ui.ids.localTab.attributes['aria-selected'],'true');assert.equal(ui.ids.refreshCloud.hidden,true);
  ui.ids.localTab.listeners.keydown({key:'ArrowRight',preventDefault(){}});
  assert.equal(ui.ids.cloudPanel.hidden,false);assert.equal(ui.ids.localPanel.hidden,true);
  assert.equal(ui.ids.subscriptionsTab.focused,true);assert.equal(lists,1);assert.equal(ui.searches.length,0);
});

test('menu and delete confirmation do not open search; cancel preserves data and server error stays visible',async()=>{
  let fail=true;const removed=[];
  const ui=setup({list:async()=>removed.length?[]:[{id:17,name:'<b>Мій Golf</b>',filters:ui.filters(),enabled:false}],
    remove:async id=>{if(fail)throw Error('Не вдалося видалити');removed.push(id)}});
  await ui.nav.saved.listeners.click();
  part(ui.ids.cloudSearchList.children[0],'subscription-more').listeners.click();
  assert.equal(ui.ids.subscriptionMenu.open,true);assert.equal(ui.searches.length,0);
  assert.equal(ui.ids.subscriptionMenuTitle.textContent,'<b>Мій Golf</b>');
  const action=label=>ui.ids.subscriptionMenuActions.children.find(n=>n.textContent===label);
  action('Видалити підписку').listeners.click();assert.deepEqual(removed,[]);
  action('Скасувати').listeners.click();assert.deepEqual(removed,[]);
  action('Видалити підписку').listeners.click();await action('Так, видалити').listeners.click();
  assert.equal(ui.ids.subscriptionMenu.open,true);assert.equal(ui.ids.subscriptionMenuError.hidden,false);
  assert.match(ui.ids.subscriptionMenuError.textContent,/Не вдалося/);
  fail=false;await action('Так, видалити').listeners.click();
  assert.deepEqual(removed,[17]);assert.equal(ui.ids.subscriptionMenu.open,false);assert.equal(ui.ids.cloudCount.textContent,'0');
});

test('device save returns to device list and deletion remains local',async()=>{
  let network=0;
  const ui=setup({list:async()=>{network++;return[]},remove:async()=>{network++}});
  ui.ids.saveSearchBtn.listeners.click();ui.ids.savedName.value='Локальний Golf';
  assert.equal(ui.ids.subscriptionTabs.hidden,true);assert.equal(ui.ids.newSavedSearch.hidden,true);
  ui.ids.saveSearchForm.listeners.submit({preventDefault(){}});
  assert.equal(ui.ids.saveSearchForm.hidden,true);assert.equal(ui.ids.subscriptionTabs.hidden,false);
  assert.equal(ui.ids.localPanel.hidden,false);assert.equal(ui.ids.newSavedSearch.hidden,false);
  assert.equal(ui.ids.localCount.textContent,'1');
  part(ui.ids.savedSearchList.children[0],'subscription-more').listeners.click();
  assert.equal(ui.ids.openNotificationSettings.hidden,true);
  ui.ids.subscriptionMenuActions.children.find(n=>n.textContent==='Видалити пошук').listeners.click();
  ui.ids.subscriptionMenuActions.children.find(n=>n.textContent==='Так, видалити').listeners.click();
  assert.equal(ui.ids.localCount.textContent,'0');assert.equal(network,0);
});

test('subscription edit restores all filters and updates the same id without a scan or a duplicate',async()=>{
  let stored;const updates=[];
  const ui=setup({list:async()=>[stored],save:async()=>assert.fail('edit must not create'),
    update:async(id,name,filters)=>{updates.push({id,name,filters});stored={id,name,filters,enabled:false};return stored;},
    notificationStatus:async()=>({available:false,telegram_ready:true,test_sent:false})},'subscriptions');
  stored={id:27,name:'Мій Golf',filters:ui.filters(),enabled:false};
  ui.ids.brand.value='BMW';ui.ids.model.value='X5';
  await ui.nav.saved.listeners.click();
  assert.match(part(ui.ids.cloudSearchList.children[0],'subscription-state').textContent,/На паузі/);
  await part(ui.ids.cloudSearchList.children[0],'subscription-open').listeners.click();
  assert.equal(ui.ids.searchTitle.textContent,'Редагувати підписку');
  assert.equal(ui.ids.cancelSubscriptionEdit.hidden,false);
  assert.equal(ui.ids.model.value,'Golf');assert.equal(ui.filters().mileage.to,180);
  assert.equal(ui.filters().region,'Хмельницька область');assert.deepEqual(Array.from(ui.filters().fuel),['Дизель']);
  ui.ids.priceTo.value='15000';ui.ids.saveSearchBtn.listeners.click();
  assert.equal(ui.ids.savedName.value,'Мій Golf');assert.equal(ui.ids.confirmSaveSearch.hidden,true);
  ui.ids.savedName.value='Новий Golf';await ui.ids.saveCloudSearch.listeners.click();
  assert.equal(updates.length,1);assert.equal(updates[0].id,27);assert.equal(updates[0].filters.price.to,15000);
  assert.equal(updates[0].filters.onlyDeals,true);assert.equal(stored.name,'Новий Golf');
  assert.equal(ui.ids.cloudCount.textContent,'1');assert.equal(ui.searches.length,0);
  assert.equal(ui.ids.saveSearchForm.hidden,true);assert.equal(ui.ids.cancelSubscriptionEdit.hidden,true);
});

test('cancelled edit performs no write and a new subscription starts with clear filters',async()=>{
  let stored;const ui=setup({list:async()=>[stored],update:async()=>assert.fail('cancelled'),save:async()=>assert.fail('cancelled')},'subscriptions');
  stored={id:9,name:'Original',filters:ui.filters(),enabled:false};
  await ui.nav.saved.listeners.click();await part(ui.ids.cloudSearchList.children[0],'subscription-open').listeners.click();
  ui.ids.priceTo.value='1';await ui.ids.cancelSubscriptionEdit.listeners.click();
  assert.equal(stored.filters.price.to,20000);assert.equal(ui.ids.savedDialog.hidden,false);
  ui.ids.newSavedSearch.listeners.click();
  assert.equal(ui.ids.saveSearchBtn.textContent,'Створити підписку');assert.equal(ui.ids.brand.value,'');
  assert.equal(ui.filters().price.to,null);assert.deepEqual(Array.from(ui.filters().fuel),[]);
  assert.equal(ui.filters().onlyDeals,true);assert.equal(ui.searches.length,0);
});

test('edit failure leaves its draft and actionable error visible and retry updates the same id',async()=>{
  let stored,fail=true;const calls=[];
  const ui=setup({list:async()=>[stored],update:async(id,name,filters)=>{
    calls.push(id);if(fail)throw Error('Підписка з такими фільтрами вже є');stored={id,name,filters,enabled:false};return stored;
  }},'subscriptions');stored={id:4,name:'Golf',filters:ui.filters(),enabled:false};
  await ui.nav.saved.listeners.click();await part(ui.ids.cloudSearchList.children[0],'subscription-open').listeners.click();
  ui.ids.saveSearchBtn.listeners.click();ui.ids.savedName.value='Зміни';
  await ui.ids.saveCloudSearch.listeners.click();
  assert.equal(ui.ids.saveSearchForm.hidden,false);assert.equal(ui.ids.saveStatus.hidden,false);
  assert.match(ui.ids.saveStatus.textContent,/вже є/);assert.equal(stored.name,'Golf');
  assert.equal(ui.ids.savedName.value,'Зміни');assert.equal(ui.ids.saveCloudSearch.disabled,false);
  fail=false;await ui.ids.saveCloudSearch.listeners.click();
  assert.deepEqual(calls,[4,4]);assert.equal(stored.name,'Зміни');assert.equal(ui.ids.cloudCount.textContent,'1');
});

test('late dictionary response cannot pull the user out of settings into an editor',async()=>{
  let resolve;const ui=setup({list:async()=>[{id:8,name:'Golf',filters:ui.filters(),enabled:false}],
    notificationStatus:async()=>({available:false})},'subscriptions');
  await ui.nav.saved.listeners.click();
  ui.window.AutoDealCatalog={ensureFilters:()=>new Promise(r=>{resolve=r})};
  const pending=part(ui.ids.cloudSearchList.children[0],'subscription-open').listeners.click();
  await ui.nav.settings.listeners.click();resolve();await pending;
  assert.equal(ui.ids.settingsPage.hidden,false);assert.equal(ui.ids.searchFilters.hidden,true);
  assert.equal(ui.run('editingSearch'),null);
});

test('device drafts edit in place without writing to the account',async()=>{
  const ui=setup({save:async()=>assert.fail('local only'),update:async()=>assert.fail('local only')},'subscriptions');
  ui.run('savedAPI.save(window.localStorage,readCurrentFilters(),"На телефоні",false)');
  const first=JSON.parse(ui.local()).searches[0];
  await ui.nav.saved.listeners.click();ui.ids.localTab.listeners.click();
  await part(ui.ids.savedSearchList.children[0],'subscription-open').listeners.click();
  ui.ids.priceTo.value='17000';ui.ids.saveSearchBtn.listeners.click();
  assert.equal(ui.ids.saveCloudSearch.hidden,true);ui.ids.savedName.value='Оновлено';
  ui.ids.saveSearchForm.listeners.submit({preventDefault(){}});
  const items=JSON.parse(ui.local()).searches;
  assert.equal(items.length,1);assert.equal(items[0].id,first.id);assert.equal(items[0].filters.price.to,17000);
  assert.equal(items[0].name,'Оновлено');assert.equal(ui.ids.localPanel.hidden,false);
});

test('changing filters from the review form preserves the typed subscription name',()=>{
  const ui=setup({},'subscriptions');
  ui.ids.saveSearchBtn.listeners.click();ui.ids.savedName.value='Для роботи';
  ui.ids.editDraftFilters.listeners.click();ui.ids.priceTo.value='18000';
  ui.ids.saveSearchBtn.listeners.click();
  assert.equal(ui.ids.savedName.value,'Для роботи');assert.match(ui.ids.draftSummary.textContent,/18000/);
  assert.equal(ui.searches.length,0);
});
