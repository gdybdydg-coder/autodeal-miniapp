const assert = require('node:assert/strict');
const {transition, DAY} = require('./workflow.js');
const original = {id:'AD-DEMO',uid:111,amount:249,days:30,state:'awaiting'};
const review = transition(original,{type:'receipt',filename:'synthetic-receipt.pdf'},1000);
assert.equal(original.state,'awaiting');
assert.equal(review.state,'review');
assert.equal(review.expires,undefined);
assert.throws(()=>transition(review,{type:'approve',reference:'demo-1',amount:249},1001));
assert.throws(()=>transition(review,{type:'approve',bankVerified:true,reference:'demo-1',amount:248},1001));
assert.throws(()=>transition(review,{type:'approve',bankVerified:true,reference:'demo-1',amount:249,usedReferences:['demo-1']},1001));
const approved = transition(review,{type:'approve',bankVerified:true,reference:'demo-1',amount:249},1001);
assert.equal(approved.expires,1001+30*DAY);
assert.throws(()=>transition(approved,{type:'approve',bankVerified:true,reference:'demo-1',amount:249},1002));
const clarify = transition(review,{type:'clarify',note:'Вкажи час тестового переказу'},1002);
assert.equal(clarify.state,'clarification');
assert.equal(transition(clarify,{type:'receipt',filename:'new-fixture.pdf'},1003).state,'review');
assert.throws(()=>transition(review,{type:'clarify',note:''},1002));
const rejected = transition(review,{type:'reject'},1002);
assert.throws(()=>transition(rejected,{type:'receipt',filename:'new-fixture.pdf'},1003));
assert.equal(transition({...review,expires:2000},{type:'approve',bankVerified:true,reference:'demo-2',amount:249},1001).expires,2000+30*DAY);
console.log('Workflow checks passed');
for(const value of [' spaced ','line\nbreak','x'.repeat(101)]) {
  assert.throws(()=>transition(review,{type:'approve',bankVerified:true,reference:value,amount:249},1001));
}
assert.throws(()=>transition(original,{type:'receipt',filename:'x'.repeat(201)},1000));
assert.throws(()=>transition(review,{type:'clarify',note:'x'.repeat(501)},1002));
