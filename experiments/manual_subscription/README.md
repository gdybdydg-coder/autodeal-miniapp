# Ручне підтвердження абонементів — ізольований тест

Актуальний етап — **9**: власник погодив **250 грн за 30 днів** о 19:36
Kyiv 01.10.2026. Нові test orders, authenticated реквізити й admin review
використовують цю ціну. **Реальні перекази вимкнені**. Див. stage-9-handoff.md.
Етап 8 — приватний отримувач; етап 7 — login/files/TTL/revocation.
Старі етапи нижче — історія перевірок. Публічної тестової адреси немає;
робочий бот і реальні платежі не підключені.

Дата: 01.10.2026. Доручення власника о 12:53 Kyiv: працювати поетапно у
фоновому режимі; нічого не додавати до офіційної версії бота.

Remote main до роботи: 005897b0a93c7fe0166424341c0564a5fbaf94cb,
tree 0cc95880c4c2676ac5c0d14a2427ae232ea67bed.
Перевірено окремі WIP heads: owner-stars-live
1f1c3292be091ea83f9533a12a0ced62e9b421af, discovery-coverage
2c5ce7fb94028b9abffae569b2a0acf2a7b4c72f. Ці гілки не змінюються.

## Етап 1: облік і переходи станів

Самостійна stdlib SQLite модель, не імпортується backend чи frontend бота.
Стани: awaiting -> review -> approved або rejected. Квитанція — лише тестове
посилання, не перевірений платіж. Адміністратор окремо подає номер фактично
зарахованого банківського платежу, перевірену суму та підтвердження звірки.
bank_verified=True — моделювання ручного рішення, не інтеграція з банком.

Тариф у тестах 249 грн/30 днів — вигадана fixture за прикладом конкурента,
не затверджений тариф AUTODeal. Немає реальних номерів карток, виписок,
квитанцій, API банків, секретів, Telegram запитів чи AUTO.RIA запитів.

Облік забезпечує:
- окремий номер замовлення і snapshot ціни;
- одну відкриту заявку на клієнта;
- квитанція та «я оплатив» не нараховують доступ;
- admin-only рішення і перевірка належності квитанції;
- один банківський reference не нараховує доступ кільком клієнтам;
- транзакція SQLite BEGIN IMMEDIATE, ідемпотентність дубля approval;
- 30 днів із підтвердження або продовження чинного строку;
- точну межу expiry без видалення замовлень;
- журнал created, receipt_submitted, approved, rejected;
- стан та захист від дубля після перезапуску, чотири конкурентні approval.

Перевірка: `python -m unittest discover -s experiments/manual_subscription -v`
Результат: 12 tests passed, 0.028 seconds. Тимчасові SQLite бази видаляються
після тестів. Прототип не має мережевого транспорту. Production tests не
перезапускали: жодні production файли не змінювалися; ці 12 тестів не є
регресійною перевіркою майбутньої інтеграції.

## Межі

Це модель обліку, не автентифікований API або клієнтська панель. actor/uid
для бібліотеки передає довірений caller; у майбутньому потрібна реальна
server-side auth, а не довіра до ID із форми. Файли ще не завантажуються,
доступ користувачів не обмежується, квитанції не надсилаються адміністратору.
Автоматичних підтверджень, карткових платежів, списань чи deployments немає.

Збереження /stop, epoch та pending/sent/uncertain claims поки гарантується
ізоляцією: у цій моделі цих production сутностей немає. Далі потрібні окремі
offline сценарії entitlement-перевірки без вмикання зупинених пошуків.

Модель карткової оплати цифрового доступу всередині Telegram має відому
несумісність з вимогою Stars. Тестовий код не є дозволом на публічний запуск
і не обходить правила Telegram. Вебмодель і її можливі Telegram-сповіщення
потребують окремого рішення до інтеграції.

## Наступні етапи

