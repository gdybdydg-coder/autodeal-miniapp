const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
function setup(names=['BMW','Audi','Volkswagen','Mercedes-Benz','Skoda']) {
  function element(tag='div') {
    return {tag,value:'',children:[],attributes:{},listeners:{},style:{setProperty(){}},
      get options(){return this.children},
      replaceChildren(...nodes){this.children=nodes},
      setAttribute(key,value){this.attributes[key]=value},
      addEventListener(type,fn){(this.listeners[type]||=[]).push(fn)},
      dispatchEvent(event){for(const fn of this.listeners[event.type]||[])fn(event)},
      focus(){this.focused=true},showModal(){this.open=true},
      close(){this.open=false;this.dispatchEvent({type:'close'})},
      querySelector(tag){return this.children.find(node=>node.tag===tag)}};
  }
  const ids=Object.fromEntries(['brand','brandLabel','brandPickerButton','brandPickerValue','brandDialog',
    'brandSearch','brandOptions','brandSearchStatus','closeBrandDialog'].map(id=>[id,element()]));
  const Option=function(text,value){return {text,value}};
  ids.brand.replaceChildren(new Option('Всі марки',''),...names.map(name=>new Option(name,name)));
  let changes=0;
  ids.brand.addEventListener('change',()=>changes++);
  const document={getElementById:id=>ids[id],createElement:element};
  const window={document,AutoDealCloud:{catalog(){assert.fail('Typing must not call AUTO.RIA')}}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/brand-picker.js','utf8'),{window,Option,Event:function(type){this.type=type}});
  const dispatch=(id,type,extra={})=>ids[id].dispatchEvent({type,preventDefault(){},...extra});
  const open=()=>dispatch('brandPickerButton','click');
  const type=value=>{ids.brandSearch.value=value;dispatch('brandSearch','input')};
  const options=()=>ids.brandOptions.children.filter(n=>n.tag==='button');
  return {ids,window,dispatch,open,type,options,changes:()=>changes};
}

test('popular makes lead, remaining makes sort alphabetically and refreshed catalogs keep selection',()=>{
  const ui=setup(['Zotye','BMW','Alfa Romeo','Audi','Volkswagen','Mercedes-Benz','Bentley','Audi']);
  assert.deepEqual(ui.ids.brand.options.map(o=>o.value),['','Volkswagen','Audi','Mercedes-Benz','BMW','Alfa Romeo','Bentley','Zotye']);
  ui.ids.brand.value='Audi';ui.dispatch('brand','change');ui.open();ui.type('Bent');
  ui.ids.brand.children.push({text:'Aston Martin',value:'Aston Martin'});
  ui.window.AutoDealBrandPicker.refresh();
  assert.equal(ui.ids.brand.value,'Audi');assert.equal(ui.ids.brandPickerValue.textContent,'Audi');
  assert.deepEqual(ui.options().map(n=>n.textContent),['Bentley']);
  ui.type('');
  assert.deepEqual(ui.ids.brandOptions.children.filter(n=>n.tag==='h3').map(n=>n.textContent),['Популярні','За алфавітом']);
  assert.deepEqual(ui.ids.brand.options.slice(-4).map(o=>o.value),['Alfa Romeo','Aston Martin','Bentley','Zotye']);
});

test('partial names, case, spacing and Ukrainian aliases find makes without changing the applied filter',()=>{
  const ui=setup();ui.ids.brand.value='BMW';ui.dispatch('brand','change');ui.open();
  for(const query of ['volks',' VOLKSWAGEN ','фолькс','vw']) {
    ui.type(query);assert.deepEqual(ui.options().map(n=>n.textContent),['Volkswagen']);
    assert.equal(ui.ids.brand.value,'BMW');
  }
  ui.type('mercedes benz');assert.deepEqual(ui.options().map(n=>n.textContent),['Mercedes-Benz']);
  ui.type('not-a-car');assert.equal(ui.options().length,0);assert.match(ui.ids.brandSearchStatus.textContent,/не знайдено/);
  ui.dispatch('closeBrandDialog','click');
  assert.equal(ui.ids.brand.value,'BMW');assert.equal(ui.changes(),1);
  ui.open();assert.equal(ui.ids.brandSearch.value,'');assert.ok(ui.options().length>1);
});

test('confirming a make emits one change; choosing it again preserves its selected model',()=>{
  const ui=setup();ui.open();ui.type('Volks');ui.dispatch('brandSearch','keydown',{key:'Enter'});
  assert.equal(ui.ids.brand.value,'Volkswagen');assert.equal(ui.ids.brandDialog.open,false);
  assert.equal(ui.ids.brandPickerValue.textContent,'Volkswagen');assert.equal(ui.changes(),1);
  ui.open();ui.type('Volks');ui.options()[0].dispatchEvent({type:'click'});
  assert.equal(ui.changes(),1);
  ui.open();ui.options()[0].dispatchEvent({type:'click'});
  assert.equal(ui.ids.brand.value,'');assert.equal(ui.changes(),2);
});

test('an ambiguous query cannot accidentally pick the first make; restored filters update the label',()=>{
  const ui=setup(['Volkswagen','Volvo','Volkswagen Nutzfahrzeuge']);ui.open();ui.type('vol');
  ui.dispatch('brandSearch','keydown',{key:'Enter'});
  assert.equal(ui.changes(),0);assert.equal(ui.ids.brandDialog.open,true);
  ui.type('Volkswagen');ui.dispatch('brandSearch','keydown',{key:'Enter'});
  assert.equal(ui.ids.brand.value,'Volkswagen');assert.equal(ui.changes(),1);
  ui.ids.brand.value='Volvo';ui.dispatch('brand','change');
  assert.equal(ui.ids.brandPickerValue.textContent,'Volvo');
});
