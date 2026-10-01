# Етап 5а — актуальність повідомлень і ліміт outbox, 01.10.2026

Перед початком read-only перевірено remote main
`005897b0a93c7fe0166424341c0564a5fbaf94cb` та WIP
`6accc58f6e805749e41bba750b7401dbe985ae8a`, tree
`7da005914958c80e3d21f430753ef73c074d46ed`.
Усі 13 вхідних файлів experiments/manual_subscription відновлено з remote
blobs у свіжому snapshot і звірено за Git blob SHA. Прочитано README та
handoffs етапів 2, 3а, 3, 4. Production та інші WIP не змінювалися.

Перед записом виявлено паралельний `be8613e4dfe5a6c11044700039550a335f2ba1fd`,
tree `234889bfa135d2c74fcb4b5b34ab43b04160ced3`: він додав readiness review
і 3 isolation tests. Нові файли прочитано та відновлено за Git blob SHA,
README узгоджено, зміни цього кроку побудовано поверх be8613e4 без force.
Readiness не затерто: збережено попередній результат і додано оновлення.

## Відтворені дефекти

Два нові regression tests запускали на незміненому stage-4 Ledger:

- review -> clarification -> resubmit -> approval повертав worker чотири
  notices, включно зі старим проханням уточнити квитанцію; потрібне лише
  актуальне підтвердження;
- 105 синтетичних квитанцій створювали 105 активних outbox records без
  обмеження. Batch claim limit не обмежував накопичення.

Обидва тести спочатку failed. Після виправлення вони проходять.

## Реалізація

- Notice перевіряється за чинним order state, receipt revision, owner,
  payload, membership expiry і точним dedupe key. Appearance або старий
  payload не є доказом актуальності. Unknown/malformed notice відхиляється.
- Застарілі pending отримують cancelled, reason та superseded_at. Audit,
  платіжний reference, orders/memberships і terminal history зберігаються.
  Після renewal старе expiry notice/activation не описує чинний доступ.
- Після claim потрібен одноразовий `prepare_outbox()` того самого worker
  безпосередньо перед hypothetical send. Він повторно перевіряє поточний
  стан. Аcknowledge delivered/uncertain потребує успішного preflight.
  Prepared claim не можна використати повторно, зокрема після restart.
- Capacity за замовчуванням 100, допустимий fixture діапазон 1–1000.
  Значення збережене у SQLite fixture_settings, спільне для workers;
  конструктор не дозволяє непомітний override після restart.
  Враховуються pending + claimed + uncertain. Check/insert і workflow
  рішення — одна BEGIN IMMEDIATE транзакція.
- Повна черга не відкочує approval, строк доступу, payment reference чи
  audit. Нестворений notice позначається одним durable flag у вихідній
  заявці або membership. Наступний claim відновлює актуальні наміри
  batch максимум 100; flag переживає restart. Revision changes замінюють
  старий намір, renewal залишає лише останню deferred activation для uid.
- Renewal очищає deferred expiry flag. Існуючий expiry dedupe key зі
  stage-3 бази не записується повторно після additive upgrade.
- Старий spool понад configured cap не знищується і не збільшується:
  deferred notices чекають, доки workers звільнять місце. Перевірено
  поступове drain 100 старих + 5 відкладених events без втрати/дубля.
- outbox_status показує active, capacity, counts за state і deferred
  source flags. Це локальна діагностика моделі, не production endpoint.

## Перевірки

`python -m unittest discover -s experiments/manual_subscription -v`:
**55 passed** (33 lifecycle/auth/concurrency, 3 паралельні isolation review
та 19 нових outbox tests). До паралельного review окремий прогін мав
52 passed, 0.333s; після його інтеграції весь набір перевірено повторно.
`node experiments/manual_subscription/test_workflow.cjs`: **passed**.
Тільки stdlib SQLite temporary fixtures та локальна JS модель; жодних
банківських, Telegram, AUTO.RIA чи інших мережевих викликів.

Додатково перевірено backpressure з одночасними writers, deferred approval
restart, current revision, claimed expiry після renewal, stale activation
після expiry, owner/worker guards, одноразовий preflight, capacity
validation, additive old-schema migration та незмінність uncertain.
Synthetic /stop, epoch=9, sent=13, uncertain=2 збереглися при deferred
approval. Усі попередні auth/ownership/amount/dedup сценарії пройшли.

Stage-4 integration test збережено і уточнено: сім історичних outbox rows
залишаються, але після повного lifecycle шість cancelled мають точні
reasons, а лише поточне expiry claim переходить у synthetic uncertain.
Це виправляє старе очікування drain семи застарілих notices, не приховує
financial/audit регресію. Наявні fake ack tests тепер роблять preflight.

## Межі й наступний крок 5б

Немає реальної доставки або мережевого транспорту. Preflight не дає
гарантії проти зміни стану після повернення з нього й до майбутнього
зовнішнього send; її контракт і reconciliation треба визначити до
інтеграції. Claimed/prepared/uncertain після crash не повертаються в
pending й не звільняють cap автоматично. Вони можуть блокувати spool;
ручна audit reconciliation policy ще не реалізована.

Ліміт обмежує активний spool, не весь розмір БД. Фінансова історія та
source flags живуть в orders/memberships; загальна admission/retention
policy для них ще потрібна. Не називати весь backlog або всі записи
глобально bounded. Legacy spool вже понад cap не скорочується штучно.

FixtureSessions залишаються RAM test setup, не production auth; потрібно
перевірити lifetime/revocation/time ownership. Demo UI досі не зв'язаний
із adapter/SQLite; потрібен DOM event/role-boundary QA. Receipt refs —
synthetic metadata, не справжні завантажені файли. 249 грн — fixture,
не затверджений тариф; approval bank_verified — модель ручної звірки.

Наступний крок: session lifetime/revocation offline, UI event wiring та
остаточний readiness review. До будь-якого майбутнього клієнтського
запуску окремо потрібні затверджені тариф/строк, платіжна модель з
урахуванням чинних правил Telegram, реквізити отримувача, очікуваний
час ручного підтвердження і правила повернення. Ці дані не блокують
поточну offline розробку; не підставляти реквізити конкурента.

Main/Render/env/deploy/runtime, справжні абонементи та claims, Stars pilot,
платні AUTO.RIA запити не змінювалися. Merge/deploy зараз заборонені.
Підсумкову готовність ще не підтверджено; цей крок закінчений, решта
тестової роботи залишається. Перед наступним записом прочитати fresh
remote head і нові handoffs, зберегти конкурентні зміни без force.
