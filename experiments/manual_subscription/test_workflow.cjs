const assert = require('node:assert/strict');
const {transition, DAY, MAX_RECEIPT_AGE, CLOCK_SKEW} = require('./workflow.js');

const original = {
  id: 'AD-DEMO', uid: 111, amount: 249, days: 30, state: 'awaiting', created: 1000,
};
const receipt = {
  type: 'receipt', filename: 'synthetic-receipt.pdf',
  receiptReference: 'SYNTH-RECEIPT-001', paidAt: 1001,
};
const review = transition(original, receipt, 1001);
assert.equal(original.state, 'awaiting');
assert.equal(review.state, 'review');
assert.equal(review.receiptReference, 'SYNTH-RECEIPT-001');
assert.equal(review.receiptRevision, 1);
assert.equal(review.expires, undefined);

for (const bad of ['', 'ab', '../receipt', 'receipt number', 'квитанція', 'x'.repeat(101)]) {
  assert.throws(() => transition(original, {...receipt, receiptReference: bad}, 1001));
}
assert.throws(() => transition(original, {...receipt, paidAt: 999}, 1001));
assert.throws(() => transition(
  {...original, created: 1}, {...receipt, paidAt: 1}, 1 + MAX_RECEIPT_AGE + 1,
));
assert.throws(() => transition(original, {...receipt, paidAt: 1001 + CLOCK_SKEW + 1}, 1001));

assert.throws(() => transition(review, {
  type: 'approve', reference: 'DEMO-1', amount: 249,
}, 1002));
assert.throws(() => transition(review, {
  type: 'approve', bankVerified: true, reference: 'DEMO-1', amount: 248,
}, 1002));
assert.throws(() => transition(review, {
  type: 'approve', bankVerified: true, reference: 'DEMO-1', amount: 249,
  usedReferences: ['DEMO-1'],
}, 1002));
const approved = transition(review, {
  type: 'approve', bankVerified: true, reference: 'DEMO-1', amount: 249,
}, 1002);
assert.equal(approved.expires, 1002 + 30 * DAY);
assert.throws(() => transition(approved, {
  type: 'approve', bankVerified: true, reference: 'DEMO-1', amount: 249,
}, 1003));

const clarify = transition(review, {type: 'clarify', note: ' Вкажи час тестового переказу '}, 1002);
assert.equal(clarify.state, 'clarification');
assert.equal(clarify.note, 'Вкажи час тестового переказу');
const resubmitted = transition(clarify, {
  ...receipt, filename: 'new-fixture.pdf', receiptReference: 'SYNTH-RECEIPT-002', paidAt: 1002,
}, 1003);
assert.equal(resubmitted.state, 'review');
assert.equal(resubmitted.receiptRevision, 2);
assert.equal(resubmitted.note, '');
assert.throws(() => transition(review, {type: 'clarify', note: ''}, 1002));
assert.throws(() => transition(review, {type: 'clarify', note: 'x'.repeat(501)}, 1002));

const rejected = transition(review, {type: 'reject'}, 1002);
assert.throws(() => transition(rejected, receipt, 1003));
assert.equal(transition(
  {...review, expires: 2000},
  {type: 'approve', bankVerified: true, reference: 'DEMO-2', amount: 249},
  1002,
).expires, 2000 + 30 * DAY);

console.log('Workflow checks passed');
for(const value of [' spaced ','line\nbreak','x'.repeat(101)]) {
  assert.throws(()=>transition(review,{type:'approve',bankVerified:true,reference:value,amount:249},1001));
}
assert.throws(()=>transition(original,{type:'receipt',filename:'x'.repeat(201)},1000));
assert.throws(()=>transition(review,{type:'clarify',note:'x'.repeat(501)},1002));
