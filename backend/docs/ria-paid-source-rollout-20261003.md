# AUTO.RIA: API лише для оплачених клієнтів, 2026-10-03

Власник окремо дозволив розгорнути виправлення витрат і уточнив: запити мають
витрачатися лише на оплачених клієнтів. Це продовження підготовленого
`8250da72cad86425872f79a538fb37fa7841dfa8` у `wip/ria-api-audit-20261003`.
Історичний аудит, синтетичний replay та обмеження старих вимірів збережені.
Факт запуску підтверджується Render deploy, release у `/health` і новим
агрегованим startup log, а не лише наявністю цього документа.

## Чинна політика

Production entry point `backend.app:factory` завжди вмикає
`autodeal_confirmed_paid_sources_only` на спільному SQLAlchemy Engine.
Перевірені службові/тестові облікові записи виключаються тим самим
`purchase_stats.excluded_user_ids(settings)` із конфігурації; жодних
нових чи вгаданих ID немає. Навіть approved тестовий запис не є дозволом
на production API-роботу. Список зберігається в execution options Engine
і застосовується до bulk SQL, request gates і доставки.
Дозвіл на оплачувану роботу потребує одночасно:

- заявки `manual_payment_requests` зі станом `approved`, додатною сумою,
  валютою UAH, додатним строком і `expires_at > now`;
- чинного `billing_entitlements.expires_at > now`;
- для моніторингу: користувач ready, пошук enabled і поточний epoch;
- для фонового full scan: користувач ready і власна чинна scan lease.

Заявка або квитанція без підтвердження, подарований доступ, legacy Stars pilot,
старий оплачений строк та вимкнення billing enforcement не дозволяють API-роботу.
Історичний покупець не прирівнюється до чинного оплачуваного строку.
Платіжні та subscription записи ця політика не редагує.

`create_app(..., paid_source_only=False)` залишено для явно створених
ізольованих/legacy застосунків; Render використовує перевірений production
factory, який завжди передає `paid_source_only=True`. Це не перевірка назви
тесту чи секретів. Кожен production worker отримує ту саму політику.

## Де перевіряється оплата

Bulk SQL застосовано до discovery feeds, interests і jobs. Усі фільтри та
pending jobs неоплаченого клієнта зберігаються, проте не авторизують source work.
Чинність повторно перевіряється до кожного uncached оплачуваного request,
включно з detail, AI valuation і довідниками. Додатковий global gate у
`RiaSearch` забороняє оплачуваний виклик, коли немає жодного чинного покупця.
Цільові gates не дозволяють неоплаченому клієнту користуватися оплатою іншого.

Mini App search/start/enable, cold catalog, full scan та owner photo repair
застосовують ту саму політику. Теплий спільний catalog cache не створює
provider request. Відмова на cold cache — HTTP 402 без резервування квоти.
У `/api/billing/status` доступ відображає цю фактичну production політику.

Доставка перевіряє чинну оплату та `/stop` перед відправленням. Зняття оплати
між detail і valuation блокує valuation та delivery, зберігаючи pending work.
Присутність інших оплачених клієнтів не авторизує неоплаченого адресата.

## Автоматичні платні перевірки запуску

У strict production режимі не запускаються connectivity/search probes,
catalog rollout probe, valuation audit, listing/date/VIN diagnostics,
AI probe та фоновий `ria_validation_run_id` сценарій. Відключено й повторний
selected listing diagnostic у normal monitor tick. Штатний моніторинг для
чинних оплачених клієнтів продовжує працювати за чинним розкладом.

Оплата, сума 250 грн/30 днів, підтвердження власником, AUTO.RIA формула,
фільтри, `/stats`, OLX та рекламні кампанії не змінювалися.

## Перевірки та межі

Перші 17 strict-production сценаріїв і 43 quota regression тести:
**60 passed**, 6.53 с, без зовнішньої мережі.
Перевірено двох оплачених та одного неоплаченого, спільний API пошук/detail/AI,
HTML publication fan-out, подарований/pending/expired/revoked/pilot доступ,
disabled enforcement, expiry або `/stop` між етапами, paid full scan,
cold catalog за наявності іншого покупця, factory і відсутність платних probes.

Повна перевірка перед цим: 1476 passed, 1 старий fixture failure за 230.73 с.
У `test_quota_management` виправлено лише ізольовану схему: `/stats` читає
історію покупок, тому потрібні ManualBase таблиці навіть без увімкнених продажів.
Assertions не послаблено, код `/stats` не змінено; цей тест повторно пройшов.
Остаточний повний прогін першого rollout: **1479 passed**, 235.79 с.
Після перевірки живого запуску додано 18-й strict сценарій: approved запис
службового/тестового користувача, виключеного конфігурацією, не створює
API-викликів чи доставки. Результат прогону цього уточнення та фактичні
production агрегати зберігаються в окремому звіті запуску.

SQL для paid members, interests та scan predicates компілюється діалектом
PostgreSQL. Це не видається за перевірку всіх PostgreSQL concurrency сценаріїв.
Новий ledger `ria_api_attempts` додається штатним `Base.metadata.create_all`
без зміни колонок або історії наявних таблиць. Failure запису reservation
блокує HTTP. Ledger доводить нові спроби, а не число списаних provider credits.

## Перевірка живого запуску

1. Перевірити, що main не змінився після baseline `2d94954`.
2. Опублікувати перевірений child commit у research branch; workflow не має
   trigger для цієї гілки, Render стежить лише за main, previews off.
3. Fast-forward main без force. Спочатку перевірити фактичний auto deploy.
   Перший rollout: через 148 с нового deploy не було попри autoDeploy on;
   один manual deploy `dep-db0du2mgekts7398ret0` запущено після перевірки черги,
   live о 2026-10-03 10:56:41 UTC. Не запускати manual deploy поверх queued/building.
4. Дочекатися live, перевірити `/health`: release exact SHA, database connected.
5. Прочитати `Paid source access guard` startup aggregate: enabled,
   current paid clients, paid/excluded ready searches, paid filter fingerprints,
   automatic paid startup checks false. Ніяких ID чи реквізитів у новому log.
6. Перевірити штатні app logs на SQL/startup errors. Не викликати платний пошук
   заради тесту, не відправляти тестові картки клієнтам, не читати source-status.

## Відкат

Не повертати старий `2d94954` без paid guard: це відновить дефект витрат.
Якщо потрібне виправлення, окремим code deployment залишити строгий gate та
skip платних probes, а проблемні зміни обліку/retry/cache відкотити вибірково.
Нову telemetry table зберегти. Платіжну історію та строки доступу не переписувати.
Зміна environment або ручна міграція production даних для цього rollout не потрібна.
