# AUTO.RIA: ізольований аудит доступу та витрат, 2026-10-03

Цей документ фіксує історичний стан підготовки `8250da7` до дозволу на запуск.
Після окремого дозволу власника політику посилено до **лише чинної
підтвердженої ручної оплати**. Див. `ria-paid-source-rollout-20261003.md`;
пункти нижче про подарований/тестовий доступ не описують новий production gate.

Це підготовлена зміна, а не production-виправлення. Оплата, строки доступу,
конфігурація, AUTO.RIA-формула та розклад не змінюються. Реальні платежі,
повідомлення та оплачувані API-запити для аудиту не виконувалися.

## База та межі доказів

Перевірений production commit: `2d94954bd4e93cec9f819526cc984baeff7ed885`,
tree `576876c391ac4573456733cf3a986e1a30db0d3b`.
Локальний baseline commit `c7b8c79314b15311caac625d10e3a2eb2977397a`
має той самий tree; різні commit IDs не приховуються.
Гілка підготовки: `wip/ria-api-audit-20261003`.

Робоча БД не читалася успішно через мережеве обмеження зовнішнього
PostgreSQL-підключення. Поточну кількість клієнтів, доступів і груп не доведено.
Історичні логи не є журналом вихідних HTTP-спроб чи списань провайдера.
Повний приватний кількісний звіт зберігається окремо від репозиторію.

## Підтверджені дефекти та зміни

1. Ready/enabled searches раніше авторизували discovery/evaluation без
   `billing.allowed`, хоча доставка мала перевірку доступу. Bulk SQL predicate
   тепер застосовує ту саму політику до груп, interests, full scan, HTML validation
   та кожного uncached source request. Записи пошуку та epoch не переписуються.
   Pending work може відновитися після поновлення доступу.
2. Холодний `/api/catalog` міг запускати provider requests для authenticated
   клієнта без доступу. Теплий спільний кеш залишається доступним; miss потребує
   чинного доступу до кожного оплачуваного request, інакше HTTP 402.
3. Explicit owner photo repair мав прогалину доступу. Gate доданий до читання
   деталей, edit claim і самого edit; це не надає owner автоматичного винятку.
4. Info HTTP 404/410 раніше повторювався як upstream_error кожні 60 секунд.
   Тепер `manual_review/info_endpoint_unavailable` зберігає авто й evidence.
   API-route failure не видається за доказ видалення автомобіля.
5. Повторні transient detail failures раніше не мали кінцевого бюджету.
   Лише info connection/upstream/invalid-response errors рахуються окремо:
   паузи 60/120/240/480 секунд, п'ята невдала спроба — manual_review.
   Quota/access/busy/catalog failures не спалюють цей бюджет. Успішні parsed
   details скидають consecutive counter; restart не скидає hold або next_run.
6. Exact broadened discovery pages можуть бути повторені різними профілями
   optional-фільтрів. Кеш одного acquired source cycle підготовлений окремо:
   лише однакові повні параметри, включно з датами й page; errors не кешуються,
   наступний цикл примусово оновлює сторінку. Membership/feeds не мігруються.

Подарований та legacy pilot доступ продовжує визначатися чинною
`billing.allowed`; він не прирівнюється до підтвердженої ручної покупки.
Немає нового admin-ID bypass. Вимкнене enforcement залишається чинною
операторською конфігурацією: patch не вмикає його самостійно.

## Новий prospective облік

`api_attempt_audit.py` і proposed SQL migration вводять нову таблицю
`ria_api_attempts`. Міграція не виконана у production.

Запис відрізняє reservation, dispatch, observed transport start, HTTP status,
успіх парсингу/помилку та incomplete state. Cache hit не є новою спробою.
HTTP-вікно визначається за `transport_started_at`, reservation-вікно — окремо.
Повтор після успіху, повтор після помилки та повтор після incomplete не
змішуються; одна спроба належить одній категорії. Credits провайдера невідомі.
Немає raw URL, ключів, параметрів, Telegram/user IDs чи exception text.

Scope: нові `SourceBudget`-операції. Direct one-time connectivity probe
поза цим журналом; його власний counter треба зіставляти окремо.
Давня умовна seed-сума SourceBudget не стає історичним HTTP-доказом.
API callbacks не повторюють HTTP при помилці запису; незавершений запис
залишається невизначеним. Якщо reservation/table transaction недоступна,
новий request не виконується з неправдивим нульовим обліком.

