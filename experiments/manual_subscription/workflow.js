(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ManualDemo = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';
  const DAY = 86400;
  function transition(order, action, now) {
    const next = {...order};
    if (!Number.isSafeInteger(now) || now <= 0) throw Error('Некоректний час');
    if (action.type === 'receipt') {
      if (!['awaiting','clarification'].includes(order.state)) throw Error('Заявка вже на перевірці або закрита');
      if (typeof action.filename !== 'string' || !action.filename.trim()) throw Error('Обери тестову квитанцію');
      next.receipt = action.filename.slice(0,200);
      next.state = 'review';
      next.note = '';
    } else if (action.type === 'clarify') {
      if (order.state !== 'review') throw Error('Спочатку потрібна квитанція');
      if (typeof action.note !== 'string' || !action.note.trim()) throw Error('Вкажи, що потрібно уточнити');
      next.note = action.note.trim().slice(0,500);
      next.state = 'clarification';
    } else if (action.type === 'approve') {
      if (order.state !== 'review') throw Error('Заявка недоступна для підтвердження');
      if (action.bankVerified !== true || !action.reference || !action.reference.trim()) throw Error('Познач тестову звірку й вкажи вигаданий номер платежу');
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
