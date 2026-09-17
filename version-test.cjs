const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
test('update fetches same-origin version without identity and preserves Telegram fragment on reload',async()=>{
  let reload,calls=0;
  const button={textContent:'Оновити',addEventListener(t,fn){this[t]=fn}};
  const window={location:{href:'https://gdybdydg-coder.github.io/autodeal-miniapp/?v=old#tgWebAppData=fake-test',replace:v=>reload=v},addEventListener(){},
    fetch:async(url,options)=>{calls++;assert.ok(url.startsWith('https://gdybdydg-coder.github.io/autodeal-miniapp/release.json?'));
      assert.ok(!url.includes('tgWebAppData'));assert.equal(options.cache,'no-store');assert.equal(options.credentials,'omit');
      return{ok:true,json:async()=>({version:'new-19'})};}};
  const document={querySelector:()=>({content:'old'}),getElementById:()=>button,addEventListener(){}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/app-version.js','utf8'),{window,document,URL,Date,AbortSignal});
  await new Promise(r=>setImmediate(r));
  assert.match(button.textContent,/нову версію/);assert.equal(reload,undefined);
  await button.click();
  assert.equal(new URL(reload).searchParams.get('v'),'new-19');
  assert.equal(new URL(reload).hash,'#tgWebAppData=fake-test');
  assert.equal(calls,2);
});
