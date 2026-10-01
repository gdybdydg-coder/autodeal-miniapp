# Етап 3a — durable уточнення та валідація, 01.10.2026

База WIP: 484797dde46e345b690bd1ec42f10871d890f319. Актуальний main
005897b0a93c7fe0166424341c0564a5fbaf94cb перевірено read-only. Локальні
вхідні файли звірено з remote Git blobs. Main/Render не змінювалися.

У Ledger додано clarify() та owner-only order_status(). Окрема таблиця
clarifications у локальній SQLite зберігає текст/автора/час; не змінюємо
існуючі orders/memberships/audit схеми. Відкритий clarification order
повторно використовується create_order(), після restart показує note;
resubmit повертає review. До нового admin approval доступ не нараховується.
Уточнення можна відхилити; його історія залишається в БД. Status показує
note лише в актуальному clarification state, щоб виправлена заявка не
показувала старе повідомлення. Дублі тієї самої квитанції в review не
додають повторний audit event.

Clock: строго додатне int < 2**63, bool/float/str/overflow відхиляються.
References: обмежена довжина, без leading/trailing whitespace, C0 та DEL.
Receipt reference максимум 200 символів, bank ref 100; note 500 (дозволено
звичайний перенос рядка). Amount mismatch та непідтверджений bank_verified
як і раніше не дозволяють approval. JS workflow погоджено щодо whitespace,
контрольних символів і довжин; це окрема UI модель, не server API.

Перевірка:
- python -m unittest discover -s experiments/manual_subscription -v:
  18 passed, 0.024 seconds;
- node experiments/manual_subscription/test_workflow.cjs: passed.

Нові тести: clarification/restart/resubmit/approval, owner/admin boundaries,
duplicate receipt audit, invalid reference/clock без зміни заявки,
reject clarification зі збереженням історії, additive upgrade старої
локальної SQLite зі збереженим активним абонементом.
Перший локальний прогін виявив name-shadowing нового reference validator
локальною змінною approve(); її перейменовано, потім усі 18 тестів пройшли.

Ліміти: UI не підключено до SQLite; справжня server auth та завантаження
файлів не реалізовані; bank_verified — ручне synthetic рішення; немає
банківських/Telegram/AUTO.RIA API, справжніх квитанцій чи реквізитів.
249 грн лишається fixture. SQLite admin actor параметр не є аутентифікацією.

Наступний крок 3b: bounded durable offline outbox зі claim-before-send,
dedup/restart та uncertain state без повторного надсилання. Offline модель
перевірки entitlement при expiry/renewal має зберігати /stop, epoch,
sent/uncertain claims. Далі auth-boundary facade й end-to-end тести,
візуальний QA та підсумковий review. Merge/deploy заборонені власником.
