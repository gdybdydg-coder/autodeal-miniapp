(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ManualDemo = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';
  const DAY = 86400;
  const MAX_RECEIPT_AGE = 7 * DAY;
  const CLOCK_SKEW = 300;
  const REFERENCE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$/;

  function syntheticReference(value, label) {
    if (typeof value !== 'string' || value !== value.trim() || !REFERENCE_RE.test(value)) {
      throw Error('Некоректний ' + label);
    }
    return value;
  }

  function boundedText(value, limit, message, allowNewline) {
    const controls = allowNewline ? /[\x00-\x09\x0b-\x1f\x7f]/ : /[\x00-\x1f\x7f]/;
    if (typeof value !== 'string' || value !== value.trim() || !value.length ||
        value.length > limit || controls.test(value)) {
      throw Error(message);
    }
    return value;
  }

  function transition(order, action, now) {
    const next = {...order};
    if (!Number.isSafeInteger(now) || now <= 0) throw Error('Некоректний час');
    if (action.type === 'receipt') {
      if (!['awaiting', 'clarification'].includes(order.state)) {
        throw Error('Заявка вже на перевірці або закрита');
      }
      if (!Number.isSafeInteger(action.paidAt) || action.paidAt <= 0 ||
          action.paidAt < order.created || action.paidAt < now - MAX_RECEIPT_AGE ||
          action.paidAt > now + CLOCK_SKEW) {
        throw Error('Час квитанції поза дозволеним тестовим діапазоном');
      }
      next.receipt = boundedText(
        action.filename, 200, 'Обери коректну назву тестової квитанції', false,
      );
      next.receiptReference = syntheticReference(action.receiptReference, 'номер квитанції');
      next.receiptAt = action.paidAt;
      next.receiptRevision = (order.receiptRevision || 0) + 1;
      next.state = 'review';
      next.note = '';
    } else if (action.type === 'clarify') {
      if (order.state !== 'review') throw Error('Спочатку потрібна квитанція');
      const note = typeof action.note === 'string' ? action.note.trim() : '';
      if (!note || note.length > 500 || /[\x00-\x09\x0b-\x1f\x7f]/.test(action.note)) {
        throw Error('Вкажи уточнення до 500 символів');
      }
      next.note = note;
      next.state = 'clarification';
    } else if (action.type === 'approve') {
      if (order.state !== 'review') throw Error('Заявка недоступна для підтвердження');
      if (action.bankVerified !== true) throw Error('Познач тестову звірку надходження');
      const paymentReference = syntheticReference(action.reference, 'номер платежу');
      if (action.amount !== order.amount) throw Error('Сума не збігається');
      if ((action.usedReferences || []).includes(paymentReference)) {
        throw Error('Цей платіж уже зараховано');
      }
      next.state = 'approved';
      next.reference = paymentReference;
      next.expires = Math.max(now, order.expires || 0) + order.days * DAY;
    } else if (action.type === 'reject') {
      if (!['review', 'clarification'].includes(order.state)) {
        throw Error('Заявка недоступна для відмови');
      }
      next.state = 'rejected';
    } else {
      throw Error('Невідома дія');
    }
    return next;
  }

  return {transition, DAY, MAX_RECEIPT_AGE, CLOCK_SKEW};
});
