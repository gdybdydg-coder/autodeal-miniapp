# Ізольоване дослідження OLX — 04.10.2026

База: afaf9cee348db64f0558e5d113b2895c48366421. Production read-only.
Заборонені main/deploy/restart/production DB/config/Telegram/paid API changes.
Гілка: research/olx-isolated-fx-market-20261004. Render auto-deploy main;
PR previews off; існуючий CI не підписаний на цю дослідницьку гілку.

| Етап | Уже є | Реалізація / залежності | Результат і перевірка | Завершення |
|---|---|---|---|---|
| А. Аудит | Парсери, eligibility, FX НБУ, market, SQLite queue, owner batch | Перевірити live SHA, diff і branch triggers | Read-only Render + git; backend без змін | Зафіксована база й межі ізоляції |
| Б. Джерело | Публічний HTML; Partner API лише власні adverts | Невелика актуальна вибірка; офіційні docs/terms | HTTP/status/повнота, сортування/пагінація, журнал GET | Показано доступний канал і його межі; licence/newness окремо |
| В. Характеристики | Search/detail adapters, conflict detection | Детальні причини пропусків; підтвердження покоління лише джерелом | Повна сторінка, ID, source fields; невідомі не вгадуються | Нормалізовані реальні записи з provenance |
| Г. Відсів | UA/RU customs/parts/donor, заперечення | Повторно використати; неоднозначне зберігати | Контроль цілого ремонтного авто і заборонених пропозицій | Жодне відоме заборонене не проходить тести |
| Д. FX | Decimal, dated NBU, локальний cache | Перевірений резерв + один basis на cohort; timeout/cache policy | NBU/fallback/cache/all failed/USD/units/date/anomaly tests | Реальний резерв або конкретний доказ недоступності; replay усіх переходів |
| Е. Оцінка | Median/Q25/weighted, строгі matching/dedup | Trimmed mean, прозорі причини й independent evaluation; дані з Б–Д | Однакові cohort для методів; holdout labels окремо | Немає вигаданого reference; coverage/errors відділені від арифметики |
| Є. Фільтри | Backend mapping + missing optional policy | Explanations; known mismatches до detail cap | Bad-first candidates не витісняють наступні придатні | Доведений regression test попереднього відбору |
| Ж. Дублі/втрати | Source+ID, delivery+user, SQLite leases | Перевірити restart/shared detail cache/partial page | Відсутність повторів і cursor advance за неповної видачі | Durable state на tmp SQLite, без production |
| З. Повний потік | Fake sender harness | Ізольований CLI + FX + assessment + card previews | Кілька paid/unpaid/stop, відновлення, partial/source errors | Replay без мережі та імпорту backend; реальні дані позначені |
| И. Погоджений тест | Owner-only bounded worker у поточному main вимкнений | Підготувати мінімальний diff/ліміти/stop/runbook; НЕ активувати | Review → явне погодження → owner test → окремо clients | Лише підготовлений пакет; запуск після нового дозволу |

Порядок: джерело/ціна/оцінка → прототип → ізольований replay → результати.
Перед польовими перевірками: не більше 18 OLX GET / 64 MiB / 20 хв;
не більше 3 currency data GET (по одному провайдеру), без retries/redirects;
OLX пауза щонайменше 4 с; 401/403/429 припиняють звернення до цього джерела.
Одна недоступна detail не скасовує інші записи; incomplete page не complete.
Сирий HTML лишається поза git; публікуються тільки мінімальні факти без контактів.
Курс і метод оцінки є пропозиціями для погодження, не production-політикою.

## Статус після виконання

