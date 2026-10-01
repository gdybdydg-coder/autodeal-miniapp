# Етап 7 — окремі локальні входи та файли квитанцій, 01.10.2026

Доручення власника о 16:51 Kyiv: «Давай, підключаємо далі, працюємо в темпі».
Scope залишається TEST ONLY; це не дозвіл міняти офіційного бота чи deploy.
Перед роботою read-only main 005897b0a93c7fe0166424341c0564a5fbaf94cb,
Render dep-dav2570473hc73d7bkj0 live з тим самим commit,
finishedAt 2026-10-01T09:07:41.772737Z. Main/service/env/jobs не змінювалися.
Remote WIP база 7c57d99b2265e49b603e1d947fe7aa85098cb485,
tree 095f7f4b3b0997de36582b92c3f30492c0c77cd8. 29 файлів відновлено
через GitHub connector зі свіжого snapshot і звірено за Git blob SHA.
AGENTS.md у remote tree немає. Прочитано актуальний stage-6 handoff.

## Завершений крок

Новий `owner_harness.py` відокремлено від legacy stage-6 harness/preview.
`/client` і `/admin` містять лише статичну форму входу та shared UI;
жодних bootstrap session tokens. `/fixture-config.js`, `/connected.js`,
DB/source files, production paths не видаються. Перемикачі ролей
прибрані з owner UI; реальна авторизація кожної дії на сервері.
Synthetic учасники: client 111, owner-admin 777292211. Ніяких реальних
клієнтів/Telegram identity, банків або нарахування чинного bot access.

`local_auth.py`: серверні 32-byte random одноразові запрошення, 15 хв
на обмін; bearer session у JS RAM, 1 година абсолютного TTL. У DB
лише SHA-256 hashes, uid/role/час і revocation. Web body не вибирає
uid/role/clock/tariff. Rotation скасовує старий ключ, не продовжує строк;
logout і revocation зберігаються після restart. Позитивний integer clock
читається після BEGIN IMMEDIATE; high-water mark фіксується навіть при
failed expiry/authorization. Clock rollback не відновлює стару владу.
Savepoint відкочує failed auth operation без відкату high-water mark.
Максимум 32 активні сесії/32 запрошення; terminal/expired hashes prune,
невідомі старі токени fail closed, чинні сесії не витісняються.

CLI пише локальний операторський файл `*.local-invitations.json` із
правами 0600, не друкує коди; файл і DB ігноруються Git. DB 0600,
symlink DB/операторського файла не приймається. Login/refresh потребує
свого one-use коду; немає cookies/localStorage/sessionStorage. Це модель
локальної довіреної видачі кодів, не production identity/auth deployment.

`receipt_store.py`: raw PNG/JPEG/PDF до 2 МБ, MIME+signature check.
Файл+review revision+audit+outbox пишуться однією SQLite транзакцією.
Ledger receipt transition винесено в `_submit_receipt_locked`; legacy
submit лишається через ту саму логіку й свій BEGIN IMMEDIATE. Exact
bytes retry для поточної review receipt ідемпотентний, clarification
дозволяє повторно подати той самий файл із новою ревізією. Callback/
outbox failure відкочує bytes і перехід разом. Немає orphan staging
directory/довільних names, paths або URL. Global 20 files/20 МБ, до 5
на order: reject on full, evidence/financial state не видаляється.
Ці ліміти не обмежують усю історію orders/audit/outbox/source flags.

Файли — quarantine, не proof of payment або malware/decoder validation.
Дані synthetic fixture; не зберігати справжні банківські/клієнтські
документи. Encryption at rest, scanning та retention/delete policy
для реальних файлів ще відсутні. Admin-only download повертає точні
bytes як `application/octet-stream`, controlled attachment filename,
nosniff, CSP sandbox; client/anonymous не можуть download навіть із ID.
Receipt metadata сама не активує membership; approve потребує окремої
admin-сесії, exact amount/reference та checkbox ручної fixture звірки.

Shared UI у file mode показує picker/download/payment reference і скидає
bank checkbox при зміні review revision. Reference стабільний для retry
одного order, адміністратор може його відредагувати. Lost approval HTTP
response -> повтор із тим самим reference -> один збережений строк,
без повторного нарахування. /stop guard лишається false; epochs,
sent/uncertain fixture claims не змінюються. Старий preview stage-6 RAM
не оновлено/не видавати його за цей підключений owner тест.

Transport only same-origin loopback. Bind 127.0.0.1; strict Host/Origin/
Sec-Fetch-Site/custom header, raw upload authorization до читання bytes
і повторно перед write. JSON <=8192, file <=2 МБ, exact Content-Length,
chunked/partial/oversize відхиляються. 5 s read timeout, максимум 8
handler threads, overload 503. no-store/nosniff/no-referrer/CSP self,
немає HTTP credential/body/name logs або outgoing API. Не публікувати
цей сервер або operator codes і не змінювати bind на public address.

## Перевірки

- 90 Python tests passed: усі попередні 62 + 9 durable-auth + 8 file-store
  + 11 owner HTTP. Exact expiry/failed-check rollback, invitation/session
  capacity, hash-only DB, one-use parallel exchange, rotation/revocation
  restart, file ownership/budgets/retry/atomic rollback/restart, partial
  body, auth before upload, attachment bytes, client admin denial,
  production routes/Host/Origin guards, unchanged /stop/epoch/claims.
- Фінальний `check-offline.py`: 90 tests, 9.674s, passed; outgoing
  non-loopback TCP і forward/reverse DNS fenced, loopback HTTP дозволено.
- `check-owner-ui.py` passed: фактичний `owner-login.js` і shared UI
  listeners -> raw HTTP -> LocalSessions -> ReceiptStore/Ledger -> SQLite.
  Різні client/admin logins, missing file/checkbox, wrong amount,
  download, clarify/resubmit, rotate/logout/private DOM reset і client
  action denial. Окремо відтворено успішний approval із втраченою HTTP
  відповіддю; retry не продовжує membership вдруге.
- Legacy `check-ui.py` passed: stage-6 RAM fragment і actual legacy
  connected UI -> offline SQLite, busy guard/reject/renewal.
- Legacy `test_workflow.cjs` passed.
- CLI `--issue-only` smoke passed: приватний operator file 0600,
  правильні дві ролі, жодного token value у stdout/stderr і no server.

DOM event fixture мінімальний; це не реальний browser render, file picker,
object URL/download engine або accessibility QA. Playwright Chromium
раніше був відсутній; browser check тут не виконували/не приписуємо.
Це повний regression прогін **цього isolated prototype**, не backend
production suite й не доказ готовності merge/deploy/real payments.

## Наступний крок і незмінні межі

Offline contract entitlement gate: active/expired/renewed/rejected,
late payment, /stop/epoch/sent/uncertain before admission і повторна
перевірка before fake dispatch; не чіпати production searches/delivery.
Потім reconciliation claimed/prepared/uncertain та admission/retention
source records. Реальна owner-only доступна з телефону test address/
identity, privacy/scanning/retention та transport для receipt files
ще потребують окремого рішення, без public bootstrap/двох рольових ключів.

Для реальних оплат не затверджено тариф/отримувача/review-refund умови й
модель сумісності з правилами Telegram. Це не блокує offline work.
Ніяких реквізитів, переказів, Telegram повідомлень, AUTO.RIA requests,
ресурсів або deploys; main/Render/env/runtime accounting й інші WIP
гілки не змінювати. Не створювати нові automations і не відновлювати shadow.
Перед GitHub записом перевірити актуальний WIP head; push лише non-force
із повним збереженням concurrent changes, remote tree звірити із тестами.
