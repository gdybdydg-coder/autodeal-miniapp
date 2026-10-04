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