## Відтворення без production-конфігурації

Із checkout, з наявними тестовими залежностями:

```bash
python -m pytest backend/tests -q
```

Tests/conftest.py блокує зовнішню мережу. Не запускати `backend.app:factory`
з production env чи ключем для цієї перевірки.

Однаковий flow у двох checkout, запуск із каталогу поза ними:

```bash
PYTHONPATH=/absolute/path/to/checkout python -c "import runpy,sys; sys.argv=['replay','--label','checkout']; runpy.run_path('/absolute/path/to/ria_cost_replay.py',run_name='__main__')"
```

`backend/tests/ria_cost_replay.py` використовує fake provider/Telegram,
ізольований SQLite і керований clock. JSON відрізняє initial polling від
new-publication flow, catalog, search, detail, valuation і fake deliveries.
Synthetic dispatch не називається реальним HTTP чи provider charge.

Фінальна перевірка всього `backend/tests`: **1461 passed, 1 failed**,
219.65 секунд. Baseline: **1371 passed, 1 failed**, 166.85 секунд.
Єдиний failure в обох версіях:
`test_quota_management.py::test_confirmation_replaces_remaining_preserves_counters_and_spending_in_flight`.
Це давній `/stats` fixture failure; команда не змінювалася в цій гілці.
90 нових тестів пройшли. Три старі scheduling fixtures доповнено валідними
pending interests поточного epoch; assertions пріоритету не послаблено.

Санітизований JSON однакового replay: `ria-cost-replay-20261003.json`.

| Однаковий синтетичний потік | До → після | Перевірка доставки |
|---|---|---|
| Двоє paid зі спільним фільтром + unpaid з іншим | Search 2 → 1 на кожному етапі | 2 paid, 0 unpaid в обох версіях |
| Лише unpaid | 8 → 0 dispatches | 0 accepted в обох |
| Двоє paid з однаковими фільтрами | 8 → 8 dispatches | Обидва збережені |
| Двоє paid з різними фільтрами + unpaid | Search 3 → 2 на кожному етапі | Обидва збережені |
| Постійний info HTTP404, 60 циклів | Detail 60 → 1; усі dispatches 120 → 61 | Hold з evidence, пошук інших авто продовжується |

Це не прогноз добової економії та не живий вимір повноти/затримки.
Hold обмежує марні повтори, але авто з нерозв'язаною помилкою потребує
перевірки оператора; гарантія нульових пропусків не заявляється.

## Що ще потребує підтвердження

- Поточна cohort та активні групи: SELECT-only template
  `ria-api-audit-cohort.sql`, з verified exclusions; query не виконана.
- Фактичні runtime flags старого full scan, active window та diagnostics.
- Квота провайдера на початку/кінці того самого інтервалу та правила debit
  для помилок/AI. Офіційний каталог: https://developers.ria.com/products;
  правила строку пакета: https://developers.ria.com/faq/ (перевірено 2026-10-03).
- Інші непідконтрольні виконавці з тим самим ключем не виключені.
- AI method-wide permission cooldown поки не підготовлено; різні listing IDs
  можуть повторювати заборонену AI-операцію. Це окремий кандидат після вимірювання.
- Single-cycle reuse не є постійним злиттям feed-груп: різні poll windows
  залишаються різними; їхня економія потребує виміру, а не припущення.

## Майбутнє погодження

До запуску: отримати read-only cohort/runtime counters, звірити provider quota,
перевірити Postgres row locks і міграцію на окремому стенді, переглянути hold
policy та узгодити retention нового журналу. У тестах SQLite не доводить
поведінку блокувань усіх PostgreSQL workers.

Лише після окремого дозволу: approved schema → цей конкретний commit →
обмежене спостереження штатного потоку без додаткового paid обходу →
зіставлення однакових UTC вікон/доставок/затримки/квоти.
Відкат: повернути попередній application commit; нову telemetry table
зберегти, не видаляти історію. Розклад, платіжні записи й AUTO.RIA-формула
не потребують відкату, бо не змінювалися.
