# Етап 4 — наскрізний offline сценарій і межа auth, 01.10.2026

Remote production main 005897b0a93c7fe0166424341c0564a5fbaf94cb.
Remote WIP перед початком: 09588faedeebb8471692f360bdb45afbd91204c9.
Фонова робота вже завершила етап 3: усі її зміни прочитано та відновлено
в новому локальному snapshot; кожен файл звірено за Git blob SHA.
Працюємо поверх етапу 3; не повторювали та не затерли його результат.

Додано adapter.py: offline FixtureSessions і Principal + facade з явною
межою ідентичності. uid/actor береться тільки з session principal, не
payload; тариф задається в adapter configuration, не клієнтській формі.
create/submit/clarify/approve/reject приймають лише документовані fields;
status перевіряє власника заявки, review queue — admin session.

FixtureSessions.issue() — лише локальний test setup. Токени синтетичні,
генеруються у RAM і не друкуються. Це НЕ Telegram login, не HTTP API,
не production authentication; справжня session identity ще не реалізована.
Actor-guard у Ledger залишився defense-in-depth модельного рішення.

Додано 6 інтеграційних тестів:
1. Full lifecycle, restart, точний audit/outbox, /stop, renewal та expiry.
2. Невідома session, client-as-admin, чужа заявка, підміна uid/actor/price/
   expiry у form payload — без зміни доступу.
3. Відкликана fixture session не дозволяє admin reject.
4. Чотири конкурентні create повертають одне відкрите замовлення.
5. Конкурентні workers отримують різні claims; crash після claim не
   повертає їх у pending після restart.
6. Інший worker не може підтвердити чужий claim.

Перевірки:
`python -m unittest discover -s experiments/manual_subscription -v`:
33 passed, 0.068s (27 існуючих + 6 нових інтеграційних).
`node experiments/manual_subscription/test_workflow.cjs`: passed.
Без мережі, банку, Telegram або AUTO.RIA. Claims sent=13/uncertain=2 та
epoch=5 після synthetic /stop збережені через approval, renewal, expiry.

Поточні межі: demo.html не підключений до SQLite/adapter; browser DOM і
візуальний QA ще не виконані. Outbox state позначається лише fake-worker
методами, не є фактичною доставкою. 249 грн та всі references — synthetic.
Test schemas не підключаються до production, full production regression
не замінено цими 33 тестами. Main/Render/Stars pilot не змінювалися.

Наступний етап 5: review/readiness. Перевірити насамперед obsolete pending
notices (старе уточнення після approval, expiry notice після renewal),
ліміт накопичення outbox, server-session lifetime/revocation assumptions,
рольові UI межі та міграції. Нинішній lifecycle перевіряє запис усіх подій,
але ще не фільтрує stale payload перед hypothetical send. Не називати
чергу готовою для справжніх повідомлень. Потрібна reconciliaton policy
для claimed/uncertain, справжня auth та окреме рішення щодо моделі оплати
до будь-якої integration. Підготувати короткий handoff/список даних власника;
merge/deploy зараз заборонені. Автоматичні платежі або повідомлення не робити.
