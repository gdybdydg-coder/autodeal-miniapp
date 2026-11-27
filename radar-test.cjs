const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const Radar=require('./radar-ui.js'),{create}=require('./cloud-api.js');
const rid=n=>n.toString(16).padStart(32,'0');
const catalog={brands:[{id:9,name:'BMW'},{id:84,name:'Volkswagen'}],regions:[{id:1,name:'Вінницька область'},{id:10,name:'Київська область'}],year_min:1900,year_max:2027,
 modelsByBrand:{9:[{id:90,brand_id:9,name:'X5'},{id:91,brand_id:9,name:'3 Series'}],84:[{id:840,brand_id:84,name:'Golf'}]}};
const draft=()=>({name:'Мій радар',brands:[{brand_id:9,model_ids:[90]}],price_category:3,region_ids:[],year_from:2005,year_to:2026,min_profit:10,fuel_ids:[]});
const item=(slot=1)=>({...draft(),id:rid(slot),slot,status:'paused',revision:1,brands:[{brand_id:9,brand_name:'BMW',model_ids:[90],models:[{model_id:90,name:'X5'}]}],regions:[]});

test('new radar starts independent, with no arbitrary price category or selected brand',()=>{
 const a=Radar.draftFrom(null,1,2026),b=Radar.draftFrom(null,2,2026);assert.equal(a.name,'Радар 1');assert.equal(a.all_brands,false);assert.equal(a.price_category,null);assert.deepEqual(a.brands,[]);a.region_ids.push(1);assert.deepEqual(b.region_ids,[]);assert.ok(Radar.validate(a,catalog).length);
});

test('all brands is an explicit validated wildcard, never an accidental empty selection',()=>{
 const all={...draft(),all_brands:true,brands:[]};assert.deepEqual(Radar.validate(all,catalog),[]);
 assert.deepEqual(Radar.canonical(all).brands,[]);assert.equal(Radar.canonical(all).all_brands,true);
 for(const bad of [{...draft(),all_brands:true},{...all,all_brands:false},{...all,all_brands:undefined},{...all,all_brands:'true'},{...all,all_brands:1}])assert.ok(Radar.validate(bad,catalog).length);
 assert.equal(Radar.draftFrom(item()).all_brands,false);assert.equal(Radar.canonical(draft()).all_brands,false);
});

test('switching to all clears individual brands/models and selecting a brand exits all without duplicates',()=>{
 const d=draft();Radar.toggleBrand(d,84,true);Radar.toggleModel(d,84,840,true);Radar.selectAllBrands(d,true);
 assert.equal(d.all_brands,true);assert.deepEqual(d.brands,[]);assert.deepEqual(Radar.validate(d,catalog),[]);
 Radar.toggleBrand(d,9,true);Radar.toggleBrand(d,9,true);assert.equal(d.all_brands,false);assert.deepEqual(d.brands,[{brand_id:9,model_ids:[]}]);
 Radar.toggleModel(d,9,90,true);Radar.toggleModel(d,9,90,true);assert.deepEqual(d.brands[0].model_ids,[90]);
 Radar.selectAllBrands(d,true);Radar.selectAllBrands(d,false);assert.equal(d.all_brands,false);assert.deepEqual(d.brands,[]);
 assert.match(Radar.validate(d,catalog).join(' '),/хоча б одну марку/);
});

