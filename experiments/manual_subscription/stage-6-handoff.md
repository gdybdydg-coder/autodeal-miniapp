# Етап 6 — підключений екран і демонстрація власнику, 01.10.2026

Доручення «Давай» о 15:25 Kyiv: перейти до власної перевірки заявки й
ручного підтвердження без реального переказу. Production не змінювати.
Read-only main: 005897b0a93c7fe0166424341c0564a5fbaf94cb; WIP база:
4eee61a91a5ea411f59cf6445f296e5ccb6c8ac7, tree
bf01352a9433849a134ab908d08030a712e61e55. 17 файлів відновлено зі свіжого
remote snapshot, кожен звірено за Git blob SHA. AGENTS.md у дереві немає.

## Два чітко різні режими

1. **Підключений локальний тест.** trial.html/payment-ui.js -> connected.js
   -> http://127.0.0.1 -> Harness -> Adapter -> SQLite Ledger. Заявка,
   рішення, строк, audit та offline outbox реально зберігаються у test DB.
   Обидві ролі owner review мають окремі server-issued synthetic tokens;
   client token не може approve/reject/clarify чи підмінити uid/tariff/now.
   Перевірено реальними loopback requests, а не тільки JS state model.
2. **Демонстрація в чаті.** payment-trial-inline.html використовує той самий
   UI/handlers із RAM demo-adapter.js/наявним workflow.js. Мережі, HTTP,
   SQLite або browser storage немає. При повторному відкритті дані
   скидаються. Не видавати цей режим за durable DB, bank verification,
   справжню auth або активацію робочого бота. Його мета — поклікати UX.

Сценарій: client create -> «Я оплатив» -> кнопка synthetic receipt ->
admin: звірити тестову суму/checkbox -> approve -> client active date.
Є clarify/resubmit, reject/new order і renewal. Усі назви та помилки
українські, час Europe/Kyiv. Сума 249 грн/30 днів — не затверджений тариф.
Файл квитанції не читається/не завантажується; це metadata fixture.

## Harness

Стандартна бібліотека Python; outgoing HTTP, банк, Telegram, AUTO.RIA,
production backend/DB, secrets, env та runtime jobs не використовуються.
Server bind тільки 127.0.0.1. Allowlisted asset/routes, Host/Origin/
Sec-Fetch-Site/action-header guards, JSON body <=8192 bytes, CSP self-only,
no-store/nosniff; cookies/CORS не використовуються. Session body/headers
не логуються. Це захист локального огляду, не production authentication.
Bootstrap навмисно дає власнику обидві fixture ролі; публікувати його
або зробити remote bind заборонено. /api/source-status та production
paths відсутні. Конфіг/URL не ведуть до Render або робочого бота.

CLI `python experiments/manual_subscription/harness.py --db ./manual-fixture.sqlite`.
Адреса доступна лише на тому самому комп'ютері. Без --db тимчасова БД
видаляється при завершенні. З --db потрібен новий test file; restart
зберігає облік і synthetic /stop/epoch/claims, але не старі RAM tokens.
Справжній TTL, revocation persistence/rotation та login ще не реалізовані.

## Докази

- `python -m unittest discover -s experiments/manual_subscription -q`:
  **62 passed**, 3.894s: 55 існуючих + 7 HTTP harness tests. Покрито
  restart, receipt/clarify/resubmit, approval, duplicate approval, amount/
  bank checkbox guards, session/role/payload/Host/Origin/metadata guards,
  assets та відсутність production paths.
- `python experiments/manual_subscription/check-ui.py`: **passed**.
  Actual payment-ui.js listeners і connected.js transport пройшли весь
  шлях до SQLite через тимчасовий loopback server: create/receipt,
  missing checkbox, wrong amount, clarification/resubmit, approval,
  renewal, fresh mount. Search залишається false; зміни не вмикають його.
- Node event fixture також перевірив RAM preview, exact generated inline
  fragment, busy/double-tap guard, reject/new-order та RAM role guard.
- `node experiments/manual_subscription/test_workflow.cjs`: **passed**.
- Generated inline не містить transport/storage; authored markup прочитано
  назад. У генераторі виправлено duplicate class attributes на main CTA,
  додано regression assertion. Перший DOM fixture пропускав h2 через regex
  без цифр; виправлено fixture parser, усі реальні data-ui selectors є.

Event fixture — мінімальна DOM модель, не повний browser engine. Installed
Playwright package не має Chromium executable; browser render/visual/
accessibility QA не виконано й не приписується цим перевіркам. Власник
отримує интерактивний RAM preview для власної UX перевірки.

## Межі й наступний крок

Жодних реальних реквізитів, переказів, файлів-квитанцій, надсилань або
доступу в production. SQLite Ledger, Adapter та payment formula/квоти
AUTO.RIA не змінювалися. Інтерфейс показує тестовий абонемент, не чинний
Stars access власника. Main/Render/deploy/env та інші гілки не чіпалися.

Після UX feedback: визначити owner-only preview delivery без public
roles/bootstrap, реальну server auth/session lifetime, receipt upload
privacy, reconciliation claimed/prepared/uncertain та source admission/
history retention. Реальну оплату не починати до узгодження платіжної
моделі/правил платформи, тарифу, отримувача та refund/review правил.
Це не причина зупиняти дозволену offline розробку. Не змінювати
production і не створювати paid resources чи deployments.
