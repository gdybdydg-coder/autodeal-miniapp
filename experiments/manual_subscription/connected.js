'use strict';
const fixtureTokens=window.AutoDealFixtureConfig;
async function request(role,path,body) {
  const options={headers:{'X-Fixture-Session':fixtureTokens[role]}};
  if(body) {
    options.method='POST';
    options.headers['Content-Type']='application/json';
    options.headers['X-AutoDeal-Fixture']='1';
    options.body=JSON.stringify(body);
  }
  const response=await fetch(path,options);
  const result=await response.json();
  if(!response.ok) throw Error(result.error || 'Тестова заявка недоступна');
  return result;
}
window.AutoDealFixtureApp=window.AutoDealPaymentUI.mount(document.getElementById('autodeal-payment-trial'),{
  state:role=>request(role,'/api/state'),action:(role,body)=>request(role,'/api/action',body)});