2. Виконано у `484797d`: локальний клієнтський/admin UI на synthetic даних.
3. Виконано в поточній WIP-гілці: валідація reference/часу, durable
   clarification/resubmit, offline outbox dedup/restart і guard-сценарії
   `/stop`/epoch/claims без автоматичного ввімкнення пошуку.
4. Виконано: наскрізний offline сценарій create -> receipt -> clarification
   -> approval -> renewal -> expiry, audit/outbox, concurrent create/workers,
   fixture session auth boundary. Див. stage-4-handoff.md. 33 Python тести
   й Node workflow пройшли; це не production auth чи доставка.
5. Виконано у паралельному `be8613e4`: review/readiness та перелік рішень
   власника у stage-5-readiness.md, 3 додаткові isolation tests збережено.
   Прототип готовий лише до огляду, не до реальних оплат або merge/deploy.
5а. Виконано: obsolete notices відсіюються перед claim і одноразовим
    fake-worker preflight; активний outbox обмежений 100 подіями, а
    підтверджений доступ не втрачається при переповненні. Відкладений намір
    збережений у вихідній заявці/membership і відновлюється bounded batch.
    55 Python тестів та Node workflow пройшли. Див. stage-5-handoff.md.
5б. Підсумковий review ще відкритий: auth session lifetime, role/UI wiring,
    reconciliation claimed/uncertain, admission/retention усіх source records
    і конкретний перелік даних від власника. Merge/deploy заборонені.

Ліміт 100 застосовується до `pending + claimed + uncertain` у тестовому
outbox; історія фінансових рішень і source-record flags окремо не обмежені
цим лімітом. Імпортована legacy-черга понад ліміт не видаляється: нові
події чекають до її зменшення. Fake delivered/uncertain не є реальною
доставкою. Робочий бот, Stars pilot і його чинна черга не змінені.

## Етап 6: підключений тестовий екран

`trial.html` + `payment-ui.js` через `connected.js` звертаються до
`harness.py` -> Adapter -> справжнього offline SQLite Ledger цього прототипу.
Тільки 127.0.0.1, synthetic sessions/receipts, без outgoing API, реквізитів
або реальних переказів. Обидві ролі доступні власнику для огляду; це не
публічна auth/admin панель. Докладно: stage-6-handoff.md.

Запуск окремого тесту на комп'ютері:

```sh
python experiments/manual_subscription/harness.py --db ./manual-fixture.sqlite
```

Відкрити показану адресу 127.0.0.1 у браузері на тому самому комп'ютері.
`--db` — виключно нова локальна test database, не production. Без `--db`
сервер використовує тимчасову базу й видаляє її після завершення.
Збережені заявки/строк витримують restart із тим самим `--db`, а старі
RAM fixture sessions недійсні й видаються заново при відкритті екрана.
CLI не створює Render service, публічного URL або Telegram-повідомлень.

Перевірки: 62 Python tests; `python experiments/manual_subscription/check-ui.py`
перевіряє actual UI handlers + actual connected transport через loopback
server/Adapter/SQLite. Це DOM event fixture, не browser rendering engine.
Попередній Node workflow також passed.

`build-inline.py` створює `payment-trial-inline.html` для огляду в чаті:
той самий екран і його event handlers, але RAM `demo-adapter.js` із
JS workflow model замість SQLite/HTTP. Без мережі або browser storage.
Цей варіант скидається при повторному відкритті й не є доказом реального
платежу, durable database чи доступу до робочого бота. 249 грн — тест.

Перед кожним записом перевіряти актуальний head цієї гілки. Main, Render,
runtime jobs, production підписки, Stars pilot та API ledger не змінювати.

## Етап 7: окремий вхід та файли квитанцій

Закритий **локальний** тест на тому самому комп'ютері:

```sh
python experiments/manual_subscription/owner_harness.py --db ./manual-owner-fixture.sqlite
```

