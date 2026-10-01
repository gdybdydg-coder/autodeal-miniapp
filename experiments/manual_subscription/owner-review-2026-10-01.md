# Перевірка власником і наступний тест із телефона — 01.10.2026

## Повідомлений результат

Власник о 20:15 Europe/Kyiv після показу RAM демонстрації написав:
«Все, підтверджено». Це підтвердження проходження показаного сценарію,
не новий банківський платіж, не дозволений deployment і не підтвердження
активації абонемента в production.

Показано актуальний inline snapshot з WIP
415c27c7de4152b68a572cfa017c6a397d4d0ede: 250 грн / 30 днів,
клієнтська заявка -> synthetic receipt -> ручне fixture-admin approval.
Окрема read-only перевірка цього head, main і live deployment виконана.
Нових runtime тестів цей feedback не замінює. Попередній stage-9 доказ:
103 Python tests, UI/HTTP/SQLite, RAM/inline та workflow; не називати їх
новим прогоном або реальною перевіркою власника через банк/сервер.

Не маємо телеметрії конкретної сесії inline: результат відомий зі слів
власника. Демонстрація живе в RAM і скидається при повторному відкритті.
Її підтвердження не записує рядок до owner harness або production DB.

## Що залишилося для справжнього тесту з телефона

Authenticated owner harness зараз слухає тільки 127.0.0.1. Закритого
HTTPS URL, окремого тестового Telegram-бота та зовнішнього auth transport
ще немає. Не міняти bind існуючого harness і не публікувати його as-is:
це локальна fixture система із synthetic uid 111, не production identity.

Наступний транспорт готувати окремо від чинного main/Render service,
DB, Stars pilot і AUTO.RIA ledger. Власник затвердив отримувача та тариф;
повторно їх не запитувати. Дозвіл «все, підтверджено» не вважати дозволом
створити ресурс/deploy, увімкнути actual collection або Telegram sends.

До доступу з телефона підготувати concrete isolated transport:
- owner-only authenticated client/admin roles, HTTPS, bounded login
  attempts, строки сесій та logout/revocation; токени не в URL/логах;
- окрему test DB з перевіреним restart, не production connection;
- synthetic receipts, MIME/size quarantine, admin-only download;
  не використовувати справжні документи до рішення щодо scanning/retention;
- payments_enabled=false, no outgoing bank/Telegram/AUTO.RIA adapters;
- запуск/зупинку та повторне відтворення без втрати orders/memberships.

Модель оплати/правила платформи, privacy і review/refund правила залишаються
до реальних платежів; цей запис не вводить новий тариф або дозвіл на них.

## Критерії наступної перевірки власником

| Дія на телефоні | Очікуваний результат |
| --- | --- |
| Увійти як клієнт | Лише його тестова заявка; admin actions недоступні |
| Створити заявку | 250 грн / 30 днів, власний order ID, доступ неактивний |
| Завантажити вигадану квитанцію | Стан review; квитанція сама не надає доступ |
| Увійти окремо як адміністратор | Файл квитанції та збережена сума; checkbox звірки спочатку false |
| Подати неправильну суму або не підтвердити звірку | Відмова, абонемент не нараховано |
| Підтвердити exact quote 250 з унікальним synthetic reference | Один test grant на 30 днів |
| Повторити той самий reference/запит | Строк не збільшується вдруге |
| Перезапустити test server і знову увійти | Заявка та строк збережені, revoked session не оживає |
| Продовжити active membership | Нова заявка 250/30; 30 днів додаються до чинного строку |
| Перевірити /stop/epoch/sent/uncertain fixture | Без змін; підтвердження не вмикає зупинений пошук |
| Дочекатися modeled expiry | Доступ неактивний на точній межі без видалення history |

Зараз цей checklist не виконано на реальному телефоні через HTTPS backend.
Повідомляти стан точно: власник перевірив демонстрацію, remote production
незмінний; повний phone/auth/upload/restart test ще попереду.

Main на цьому checkpoint: 005897b0a93c7fe0166424341c0564a5fbaf94cb.
Live deployment: dep-dav2570473hc73d7bkj0, same commit, finished
2026-10-01T09:07:41.772737Z. Запис docs-only у WIP, без merge/deploy.
