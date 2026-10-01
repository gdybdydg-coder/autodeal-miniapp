# Ручне підтвердження абонементів — ізольований тест

Актуальний етап — **7**: окремі локальні входи клієнта/адміністратора,
файли квитанцій, durable TTL/rotation/revocation. Див. stage-7-handoff.md.
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

Stage-6 in-chat RAM preview лишається окремим старим snapshot, не має
цього server login/file storage. Його не видавати за новий owner harness.

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