Відкрити `/client` і `/admin` на показаній адресі `127.0.0.1:8766` у
двох вкладках. Одноразові коди двох ролей — у показаному локальному
`*.local-invitations.json`, доступ до файла 0600. Значення не друкуються,
не видаються через HTTP, не зберігаються в GitHub. Вхідний код діє 15 хв,
сесія — рівно 1 годину; rotation змінює ключ без продовження дедлайну.
Logout/rotation/expiry/revocation переживають restart у test SQLite.
Це локальні запрошення власника для synthetic uid 111 і admin 777292211,
не реєстрація клієнтів або перевірка Telegram identity.

Нові коди після refresh або для повторного входу:

```sh
python experiments/manual_subscription/owner_harness.py --db ./manual-owner-fixture.sqlite --issue-only
```

Команда лише видає нові локальні запрошення, не запускає сервер або
deployment. Попередні невикористані коди діють до свого дедлайну; загальний
ліміт 32 запрошення/32 активні сесії. Без `--db` тестова база тимчасова й
видаляється при завершенні сервера. Лише новий test file, не production.

Клієнт створює заявку, натискає «Я оплатив», обирає **вигадану тестову**
квитанцію PNG/JPEG/PDF до 2 МБ і надсилає. Файл та перехід до review
пишуться атомарно; повтор однакових bytes після втраченої відповіді не
створює другу ревізію. Квитанція сама не активує абонемент.

Адміністратор входить своїм кодом, завантажує файл як attachment,
перевіряє тестову суму та платіжний reference, окремо ставить checkbox
звірки й approve/clarify/reject. Нова ревізія квитанції скидає checkbox.
Повтор approval із тим самим reference після втраченої HTTP відповіді
повертає збережений строк без другого нарахування. Це ручне fixture
рішення, не автоматична банківська звірка. Тариф 249 грн/30 днів — тест.

Тип перевіряється за MIME+signature. Це quarantine, **не** malware scan,
повний decoder або доказ справжнього платежу. Не використовувати тут
справжні клієнтські/банківські документи. Оригінальний filename не
передається/не зберігається; довільні URL/path не приймаються. Bytes у
локальному SQLite; максимум 20 файлів/20 МБ, до 5 файлів на заявку.
Переповнення відхиляє новий файл, не видаляє evidence або фінансові рішення.
Encryption at rest, retention/delete policy та справжнє сканування відсутні.

In-chat RAM preview окремий від owner harness, без server login/file
storage або приватного отримувача. Stage 9 оновив його до 250 грн.
Його не видавати за authenticated owner harness або реальний платіж.

Перевірки: 90 Python tests; legacy `check-ui.py` і `test_workflow.cjs`;
`python experiments/manual_subscription/check-offline.py` запускає весь
Python набір із забороненими non-loopback TCP/DNS з'єднаннями.
`check-owner-ui.py`: actual owner-login/UI listeners -> raw HTTP upload ->
server-issued sessions -> offline SQLite -> attachment download ->
clarification/resubmit -> approval/lost response retry -> logout.
DOM event fixture не є browser render/file-picker/accessibility QA.

Merge/deploy та production зміни заборонені. До реальної інтеграції:
owner-only тестова адреса/identity/auth transport, privacy/retention і
upload scanning, модель оплати/правила Telegram, затверджений тариф та
отримувач, review/refund правила. Далі offline — contract тестового
entitlement gate й reconciliation delivery claims; реальні повідомлення
та ввімкнення зупинених пошуків не дозволені.

## Етап 8: приватні реквізити

Власник надав реквізити о 18:01 Kyiv 01.10.2026. Самі значення зберігаються
поза repository, не в цьому документі, tests, commits або SQLite ledger.
`bank_profile.py` читає тільки явно вказаний файл з правами 0600 поза
git checkout; symlink, завеликий файл, duplicate fields, invalid IBAN/
recipient fields і спроби ввімкнути collection відхиляються.

```sh
python experiments/manual_subscription/owner_harness.py \
  --db ./manual-owner-fixture.sqlite \
  --recipient-profile ../autodeal-test-recipient.private.json
```

Шлях до скачаного приватного профілю потрібно вказати фактичний, поза
checkout. Профіль не завантажується автоматично, без нього старий тест
продовжує працювати без реквізитів. Файл не є HTTP asset; не копіювати
його в GitHub. `*.private.json` додано до experimental .gitignore.

