# Приватний тест Stars — 01.10.2026

Власник прямо доручив підключити тест оплати до чинного бота, щоб самостійно
сплатити й користуватися ним. Це вузький дозвіл на цей тест, а не на перенесення
чернетки безплатного пошуку чи виправлень discovery у production.

База: remote main 901d96fe1f7e10196155ef6dd329da9d62f2a250.
Локальний snapshot відновлено за Git blob hashes актуального remote tree.
Render до змін: live dep-daumfo59fdbs739haga0, той самий main; autoDeploy=yes.

Приватний Telegram ID: 777292211 (наданий користувачем). Немає глобальної реклами
оплати, зміни загального меню чи платного обмеження інших користувачів.

Команди в особистому чаті:
- /paytest — умови: реальна разова оплата 1 Stars, 30 днів тестового доступу.
- /paytest_confirm — згода з умовами та створення рахунку.
- /payment — статус збереженого тестового доступу.
- /refundtest — повернення останньої підтвердженої тестової оплати.
- /paysupport — допомога та шлях звернення у чат розробки.

Окрема нова таблиця stars_test_orders створюється існуючим startup create_all;
існуючі таблиці не змінюються. Unique command_update та charge_id, блокування
рядка PostgreSQL захищають повторні події; successful_payment після expiry
рахунку все одно фіксується. Precheckout тільки для власника, точного payload,
XTR та строго цілої суми 1; 15 хвилин дії рахунку. До successful_payment немає
доступу; після повернення paid-state не відновлюється повторною старою подією.

Webhook залишається захищеним секретним заголовком та 64KiB лімітом. Додано
pre_checkout_query до allowed_updates без drop_pending_updates. Рахунок,
checkout-answer і повідомлення повертаються офіційним способом Bot API у JSON
webhook-відповіді: результат виконання такого методу не доступний серверу.
Якщо рахунок не з'явився, власник може повторити команду новим повідомленням;
ми не повторюємо той самий update. Статус платежу доводить лише отриманий
successful_payment, а не доставку повідомлення на телефон.

Refund спочатку durable claim refund_sending, потім один Bot API виклик з
timeout=5; невизначений результат не повторюється автоматично. Офіційний
refunded_payment може завершити цей стан. При crash після claim стан потребує
ручного звірення; гроші повторно не списуються. Зберігаємо payment charge_id
лише в БД для повернення, не логах. Не читаємо server secrets чи env.

Тестовий доступ є окремим платіжним станом, не глобальним paywall. Він не
змінює /stop, User.ready, epochs, Search.enabled, sent/uncertain claims,
delivery deduplication чи AUTO.RIA ledger/cadence. Після оплати зупинені пошуки
власник вмикає самостійно. Спільне оцінювання та підтверджені вигідні оголошення
залишаються в чинному платному режимі.

Не production billing: немає тарифів для інших користувачів, автопродовження,
пробних періодів чи обмеження пошуку за оплатою. Максимум 20 рахунків приватного
тесту. Жодна оплата не ініціюється асистентом; власник підтверджує 1 Stars сам.

Перевірки запускаються із socket/DNS fence, без Telegram/AUTO.RIA мережі;
тестову onboarding fixture виправлено, щоб startup configure_menu не використовував
HTTP з фіктивним токеном. Повна регресія: `python -m pytest backend/tests -q` —
974 passed, 1 existing Starlette/httpx deprecation warning, 144.73 seconds.
Окремо перевіряються auth, private scope, чужі користувачі,
підмінена сума/валюта/payload, expiry, late payment, duplicate charge, refund,
restart/session persistence, /stop і незмінний ledger.

Офіційна документація, перевірена 01.10.2026:
https://core.telegram.org/bots/payments-stars
https://core.telegram.org/bots/api#making-requests-when-getting-updates
https://core.telegram.org/bots/api#refundstarpayment