test('all-brands create, edit and reopen preserve wildcard and every other filter',async()=>{
 let saved,captured;const all={...draft(),all_brands:true,brands:[],region_ids:[1,10]};
 const store=Radar.createStore({radars:async()=>({radars:saved?[saved]:[],limit:3,access:{active:false}}),
  createRadar:async payload=>{captured=payload;return saved={...payload,id:rid(1),slot:1,status:'paused',revision:1};},
  updateRadar:async(id,payload)=>saved={...payload,id,slot:1,status:'paused',revision:2}});
 await store.load();await store.save(all,null,'all-brands-create-1');assert.equal(captured.all_brands,true);assert.deepEqual(captured.brands,[]);
 await store.load();const reopened=Radar.draftFrom(store.snapshot.radars[0]);assert.deepEqual(reopened,all);
 reopened.min_profit=20;await store.save(reopened,store.snapshot.radars[0]);await store.load();
 assert.equal(store.snapshot.radars[0].all_brands,true);assert.equal(store.snapshot.radars[0].min_profit,20);assert.deepEqual(store.snapshot.radars[0].brands,[]);
 assert.deepEqual(store.snapshot.radars[0].region_ids,[1,10]);assert.equal(store.snapshot.radars[0].price_category,3);
});
test('all six price boundaries belong to exactly one category',()=>{
 for(const [price,category] of [[0,1],[1999.99,1],[2000,2],[4999.99,2],[5000,3],[9999.99,3],[10000,4],[14999.99,4],[15000,5],[19999.99,5],[20000,6],[1000000000,6]]){
  assert.deepEqual(Radar.PRICES.filter(p=>Radar.priceMatches(p.id,price)).map(p=>p.id),[category]);
 }
 for(const price of [-1,NaN,Infinity])assert.equal(Radar.PRICES.filter(p=>Radar.priceMatches(p.id,price)).length,0);
});
test('multi-brand model selections preserve associations and cannot duplicate',()=>{
 const d=draft();Radar.toggleBrand(d,84,true);Radar.toggleBrand(d,84,true);Radar.toggleModel(d,84,840,true);Radar.toggleModel(d,84,840,true);assert.equal(d.brands.length,2);assert.deepEqual(d.brands[1].model_ids,[840]);assert.deepEqual(Radar.validate(d,catalog),[]);
 Radar.toggleModel(d,9,840,true);assert.match(Radar.validate(d,catalog).join(' '),/відповідність марці/);Radar.toggleBrand(d,9,false);assert.deepEqual(d.brands,[{brand_id:84,model_ids:[840]}]);
});
test('all models is represented by an empty per-brand list and brands remain independent',()=>{
 const d=draft();Radar.toggleBrand(d,84,true);Radar.toggleModel(d,9,90,false);assert.deepEqual(d.brands[0].model_ids,[]);assert.deepEqual(Radar.validate(d,catalog),[]);Radar.toggleModel(d,84,840,true);assert.deepEqual(d.brands[0].model_ids,[]);
});
test('multiple oblasts and entire Ukraine switch without duplicate region IDs',()=>{
 const d=draft();Radar.toggleRegion(d,1,true);Radar.toggleRegion(d,10,true);Radar.toggleRegion(d,1,true);assert.deepEqual(d.region_ids,[1,10]);assert.deepEqual(Radar.validate(d,catalog),[]);d.region_ids=[];assert.deepEqual(Radar.validate(d,catalog),[]);Radar.toggleRegion(d,10,true);assert.deepEqual(d.region_ids,[10]);Radar.toggleRegion(d,10,false);assert.deepEqual(d.region_ids,[]);
});
test('unknown brands, mismatched models, unknown regions, and duplicate configurations are invalid',()=>{
 for(const change of [d=>d.brands[0].brand_id=999,d=>d.brands[0].model_ids=[840],d=>d.region_ids=[999],d=>d.region_ids=[1,1],d=>d.brands.push({...d.brands[0]}),d=>d.brands[0].model_ids=[90,90]]){const d=draft();change(d);assert.ok(Radar.validate(d,catalog).length);}
});
test('year range validation rejects reversed, future out-of-range, fractional and string years',()=>{
 for(const [from,to] of [[2026,2000],[1800,2000],[2020,2028],[2000.5,2020],['2000',2020]]){const d=draft();d.year_from=from;d.year_to=to;assert.match(Radar.validate(d,catalog).join(' '),/коректні роки/);}
 const d=draft();d.year_from=d.year_to=2027;assert.deepEqual(Radar.validate(d,catalog),[]);
});
test('exactly the seven allowed profit thresholds pass validation',()=>{
 for(const min_profit of Radar.PROFITS)assert.deepEqual(Radar.validate({...draft(),min_profit},catalog),[]);
 for(const min_profit of [0,7,12.5,60,100,'10',null])assert.ok(Radar.validate({...draft(),min_profit},catalog).length);
});
test('arbitrary and missing price values cannot be saved',()=>{for(const value of [null,0,7,1.5,'3'])assert.ok(Radar.validate({...draft(),price_category:value},catalog).length);});
test('canonical payload does not mutate a draft and sorts distinct selections',()=>{
 const d=draft();d.name='  Моє авто  ';d.brands=[{brand_id:84,model_ids:[840]},{brand_id:9,model_ids:[91,90]}];d.region_ids=[10,1];const before=structuredClone(d),payload=Radar.canonical(d);assert.deepEqual(d,before);assert.equal(payload.name,'Моє авто');assert.deepEqual(payload.brands[0].model_ids,[90,91]);assert.deepEqual(payload.region_ids,[1,10]);
});
test('dashboard always has exactly three slots and deletion frees the original position',()=>{
 assert.equal(Radar.slots([]).length,3);const rows=[item(1),item(2),item(3)];assert.equal(Radar.slots(rows).filter(s=>s.radar).length,3);assert.equal(Radar.slots(rows.filter(r=>r.slot!==2))[1].radar,null);assert.throws(()=>Radar.slots([...rows,{...item(3),id:rid(4),slot:4}]));assert.throws(()=>Radar.slots([item(1),{...item(2),slot:1}]));
});
test('editing a server radar makes a copy and preserves models without another account data',()=>{
 const original=item(),edited=Radar.draftFrom(original);edited.brands[0].model_ids.push(91);edited.name='Інше';assert.equal(original.name,'Мій радар');assert.deepEqual(original.brands[0].model_ids,[90]);assert.equal('user_id' in edited,false);assert.equal('id' in edited,false);
});
test('all wizard steps validate their own controls, final review validates everything',()=>{
 const d=draft();d.price_category=null;assert.deepEqual(Radar.validate(d,catalog,0),[]);assert.ok(Radar.validate(d,catalog,1).length);assert.deepEqual(Radar.validate(d,catalog,2),[]);assert.ok(Radar.validate(d,catalog).length);
});
test('saving without a subscription is allowed and sends an idempotency key',async()=>{
 const calls=[],store=Radar.createStore({radars:async()=>({radars:[],limit:3,access:{active:false}}),createRadar:async payload=>{calls.push(payload);return item();}});await store.load();await store.save(draft(),null,'request-unique-0001');assert.equal(calls[0].client_request_id,'request-unique-0001');assert.equal(calls[0].active,undefined);assert.equal(store.snapshot.radars[0].status,'paused');
});
test('edit uses the same radar and revision, status updates preserve configuration',async()=>{
 const calls=[],store=Radar.createStore({radars:async()=>({radars:[item()],limit:3,access:{active:true}}),updateRadar:async(id,payload)=>{calls.push([id,payload]);return {...item(),name:payload.name,revision:2};},radarStatus:async(id,active,revision)=>{calls.push([id,active,revision]);return {...item(),name:'Змінений',status:active?'active':'paused',revision:3};}});
 await store.load();await store.save({...draft(),name:'Змінений'},item());assert.equal(calls[0][0],rid(1));assert.equal(calls[0][1].revision,1);await store.status(store.snapshot.radars[0],true);assert.deepEqual(calls[1],[rid(1),true,2]);assert.equal(store.snapshot.radars[0].name,'Змінений');
});
test('permanent deletion requires explicit confirmation, then removes only the target',async()=>{
 let deletes=0;const store=Radar.createStore({radars:async()=>({radars:[item(1),item(2)],limit:3,access:{active:false}}),deleteRadar:async()=>{deletes++;}});await store.load();await assert.rejects(store.remove(item(1),false),/Підтвердьте/);assert.equal(deletes,0);await store.remove(item(1),true);assert.equal(deletes,1);assert.deepEqual(store.snapshot.radars.map(r=>r.id),[rid(2)]);
});
test('failed writes preserve current state and renewal does not auto-resume',async()=>{
 const expired={...item(),status:'subscription_expired'},store=Radar.createStore({radars:async()=>({radars:[expired],limit:3,access:{active:true}}),radarStatus:async()=>{throw Error('Немає доступу');}});await store.load();assert.equal(store.snapshot.radars[0].status,'subscription_expired');await assert.rejects(store.status(expired,true));assert.equal(store.snapshot.radars[0].status,'subscription_expired');
});
test('an old refresh response cannot overwrite a successful mutation',async()=>{
 let release;const store=Radar.createStore({radars:()=>new Promise(resolve=>release=resolve),createRadar:async()=>item()});const refreshing=store.load();await store.save(draft());release({radars:[],limit:3,access:{active:false}});await refreshing;assert.equal(store.snapshot.radars.length,1);
});
test('independent accounts and returned snapshots cannot alter each other',async()=>{
 const a=Radar.createStore({radars:async()=>({radars:[item()],limit:3,access:{active:true}})}),b=Radar.createStore({radars:async()=>({radars:[],limit:3,access:{active:false}})});await Promise.all([a.load(),b.load()]);a.snapshot.radars[0].name='Не збережено';assert.equal(a.snapshot.radars[0].name,'Мій радар');assert.equal(b.snapshot.radars.length,0);
});
test('radar API keeps Telegram proof in header and includes optimistic revisions',async()=>{
 const calls=[],api=create(async(url,options)=>{calls.push([url,options]);return {ok:true,status:200,json:async()=>({})};},()=> 'signed-session');await api.radars();await api.radarCatalog(84);await api.createRadar(draft());await api.updateRadar(rid(1),{...draft(),revision:2});await api.radarStatus(rid(1),false,3);await api.deleteRadar(rid(1),4);
 assert.deepEqual(calls.map(c=>c[1].method),['GET','GET','POST','PUT','PATCH','DELETE']);for(const [url,options]of calls){assert.equal(options.headers['X-Telegram-Init-Data'],'signed-session');assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');assert.ok(!url.includes('signed-session'));}assert.match(calls[1][0],/brand_id=84$/);assert.deepEqual(JSON.parse(calls[4][1].body),{active:false,revision:3});assert.match(calls[5][0],/revision=4$/);
});
test('unsafe radar IDs, revisions and unauthenticated catalog never reach backend',async()=>{
 let calls=0;const api=create(async()=>{calls++;},()=> 'signed');for(const bad of ['../other',1,'abc','f'.repeat(31)]){await assert.rejects(api.updateRadar(bad,{}));await assert.rejects(api.deleteRadar(bad,1));await assert.rejects(api.radarStatus(bad,true,1));}await assert.rejects(api.radarStatus(rid(1),'true',1));await assert.rejects(api.deleteRadar(rid(1),0));await assert.rejects(api.radarCatalog('../9'));assert.equal(calls,0);await assert.rejects(create(()=>{throw Error('must not fetch');},()=> '').radarCatalog(),/Telegram/);
});
test('backend radar failures are Ukrainian and raw framework payloads never leak',async()=>{
 for(const status of [401,402,403,404,409,422,429,503]){const api=create(async()=>({ok:false,status,json:async()=>({detail:[{msg:'Input should be valid'}]})}),()=> 'signed');await assert.rejects(api.radars(),error=>/[А-Яа-яІіЇїЄєҐґ]/.test(error.message)&&!error.message.includes('Input'));}
 const api=create(async()=>({ok:false,status:409,json:async()=>({detail:'Радар уже змінено в іншому вікні.',code:'radar_changed'})}),()=> 'signed');await assert.rejects(api.radars(),error=>error.status===409&&error.code==='radar_changed'&&error.message==='Радар уже змінено в іншому вікні.');
});

test('malformed successful JSON never exposes raw browser diagnostics in reachable screens',async()=>{
 const api=create(async()=>({ok:true,status:200,json:async()=>{throw new SyntaxError('Unexpected token PRIVATE_RESPONSE');}}),()=> 'signed');
 for(const operation of [()=>api.radars(),()=>api.radarCatalog(),()=>api.notificationStatus(),()=>api.billingStatus(),()=>api.traceListing('123')]){
  await assert.rejects(operation(),error=>error.message.includes('некоректну відповідь')&&!/Unexpected|PRIVATE_RESPONSE/.test(error.message));
 }
});

test('notification errors use current Ukrainian controls and never legacy search limits',async()=>{
 for(const status of [403,404,409,422,429,503]){
  const api=create(async()=>({ok:false,status,json:async()=>({detail:'UNKNOWN_INTERNAL_NOTICE'})}),()=> 'signed');
  await assert.rejects(api.testNotification(),error=>/[А-Яа-яІіЇїЄєҐґ]/.test(error.message)&&!/20 серверних|UNKNOWN_INTERNAL|пошук/i.test(error.message));
 }
 const api=create(async()=>({ok:false,status:409,json:async()=>({detail:'Send a test notification first'})}),()=> 'signed');
 await assert.rejects(api.testNotification(),/«Надіслати тест»/);
});
test('published shell replaces old search form, keeps billing and contains only product-facing Ukrainian',()=>{
 const html=fs.readFileSync('index.html','utf8');assert.match(html,/<html lang="uk" data-mode="radars">/);for(const old of ['id="searchFilters"','id="minDiscount"','id="saveSearchForm"','src="cloud-searches.js','src="data/cars.js'])assert.ok(!html.includes(old));for(const id of ['radarSlots','radarEditor','radarPrices','radarAllUkraine','radarYearFrom','radarYearTo','radarProfits','deleteRadarDialog','billingStatus','testNotification'])assert.ok(html.includes('id="'+id+'"'));assert.match(html,/Уся Україна/);assert.match(html,/Рік від/);assert.match(html,/Рік до/);assert.doesNotMatch(html,/>\s*(Save|Cancel|Delete|Settings|Loading|Search)\b/);
});
test('listing support preserves factual Telegram acceptance and uncertain delivery explanations',()=>{
 assert.match(Radar.describeTrace({telegram_accepted:true}),/не підтверджує доставку на телефон/);
 assert.match(Radar.describeTrace({delivery_state:'uncertain'}),/автоматичного повтору немає/);
 assert.match(Radar.describeTrace({state:'not_observed'}),/не доводить/);
 assert.match(Radar.describeTrace({subscriptions:[{state:'below_min_discount',reason:'quota_exceeded'}]}),/Вигода нижча.*ліміт AUTO.RIA/);
 assert.doesNotMatch(Radar.describeTrace({subscriptions:[{state:'database_secret',reason:'raw_error'}]}),/database_secret|raw_error/);
});
