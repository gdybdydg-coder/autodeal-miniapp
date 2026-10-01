# Етап 9 — погоджений тариф 250 грн / 30 днів, 01.10.2026

Власник о **19:36 Kyiv** відповів «Давай 250 грн» на питання про ціну
30-денного абонемента. Ціну погоджено. Це не дозвіл на merge/deploy,
production/env/підписки/claims/accounting, bank API, фактичний збір
грошей або Telegram повідомлення. Продовжено лише ізольований тест.

## Свіжа база

- Remote main: 005897b0a93c7fe0166424341c0564a5fbaf94cb.
- WIP base: 34e0964e5e43acab3e1656236cb317b61dc9f8fb,
  tree e293371f24b7aec09aabfd3bba99e2078030dfed.
- Render read-only: dep-dav2570473hc73d7bkj0 live, same main,
  finished 2026-10-01T09:07:41.772737Z.
- 45 файлів WIP отримано заново GitHub connector; кожен Git blob SHA
  перевірено. Remote AGENTS.md немає. Прочитано stage-8 handoff/README.
  Старі локальні checkout не використані як source of truth.

## Зміни

- `tariff.py`: одна Python конфігурація 250 UAH / 30 днів; collection
  незмінно false. Trusted Adapter defaults і Harness state використовують
  ці значення. У клієнтського payload немає тарифних повноважень.
- Нові orders і renewal — 250/30. Наявні open/review orders зберігають
  amount/days snapshot; вони не переписуються міграцією або restart.
  Підтвердження звіряє exact integer amount з order, не поточною ціною.
- Authenticated реквізити мають погоджений тариф лише для snapshot
  250/30; інші історичні test quotes не позначаються погодженими.
  `payments_enabled=false`, явний текст «Не переказуй кошти» всюди.
- Recipient JSON schema v1 і дані не змінено. Legacy input
  `tariff_confirmed=false` — заборона профілю затверджувати ціну,
  НЕ актуальний статус тарифу. Server quote/instruction визначають
  поточне погодження незалежно від цього поля. Profile із true
  лишається невалідним. Не вставляти private реквізити в GitHub.
- UI header/payment/admin amount використовують збережену ціну заявки,
  кнопка наступної заявки — current quote 250. Жодного статичного
  старого value=249. Сума review заповнюється з order, не HTML.
  Checkbox звірки скидається після зміни order/receipt revision в усіх
  тестових варіантах, щоб старе рішення не переходило на нову квитанцію.
- RAM demo теж 250/30; inline HTML відтворено актуальним builder і
  перевірено. У ньому немає auth/HTTP/постійного збереження/приватних
  реквізитів; він не заміняє authenticated owner harness.

## Перевірки та межі доказів

- `check-offline.py`: **103 tests**, 11.811s, OK, non-loopback TCP/DNS
  fenced. 101 попередній і 2 нові snapshot/payment-boundary тести.
  Default HTTP/Adapter tests адаптовано під current quote 250; direct
  Ledger/custom historical fixtures 249 залишено, не замінено глобально.
- New snapshot test із двома previous states awaiting/review: незмінний
  old order 249 після restart/create, неправильні 250 відхилено, fake
  approval 249 нараховує лише 30 днів, duplicate після restart не
  подовжує, наступний renewal 250 додає 30 днів, expiry exact,
  /stop=false guard epoch9/sent13/uncertain2 збережені.
- Для нового 250 quote: 249, 251, float250.0, string250, bool не
  підтверджують доступ; review/неактивний membership/stop збережені.
- `check-owner-ui.py`: actual listeners -> loopback HTTP -> local
  sessions/upload/SQLite passed; payment/header/admin input 250,
  approved-price notice/IBAN-purpose copy, lost response duplicate
  approval, auth rotation/logout/client denial passed.
- `check-ui.py`: RAM/inline/loopback SQLite passed, додано old snapshot
  header/admin249 при current quote250. Перший запуск зупинився на
  старому сценарії, який не ставив checkbox повторно після resubmit.
  Перевірено новий reset і явну повторну звірку; safeguard не прибрано.
- `test_workflow.cjs`: passed із історичним custom quote 249.
- Actual private owner profile authenticated smoke: 250/30 погоджено,
  collection false, exact recipient fields у local instruction; даних
  немає в SQLite або experimental source. Значення не логували та не
  надсилали для перевірки банку/зовнішнім сервісам.

Мінімальна DOM event fixture, не actual browser renderer/clipboard/
file picker/visual/accessibility QA. Жодних actual funds, auto-debits,
банківської звірки, Telegram sends або AUTO.RIA requests. Production
тести не запускали, оскільки production код не змінювали; ці перевірки
не є доказом готовності майбутньої інтеграції.

## Наступний крок

Ціна й private отримувач уже надані. Не питати повторно тариф або не
називати 249 погодженою ціною. Продовжувати offline entitlement
admission/preflight/restart/stop/expiry та reconciliation claimed/
uncertain notices без production інтеграції. До реальних платежів
залишаються owner-only phone/auth transport, payment-model/platform
compatibility, privacy/scanning/retention і review/refund правила.
Не створювати платні ресурси/deploys/нові automation або публічний
платіжний екран у чинному боті. Git WIP тільки після fresh head,
non-force update й exact-tree verification tested blobs.
