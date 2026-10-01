(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ManualDemo = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';
  const DAY = 86400;
  function reference(value,limit) {
    if(typeof value !== 'string' || value !== value.trim() || !value.length || value.length>limit || /[\x00-\x1f\x7f]/.test(value)) throw Error('Некоректне тестове посилання');
    return value;
  }
  function transition(order, action, now) {
    const next = {...order};
    if (!Number.isSafeInteger(now) || now <= 0) throw Error('Некоректний час');
    if (action.type === 'receipt') {
      if (!['awaiting','clarification'].includes(order.state)) throw Error('Заявка вже на перевірці або закрита');
      next.receipt = reference(action.filename,200);
      next.state = 'review';
      next.note = '';
    } else if (action.type === 'clarify') {
      if (order.state !== 'review') throw Error('Спочатку потрібна квитанція');
      if (typeof action.note !== 'string' || !action.note.trim() || action.note.trim().length>500 || /[\x00-\x09\x0b-\x1f\x7f]/.test(action.note)) throw Error('Вкажи уточнення до 500 символів');
      next.note = action.note.trim();
      next.state = 'clarification';
    } else if (action.type === 'approve') {
      if (order.state !== 'review') throw Error('Заявка недоступна для підтвердження');
      if (action.bankVerified !== true) throw Error('Познач тестову звірку надходження');
      reference(action.reference,100);
      if (action.amount !== order.amount) throw Error('Сума не збігається');
      if ((action.usedReferences || []).includes(action.reference.trim())) throw Error('Цей платіж уже зараховано');
      next.state = 'approved';
      next.reference = action.reference.trim();
      next.expires = Math.max(now, order.expires || 0) + order.days*DAY;
    } else if (action.type === 'reject') {
      if (!['review','clarification'].includes(order.state)) throw Error('Заявка недоступна для відмови');
      next.state = 'rejected';
    } else throw Error('Невідома дія');
    return next;
  }
  return {transition, DAY};
});
