# Етап 5 — фінальний review/readiness, 01.10.2026

Актуальні подальші зміни: stage-7-handoff.md (90 prototype tests,
окремі локальні входи та файли квитанцій). Нижче — історія stage-5/6.

Оновлення після review: stage-5-handoff.md описує перевірене виправлення
stale pending notices, одноразовий worker preflight, active outbox capacity
та durable deferred flags. Набір цього етапу: 55 Python tests + Node workflow
passed. Нижче 36-test результат залишено як історичний доказ be8613e4.
Інтеграція/реальні платежі залишаються забороненими; наступні offline
session/UI перевірки можна виконувати без реквізитів або рішень про тариф.

Наступне оновлення: stage-6-handoff.md — спільний UI, loopback Harness ->
Adapter -> SQLite, 62 Python tests і connected UI event checks. Для
власника доступне окреме RAM демо у чаті. UI wiring та збереження SQLite
після restart перевірені, browser layout/visual engine ще не перевірено.
Це не server-side production auth або реальні банківські платежі.

Перевірена база: production main
`005897b0a93c7fe0166424341c0564a5fbaf94cb`, remote WIP перед review
`6accc58f6e805749e41bba750b7401dbe985ae8a`. Перевірено README та handoff
етапів 2, 3a, 3 і 4 з нового remote snapshot. Зміни цього етапу обмежені
`experiments/manual_subscription`; merge, deploy і production інтеграції не
виконувалися.

## Рішення

Прототип **готовий до огляду власником**, але **не готовий до merge, deploy,
реальних оплат або доступу користувачів**. SQLite Ledger, synthetic UI,
fixture auth boundary, concurrency та offline lifecycle придатні як доказ
логіки. Вони не є production API, платіжною системою чи Telegram auth.

## Перевірено

- create -> receipt -> clarification/resubmit -> approval -> renewal -> expiry;
- exact expiry, price snapshot, unique payment reference, restart та atomic
  concurrent approval/create;
- admin/client межі через synthetic in-memory session; uid/actor/tariff не
  приймаються з payload;
- `/stop`, epoch і synthetic sent/uncertain claims не змінюються оплатою,
  renewal або expiry;
- outbox claim-before-send, disjoint workers, dedup, delivered/uncertain і
  crash/restart без автоматичного повтору;
- executable prototype не імпортує мережеві/зовнішні service clients, UI не
  має fetch/WebSocket/browser storage, карток/рахунків/банківських ключів немає.

Підсумкові перевірки:

- `python -m unittest discover -s experiments/manual_subscription -v` —
  36 passed (33 lifecycle/auth/concurrency + 3 safety review);
- `node experiments/manual_subscription/test_workflow.cjs` — passed;
- `git diff --check` — clean.

Browser DOM/visual QA не входить у доказ: у локальному середовищі немає
встановленого browser executable. Node перевіряє workflow model, але не
реальні DOM events або рольове розділення екранів.

## No-go блокери перед будь-якою інтеграцією

1. **Справжня автентифікація.** FixtureSessions живе в RAM, не має TTL,
   ротації, persistent revocation, CSRF/session transport чи Telegram WebApp
   signature verification. `actor`/`uid` у Ledger — trusted caller.
2. **Модель оплати й правила платформи.** Треба окремо погодити законний канал
   ручної оплати цифрового доступу та сумісність із правилами Telegram Stars.
   Поточний тест не дозволяє публічну карткову оплату.
3. **Truth source перевірки.** `bank_verified=True` — рішення fixture-admin,
   не доказ надходження. Немає банківського API, виписки, dual control або
   процедури повернення/спору.
4. **Outbox policy.** Stale pending відсів і active-spool cap реалізовано
   в stage-5a. Залишаються reconciliation для claimed/prepared/uncertain,
   post-preflight send race, source-record admission і history retention;
   ліміт spool не є лімітом усієї БД. Див. stage-5-handoff.md.
5. **Дані та приватність.** Файл квитанції не завантажується. Не визначені
   дозволені формати, storage, строк видалення, доступ адміністраторів,
   audit/privacy policy, backup та migration rollout/rollback.
6. **UI/операції.** Клієнт/admin показані власнику у тесті. Stage 6
   перевіряє UI events через SQLite transport; public role-separated
   routes, browser/layout/accessibility regression, review SLA, підтримка
   й production observability залишаються окремими завданнями.

## Мінімальні рішення від власника

До наступного доручення достатньо надати п'ять рішень, без секретів у Git:

1. затверджені ціна, валюта, тривалість і правила renewal/refund;
2. обраний законний та Telegram-сумісний канал оплати;
3. хто є отримувачем/продавцем і де безпечно зберігатимуться реквізити
   (самі реквізити в репозиторій не додавати);
4. список admin-ролей та бажаний спосіб server-side auth/session lifecycle;
5. операційна політика: що є доказом надходження, строки review/retention і
   як вручну вирішуються stale, claimed та uncertain події.

Після цих відповідей потрібне окреме нове доручення на threat model та дизайн
production інтеграції. До нього WIP-гілку не merge/deploy, production main,
Render, Telegram, AUTO.RIA, Stars pilot, runtime jobs і реальні дані не чіпати.
## Оновлення етапу 7 (01.10.2026)

Попередні висновки нижче описують stage-5 стан. У stage-7-handoff.md
зафіксовано окремий loopback owner harness: one-use invitations,
durable 1-hour TTL/rotation/revocation, bounded file receipt quarantine
і фактичні UI -> HTTP -> SQLite event checks. Це закриває частину
offline auth/upload прототипування, але не production identity, public
owner-only delivery, scanning/privacy/retention, reconciliation claims,
source admission або погодження реальної моделі оплати. Заборона
main/Render/deploy і реальних платежів лишається чинною.
