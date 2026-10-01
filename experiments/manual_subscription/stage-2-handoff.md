# Етап 2 — локальний UI прототип, 01.10.2026

Перед роботою remote WIP head перевірено:
c1a292eeedd92aeb10f8a34ba1bc75f05bec70c6. SHA кожного з трьох
попередніх локальних файлів звірено з актуальними GitHub blobs.

Додано demo.html та workflow.js: адаптивний український макет двох ролей,
клієнтська квитанція та admin review. Вибраний файл не читається й не
завантажується: у демо використовується тільки його назва. Реквізитів немає;
249 грн — synthetic fixture, не погоджений тариф. Обидві ролі показуються
разом тільки в локальному огляді. Немає аутентифікації чи production API.

У демо: awaiting -> review -> approved/rejected/clarification; уточнення
можна виправити новою назвою тестової квитанції. Квитанція не дає доступ;
approve потребує тестового bankVerified, точного amount та synthetic reference.
Approve приховується після завершення, повторна дія відхиляється. Назви
файлів, нотатки та повідомлення виводяться textContent, не HTML-вставками.
Без fetch, cookies, storage, зовнішніх assets, платежів та доставки.

Показаний строк у Kyiv. Реальний Ledger продовжує працювати окремо;
workflow.js — ізольована модель UI. Перезавантаження скидає макет, тоді як
SQLite Ledger окремо перевірено на restart/concurrency. Не видавати UI за
панель, що зберігає заявки чи керує доступом користувачів.

Перевірки:
- `node experiments/manual_subscription/test_workflow.cjs` — passed;
  стани receipt/clarification/resubmit/reject, wrong amount, missing bank
  verification, reused reference, duplicate approve, active renewal та
  immutable previous state.
- `python -m unittest discover -s experiments/manual_subscription -v`
  — 12 passed, 0.016 seconds; попередній облік не змінювався.

Рендер у браузері/візуальний QA ще не виконували; DOM event integration
не покрита цим Node прогоном. Для огляду завантажити demo.html та workflow.js
в одну папку і відкрити HTML локально; не публікувати. Значення на екрані
штучні та не є поточними клієнтськими даними.

Наступний крок (етап 3): durable clarification/resubmit у Ledger, однакові
правила UI/обліку, clock/reference validation, bounded receipt metadata,
offline outbox та entitlement зі /stop/epoch/sent/uncertain claims.
Потім auth boundary та інтеграційні перевірки. Main/Render не змінювалися,
Stars pilot та discovery WIP не чіпалися, deployment не створювався.