| Етап | Поточний статус | Конкретний результат / незавершене |
|---|---|---|
| А | Завершено | Live/main afaf9ce, branch deploy triggers перевірені; production незмінний |
| Б | Частково | 15 GET, 11 повних чинних details; regular production permission/newness не доведені |
| В | Частково | Розширено явні A5/body/condition observations; не вгадується покоління інших моделей |
| Г | Перевірено в межах тестів | Чинне нерозмитнене авто відхилено; UA/RU/negation/repair regression успішні; незалежна точність невідома |
| Д | Готово до перегляду | НБУ403 → Privat NBU44.8333; mono44.9999 доступний; Decimal/cache/failure tests; policy ще не погоджена для production |
| Е | Частково | Median/trimmed mean/weighted; 0/11 real coverage; максимум2 peers; independent labels0 |
| Є | Ізольовано виправлено | Pre-cap dedup/filter + явні reasons; regression сценарій пропущеного десятого кандидата |
| Ж | Ізольовано перевірено | Source+ID/user+listing, shared cache, restart0 duplicate previews, partial cursor guard; distributed coordinator ще не production-ready |
| З | Виконано ізольовано | 20 local previews для2 fake paid,0 unpaid; restart0; Telegram0; це не live доставка |
| И | Матеріали підготовлено | README/runbook, diff, FX policy, джерела та наступні рішення; owner test не дозволений поточним файлом |

## Continuation authorized 04.10.2026 23:23 Kyiv

Input: complete attached 20261004-202313 instruction. Stages1–8 authorized;
stages9–10 and every production mutation remain forbidden. Baseline main/live
afaf9ce, research07a7401; historical branches verified at37510df/dcb4af8/6e5506e.
Initial repeat verification:346 tests+150subtests; no backend app or network.
Reuse parsers/eligibility/FX unmodified unless a reproduced defect requires an
isolated adapter. Improve sufficient-cohort/stability/holdout evaluation and
persisted candidate continuation. New foreground source budget, separate from
the earlier completed run:30GET/96MiB/20minutes,4second pauses, no redirect/retry
or bypass; source401/403/429halts. Every actual request is durably recorded.
Target first group: explicit Skoda OctaviaA5,1.9diesel/manual/wagon, matching
year/mileage/condition; previously observed cohort motivates selection. Do not
weaken matching thresholds to increase coverage. Control split must be fixed
before reading prices or tuning methods; held-out asking price is only a
predictive benchmark, never a sale-price truth. Raw pages stay outside git.

## Контрольна точка 2026-10-05T00:06:28+03:00

| Етап | Стан | Перевірений результат / межа |
|---|---|---|
| 1. Основа | Виконано | Live/main afaf9ce; окрема гілка та SQLite, без backend app/config/sender |
| 2. Джерело | Частково | 413 ID, 42 деталі; пагінація і рух видачі перевірені. Регулярний дозволений канал, повнота й первинна новизна ще не доведені |
| 3. Характеристики | Перевірено на вибірці | UA/RU виправлення; 1 нерозмитнене, 1 контекст першого внеску; невідомі дані явно збережені |
| 4. FX | Ізольовано перевірено | НБУ→Privat NB→Mono reference→cache; 3 старі реальні UAH replay. Нова дата потребує придатного курсу |
| 5. Аналоги | Не завершено | Дві цільові групи, максимум 6 сумісних аналогів при мінімумі 8; фізична незалежність невідома |
| 6. Оцінка | Не завершено | Три методи, frozen holdout і stability код готові; реальне покриття 0/42, порівнюваних контрольних оцінок 0 |
| 7. Локальний потік | Виконано в межах прототипу | 42 stored +371 pending; 72 локальні картки для 2 fake paid, 0 unpaid; restart без повторів; Telegram 0 |
| 8. Матеріали | Підготовлено | Код, мінімальні збережені факти, журнал звернень, випадки, команди й залишкові задачі |
| 9–10. Реальні відправлення | Не запускалися | Потрібні нові окремі погодження, та спочатку готовність джерела й оцінки |

Фактичні totals:59 GET /111077583 bytes /42 details; нижчі цифри вище — попередні контрольні точки. Деталі: STAGES-1-8-20261004.md. Етапи5–6 залишаються незавершеними.
