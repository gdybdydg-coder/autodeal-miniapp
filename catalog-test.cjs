const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const entries=names=>names.map((name,i)=>({name,value:i+1}));
function setup(fetch) {
  const control=()=>({value:'',children:[],listeners:{},replaceChildren(...items){this.children=items},
    addEventListener(type,fn){this.listeners[type]=fn}});
  const brand=control(),model=control(),region=control(),status=control(),retry=control();
  const data={catalog:{BMW:['X5']},regions:['Вся Україна','Київська область']};
  const advancedGroups=['body','fuel','transmission'].map(name=>({name,options:[]}));
  let pickerValues=[];
  const window={AUTO_DEAL_DATA:data,AutoDealCloud:{catalog:fetch},
    AutoDealBrandPicker:{refresh(){pickerValues=brand.children.map(o=>o.value)}}};
  const context=vm.createContext({window,brand,model,region,advancedGroups,
    selectedValues:()=>[],renderAdvancedOptions(){},Option:function(text,value){return{text,value}},
    renderRegionOptions(){region.replaceChildren(...data.regions.slice(1).map(value=>({value})));},
    document:{getElementById:id=>({catalogStatus:status,retryCatalog:retry})[id]}});
  vm.runInContext(fs.readFileSync(__dirname+'/catalog-loader.js','utf8'),context);
  return{window,data,brand,model,region,status,retry,pickerValues:()=>pickerValues};
}
const rootCatalog=()=>({brands:entries(['BMW','Peugeot','Toyota']),regions:entries(['Київська','Сумська','Хмельницька']),
  body:entries(['Седан']),fuel:entries(['Дизель','Електро']),transmission:entries(['Автомат','Редуктор'])});

test('official brands and regions expand choices; models load once per selected brand',async()=>{
  const calls=[];
  const ui=setup(async brand=>{calls.push(brand||'root');return brand?{models:entries(['3008','5008'])}:rootCatalog()});
  await tick();
  assert.ok(ui.brand.children.some(o=>o.value==='Peugeot'));
  assert.ok(ui.pickerValues().includes('Peugeot'));
  assert.ok(ui.region.children.some(o=>o.value==='Сумська область'));
  assert.deepEqual(calls,['root']);
  ui.brand.value='Peugeot';await ui.brand.listeners.change();
  assert.deepEqual(Array.from(ui.data.catalog.Peugeot),['3008','5008']);
  assert.equal(ui.model.disabled,false);
  await ui.window.AutoDealCatalog.ensureFilters({brand:'Peugeot'});
  assert.deepEqual(calls,['root','Peugeot']);
});

test('late response for a previous brand cannot overwrite current models',async()=>{
  const waiting={};
  const ui=setup(brand=>brand?new Promise(resolve=>waiting[brand]=resolve):Promise.resolve(rootCatalog()));
  await tick();ui.brand.value='Peugeot';const first=ui.brand.listeners.change();
  assert.equal(ui.window.AutoDealCatalog.isBusy(),true);
  ui.brand.value='Toyota';const second=ui.brand.listeners.change();
  await tick();
  waiting.Peugeot({models:entries(['3008'])});await first;
  assert.ok(!ui.model.children.some(o=>o.value==='3008'));
  await tick();waiting.Toyota({models:entries(['Corolla'])});await second;
  assert.deepEqual(ui.model.children.map(o=>o.value),['','Corolla']);
  assert.equal(ui.window.AutoDealCatalog.isBusy(),false);
});

test('catalog failure keeps current choices and offers an explicit retry',async()=>{
  let fail=true;
  const ui=setup(async()=>{if(fail)throw Error('Сервер не відповідає');return rootCatalog()});
  await tick();assert.equal(ui.retry.hidden,false);assert.match(ui.status.textContent,/не відповідає/);
  assert.deepEqual(ui.data.catalog.BMW,['X5']);
  fail=false;await ui.retry.listeners.click();
  assert.ok(ui.brand.children.some(o=>o.value==='Toyota'));assert.equal(ui.retry.hidden,true);
});
