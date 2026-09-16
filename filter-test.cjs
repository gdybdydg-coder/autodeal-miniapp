const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
assert.equal(new Set(ids).size,ids.length);
const nodes=[];
function element(tag='div') {
 const node={tag,value:'',checked:false,style:{},validity:{badInput:false},children:[],listeners:{},classList:{add(){},remove(){},toggle(){}},textContent:'',innerHTML:'',
 add(option){this.children.push(option)},append(...children){this.children.push(...children)},addEventListener(type,fn){this.listeners[type]=fn},setAttribute(){},focus(){this.focused=true},scrollIntoView(){},querySelector(){return element()}};
 nodes.push(node);return node;
}
const byId=Object.fromEntries(ids.map(id=>[id,element()]));
const document={getElementById(id){assert.ok(byId[id],id);return byId[id]},createElement:element,querySelectorAll(selector){if(selector==='.nav')return [];const m=selector.match(/^input\[name="([^"]+)"\](:checked)?$/);assert.ok(m,selector);return nodes.filter(n=>n.tag==='input'&&n.name===m[1]&&(!m[2]||n.checked))}};
const context=vm.createContext({document,window:{},Option:function(text,value){return {text,value}},console,setTimeout:()=>0,clearTimeout(){}});
vm.runInContext(fs.readFileSync(__dirname+'/data/cars.js','utf8'),context);
vm.runInContext(fs.readFileSync(__dirname+'/app.js','utf8'),context);
const run=code=>vm.runInContext(code,context);
const selected=(name,values)=>nodes.filter(n=>n.name===name).forEach(n=>n.checked=values.includes(n.value));
const count=()=>{run('searchCars()');return byId.count.textContent};
assert.equal(nodes.filter(n=>n.name==='body').length,13);
assert.equal(nodes.filter(n=>n.name==='fuel').length,10);
assert.equal(nodes.filter(n=>n.name==='transmission').length,6);
const reducer=nodes.find(n=>n.name==='transmission'&&n.value==='Редуктор');
const reducerLabel=nodes.find(n=>n.children.includes(reducer));
const electric=nodes.find(n=>n.name==='fuel'&&n.value==='Електро');
assert.equal(reducerLabel.style.display,'none');assert.equal(reducer.disabled,true);
electric.checked=true;electric.listeners.change();
assert.equal(reducerLabel.style.display,'');assert.equal(reducer.disabled,false);
reducer.checked=true;reducer.listeners.change();assert.equal(byId.transmissionCount.textContent,'1');
selected('fuel',['Дизель']);electric.listeners.change();
assert.equal(reducerLabel.style.display,'none');assert.equal(reducer.checked,false);assert.equal(byId.transmissionCount.textContent,'');
selected('fuel',['Електро','Дизель']);electric.listeners.change();
assert.equal(reducerLabel.style.display,'');reducer.checked=true;
byId.resetAdvanced.listeners.click();assert.equal(reducerLabel.style.display,'none');assert.equal(reducer.checked,false);
assert.equal(byId.advancedCount.textContent,'');
electric.checked=true;electric.listeners.change();assert.equal(reducer.checked,false);
byId.resetAdvanced.listeners.click();
console.log('PASS: reducer visibility, disabled state, deselection/reset, mixed fuels and counters.');
assert.equal(count(),'Знайдено: 5');
run('onlyDeals=false');assert.equal(count(),'Знайдено: 6');
selected('body',['Універсал']);assert.equal(count(),'Знайдено: 1');
selected('body',['Універсал','Седан']);assert.equal(count(),'Знайдено: 4');
selected('fuel',['Дизель']);assert.equal(count(),'Знайдено: 3');
selected('transmission',['Робот']);assert.equal(count(),'Знайдено: 1');
selected('transmission',['Робот','Автомат']);assert.equal(count(),'Знайдено: 3');
byId.mileageFrom.value='186';byId.mileageTo.value='186';assert.equal(count(),'Знайдено: 1');
byId.mileageTo.value='185';count();assert.match(byId.toast.textContent,/Пробіг.*Від/);
byId.mileageFrom.value='-1';count();assert.match(byId.toast.textContent,/невід/);
byId.region.value='Київська область';
byId.resetAdvanced.listeners.click();assert.equal(byId.region.value,'Київська область');assert.equal(count(),'Знайдено: 2');
byId.region.value='';selected('fuel',['Електро']);assert.equal(count(),'Знайдено: 0');
byId.resetAdvanced.listeners.click();byId.brand.value='BMW';byId.model.value='3 Series';assert.equal(count(),'Знайдено: 1');
byId.priceTo.value='10000';assert.equal(count(),'Знайдено: 0');
run('const f={brand:"",model:"",region:"",price:{from:null,to:null},year:{from:null,to:null},mileage:{from:0,to:0},body:[],fuel:[],transmission:[],onlyDeals:false}');
assert.equal(run('matchesFilters({price:1,year:2020,mileage:0},f)'),true);
assert.equal(run('matchesFilters({price:1,year:2020},f)'),false);
for (const car of context.window.AUTO_DEAL_DATA.cars) {
 assert.ok(context.window.AUTO_DEAL_DATA.bodyTypes.includes(car.body));
 assert.ok(context.window.AUTO_DEAL_DATA.fuelTypes.includes(car.fuel));
 assert.ok(context.window.AUTO_DEAL_DATA.transmissionTypes.includes(car.transmission));
}
assert.ok(html.indexOf('id="region"')<html.indexOf('id="advancedFilters"'));
console.log('PASS: option counts; multi-select OR; combined AND; mileage conversion/bounds/zero/missing; invalid ranges; reset; basic filters; data vocabulary; visible region.');
// Exercise manager event wiring with a minimal DOM, without claiming visual QA.
let savedRaw=null;
context.window.localStorage={getItem:()=>savedRaw,setItem:(key,value)=>{savedRaw=value;}};
context.window.addEventListener=()=>{};
context.Event=function(type){this.type=type;};
for(const node of nodes) {
 node.replaceChildren=function(...children){this.children=children};
 node.showModal=function(){this.open=true};
 node.close=function(){this.open=false};
 node.dispatchEvent=function(event){this.listeners[event.type]?.(event)};
}
vm.runInContext(fs.readFileSync(__dirname+'/saved-searches.js','utf8'),context);
vm.runInContext(fs.readFileSync(__dirname+'/search-manager.js','utf8'),context);
byId.brand.value='BMW';byId.model.value='3 Series';byId.priceTo.value='20000';
byId.saveSearchBtn.listeners.click();
assert.equal(byId.savedDialog.open,true);assert.equal(byId.saveSearchForm.hidden,false);
assert.match(byId.draftSummary.textContent,/BMW/);
byId.savedName.value='My BMW';byId.draftNotify.checked=true;
byId.saveSearchForm.listeners.submit({preventDefault(){}});
assert.equal(JSON.parse(savedRaw).searches.length,1);
assert.equal(JSON.parse(savedRaw).searches[0].notificationsWanted,true);
assert.equal(byId.saveSearchForm.hidden,true);
byId.brand.value='Audi';byId.model.value='A4';
run('applySavedFilters(savedAPI.read(window.localStorage)[0].filters)');
assert.equal(byId.brand.value,'BMW');assert.equal(byId.model.value,'3 Series');assert.equal(byId.priceTo.value,20000);
const card=byId.savedSearchList.children[0];
const actions=card.children[4];
actions.children[1].listeners.click(); // First delete click requests confirmation.
assert.equal(JSON.parse(savedRaw).searches.length,1);
byId.savedSearchList.children[0].children[4].children[1].listeners.click();
assert.equal(JSON.parse(savedRaw).searches.length,0);
context.window.localStorage.setItem=()=>{throw Error('blocked')};
byId.saveSearchBtn.listeners.click();
byId.saveSearchForm.listeners.submit({preventDefault(){}});
assert.equal(byId.savedError.hidden,false);assert.equal(byId.saveSearchForm.hidden,false);
console.log('PASS: manager open/save/restore, draft notification preference, confirmed deletion and storage failure UI.');
// Cloud UI: explicit saving, read-back, error recovery, and confirmed deletion.
(async()=>{
 let cloudItems=[],saves=0,deletes=0;
 const localBefore=savedRaw;
 byId.saveSearchForm.reportValidity=()=>true;
 context.window.AutoDealCloud={
  list:async()=>cloudItems,
  save:async(name,filters)=>{saves++;cloudItems=[{id:7,name,filters,enabled:false}];},
  remove:async()=>{deletes++;cloudItems=[];}
 };
 vm.runInContext(fs.readFileSync(__dirname+'/cloud-searches.js','utf8'),context);
 assert.equal(saves,0); // No import or writes on script load.
 byId.savedName.value='<img src=x onerror=alert(1)>';
 byId.saveCloudSearch.listeners.click();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(saves,1);assert.equal(savedRaw,localBefore);
 assert.equal(byId.cloudSearchList.children[0].children[0].textContent,byId.savedName.value);
 assert.match(byId.cloudSearchList.children[0].children[2].textContent,/без сповіщень/);
 const actions=byId.cloudSearchList.children[0].children[3];
 actions.replaceChildren=function(...children){this.children=children;};
 actions.children[1].listeners.click();assert.equal(deletes,0);
 actions.children[0].listeners.click();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(deletes,1);assert.equal(byId.cloudSearchList.children.length,0);
 context.window.AutoDealCloud.list=async()=>{throw Error('Session expired');};
 byId.refreshCloud.listeners.click();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(byId.cloudStatus.textContent,'Session expired');
 assert.equal(byId.refreshCloud.disabled,false);
 console.log('PASS: cloud UI save/read/delete confirmation, no automatic import, text-safe rendering, recovery.');
})().catch(error=>{console.error(error);process.exitCode=1;});