Schema version 1: recipient_name, recipient_code, iban, bank_name,
mode=test_only, tariff_confirmed=false. IBAN нормалізується до uppercase
без пробілів, перевіряються Ukrainian 29-char structure і MOD97 checksum.
Це перевірка формату, не existence/balance/beneficiary bank verification.
Джерело структури: офіційна сторінка НБУ https://bank.gov.ua/ua/iban.
Банківські реквізити власника нікуди для перевірки не надсилалися.

Реквізити повертаються лише в authenticated `/api/state` після створення
конкретного order. Purpose містить order ID та days snapshot; amount
також із заявки. UI вставляє значення через textContent; копіювання
IBAN/purpose — тільки на явний click. Якщо clipboard API недоступний,
показує помилку та дозволяє скопіювати видимий текст вручну.

На момент stage 8 власник ще не затвердив ціну: **249 грн/30 днів** була
fixture. **Stage 9 замінив поточну ціну на погоджені 250 грн/30 днів**;
старі orders залишаються за своїм snapshot. Profile/API не можуть
увімкнути real collection, екран зберігає «Не переказуй кошти». Ніяких QR,
payment links, банк API, реальних переказів або повідомлень бота.

101 Python tests passed із non-loopback TCP/DNS fence; actual owner UI
-> HTTP -> SQLite passed, додано recipient render/copy-content checks.
Legacy UI/workflow passed. Actual private owner profile smoke passed:
expected authenticated instructions, collection disabled, no values in
SQLite DB або source. Tests містять лише спеціально non-routing synthetic
IBAN з нульовим кодом установи/рахунком, а не дані власника.

Ціну абонемента власник надав у stage 9: 250 грн за 30 днів. Збір коштів,
public/phone preview, payment-model/platform рішення та production
changes не дозволені цим додаванням приватного профілю. Робочий paid bot
і owner Stars pilot лишаються без змін.


## Етап 9: погоджений тариф 250 грн / 30 днів

Доручення власника 01.10.2026 19:36 Kyiv: «Давай 250 грн» у відповідь
на питання про ціну 30-денного абонемента. Це погодження ціни, без
дозволу на production, deployment або фактичний збір грошей.

`tariff.py` задає поточний trusted quote: 250 UAH / 30 днів. Adapter і
Harness користуються ним; client body не може замінити amount/days.
Заявка зберігає price/duration snapshot. Старі open/review заявки по
249 не переоцінюються, підтверджуються лише за збереженою сумою в
локальному fixture; наступне продовження створює новий quote 250.
UI показує snapshot поточної заявки, а кнопка нової — новий тариф.
Після нової квитанції або заявки checkbox звірки скидається.

Приватний recipient JSON schema v1 сумісний без змін: його legacy
`tariff_confirmed=false` не є джерелом погодження ціни та забороняє
профілю змінювати тариф. `payment_instruction.tariff_confirmed` визначає
сервер за snapshot 250/30. `payments_enabled=false` у всіх варіантах.
Реквізити лишаються поза GitHub і ledger.

103 Python tests passed із non-loopback TCP/DNS fence; owner UI ->
HTTP -> SQLite, legacy UI, regenerated inline RAM та workflow passed.
Old awaiting/review -> restart -> wrong amount rejection -> approve
old quote -> renewal 250 -> exact expiry, /stop/epoch/claims пройшли.
Реквізити власника smoke-tested локально без логування значень або
банк API; у test DB і джерельних файлах їх немає. Це мінімальна DOM
event fixture, не browser visual/accessibility QA.

Далі можна готувати offline entitlement admission/preflight та
reconciliation uncertain/claimed notices. Публічної тестової адреси,
реальних переказів і зв'язку із робочим ботом ще немає. Ціна більше
не є blocker; auth transport, privacy/retention, review/refund правила
та payment-model/platform рішення залишаються до реальної інтеграції.
