# Етап 3 — стійкість offline-моделі, 01.10.2026

Перед роботою remote heads перевірено: production main
`005897b0a93c7fe0166424341c0564a5fbaf94cb`, тестова гілка спочатку
`484797dde46e345b690bd1ec42f10871d890f319`. Перед push виявлено паралельний
stage-3a commit `e716396b175322d5521483939b8d7cf3cbddbd5b`; зміни перенесено поверх
нього без force, його clarification history/auth tests та handoff збережено.
Робота виконана тільки у
`experiments/manual_subscription`; production файли, Render, env/runtime jobs,
справжні підписки/epochs/claims і зовнішні API не змінювались.

## Що додано

- receipt reference і payment reference приймають лише 3–100 символів із
  synthetic-safe набору ASCII; шлях, пробіли, керувальні та довгі значення
  відхиляються;
- заявлений час квитанції не може бути раніше створення замовлення, старшим
  за 7 днів або більш ніж на 5 хвилин у майбутньому;
- `review -> clarification -> review` з bounded note, revision і окремим
  audit `receipt_resubmitted`; стан витримує закриття/повторне відкриття SQLite;
- міграція додає поля етапу 3 до бази зі схемою етапу 1 без втрати замовлення;
- локальний outbox має unique dedupe key і переходи
  pending -> claimed -> delivered/uncertain; claim виконується до умовної
  відправки, а claimed/uncertain не повертаються в pending після restart;
  жодного транспорту чи справжньої доставки немає;
- expiry створює щонайбільше одну offline-подію на конкретний строк і не
  видаляє membership history;
- окрема `search_guard_fixtures` існує лише для тест-доказу: `/stop` вимикає
  synthetic search та збільшує epoch один раз, а approval, renewal, expiry й
  entitlement-read не вмикають його та не змінюють synthetic sent/uncertain;
- UI workflow використовує ті самі reference/time/note межі й показує поля
  вигаданого номера та часу квитанції.

## Перевірки

- `python -m unittest discover -s experiments/manual_subscription -v`:
  27 passed, включно з усіма 18 перевірками паралельного етапу 3a,
  clarification/resubmit restart, old-schema migration, outbox claim/dedup,
  delivered/uncertain restart, expiry, stopped-search renewal, preservation
  synthetic claims і чотирма конкурентними approval;
- `node experiments/manual_subscription/test_workflow.cjs`: passed; покриті
  reference/time межі, clarification/resubmit, сума, bankVerified, reuse,
  approval/renewal/reject.

Значення 249 грн, усі receipt/payment references, uid і claims — вигадані
fixtures. Файл квитанції не читається й не завантажується. DOM/browser visual
QA досі не виконаний; Node перевіряє чисту workflow-модель, а не event wiring.

## Обмеження і наступний крок

`actor`/`uid` залишаються довіреними аргументами бібліотеки, не справжньою
server-side authentication. Outbox не надсилає повідомлень і навмисно не має
автоматичного reclaim для claimed/uncertain без окремої reconciliation policy.
`search_guard_fixtures` не є копією production schema та
не може підключатися до неї. Відсутні банк, Telegram, AUTO.RIA, secrets,
реальні реквізити, мережа, deploy і публічна карткова оплата.

Етап 4: скласти один наскрізний offline integration test
create -> receipt -> clarification/resubmit -> approval -> renewal -> expiry,
перевірити повний audit/outbox, конкурентні create/approval та явний auth
adapter boundary. Не реалізовувати production auth, merge або deploy.
