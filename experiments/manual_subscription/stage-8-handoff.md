# Етап 8 — приватний профіль отримувача, 01.10.2026

Власник о 18:01 Kyiv надав ім'я ФОП, recipient code, IBAN і bank name
після пропозиції додати реквізити лише в тест. Це дозволяє private
recipient configuration, але не production/deploy/real funds або
погодження тарифу. Стара stage-7 межа «ніяких реквізитів» уточнена цим
новим дорученням; усі інші межі незмінні. Реальні значення НЕ записувати
в GitHub, handoffs, unit fixtures, stdout/logs або test ledger.

Read-only main до роботи: 005897b0a93c7fe0166424341c0564a5fbaf94cb.
Fresh Render list: dep-dav2570473hc73d7bkj0 live, same commit,
finished 2026-10-01T09:07:41.772737Z. WIP база
9a38a4b2709bc49aba25eb0b041fdc6107fe74e2,
tree c105d745f4fd9610f84d541e2c499f3d3873968f.
41 файлів отримано заново через GitHub connector і звірено Git blob SHA;
remote AGENTS.md немає. Прочитано stage-7 handoff. Старі локальні
папки не використані як source of truth. Production не змінювали.

## Реалізовано

- `bank_profile.py`: strict schema/exact fields, schema version 1;
  labels <=200 без control characters, ASCII recipient code 8/10 digits;
  Ukrainian IBAN shape/country + institution code + BBAN, uppercase/
  pasted whitespace normalization та MOD97 checksum. Recipient code
  перевіряється лише структурно, не як факт реєстрації ФОП.
- `load_profile`: явно вибраний LOCAL JSON поза repo, regular file 0600,
  <=8192 bytes, O_NOFOLLOW, duplicate JSON keys fail closed; values не
  потрапляють до validation error. Немає env/secret auto-discovery,
  банківської API, HTTP lookup або автоматичного пошуку профілю.
- OwnerHarness `--recipient-profile`: профіль immutable in-memory,
  auth/order-bound payment_instruction. Bank details не пишуться до
  orders/memberships/audit/outbox/receipt DB. Без opt-in профілю legacy
  owner flow лишається без реквізитів.
- `/api/state` видає інструкцію лише із чинною сесією й після створення
  order. Body create/action не можуть замінити recipient/amount/uid.
  Public pages/assets не мають значень. Profile/source/DB paths 404.
- Shared owner UI: ім'я/код/IBAN/банк/amount/purpose, textContent,
  selectable IBAN, явні copy buttons. `owner-login.js` додає clipboard
  callback; недоступний clipboard дає помилку, не false success.
  Purpose: доступ до AUTODeal, days і конкретний order ID. Не додавали
  VAT/tax статус або інші непогоджені юридичні твердження.
- Власник **не погодив ціну**: 249 грн/30 днів — старий synthetic tariff.
  Стан tariff_confirmed=false/payments_enabled=false зашитий у цій
  схемі. Mode тільки test_only. UI «тариф не затверджено» й «Не переказуй
  кошти». Profile не може сам увімкнути live payments чи вибрати amount.

## Приватні дані

Профіль власника збережено окремим private settings file поза checkout
і приватно збережено для наступної роботи. Metadata/path/profile IDs у
цей public repository не вставляти. Значення ні в tests, ні в commit.
Локальні права 0600; *.private.json також ігнорується у experiments.

Наданий IBAN пройшов локальний shape/checksum validation. Для перевірки
дані не надсилалися НБУ/банку/іншим сервісам. Це не доказ фактичної
наявності, балансу або належності рахунку. Primary format source:
https://bank.gov.ua/ua/iban; офіційний search result підтвердив 29-char
структуру, direct open повернув 403 і не обходився. Приватні user values
не включалися до web queries.

Smoke actual supplied profile: expected fields у authenticated
instructions, payments disabled, жодного recipient value у test DB.
Перед GitHub write проскановано весь experimental source за actual
private values; жодного збігу. Не друкувати їх для «доказу» повторно.

## Перевірки

- `check-offline.py`: **101 Python tests**, 11.726s, passed; non-loopback
  TCP та DNS fenced. Усі старі 90 + 7 profile + 4 recipient HTTP.
  Invalid checksum/shape/Unicode digits/country, pasted whitespace,
  schema/tariff attempts, control characters/code lengths, permission/
  size/symlink/outside-repo, duplicate fields, no value dump errors,
  exact order snapshot, public/unauth no leakage, body can't replace
  destination, new order purpose та unchanged guards.
- `check-owner-ui.py`: actual owner login/UI -> raw HTTP -> auth/
  receipt/SQLite, manual approval/lost response retry/logout; нові
  protected recipient rendering, unpaid tariff notice і exact copied
  IBAN/purpose content passed. Fixtures виключно synthetic, не user bank.
- `check-ui.py` та `test_workflow.cjs`: legacy passed.
- Actual owner private profile authenticated smoke та source/DB privacy
  scan passed без logging значень. Ніяких actual funds або bank requests.

Мінімальна DOM event fixture, не реальний browser/file picker/clipboard
engine/visual/accessibility QA. Bank profile field values показуються
лише в local owner harness; публічної адреси або доступного з телефону
приватного test server немає. Старий stage-6 RAM inline preview не
має реальних реквізитів і не видається за цей підключений тест.

## Наступний крок

Отримати ціну на 30 днів, не приймати silence за згоду на 249 грн.
Далі offline tariff snapshot/plan contract та entitlement admission/
preflight із /stop/epoch/sent/uncertain/restart/renewal/expiry; чинний
production subscription/payment/delivery state не змінювати.

До фактичних платежів ще потрібні payment-model/platform compatibility,
owner-only phone-access/auth transport, invoice/receipt privacy/scanning/
retention та review/refund правила. Підтвердження checkbox у тесті не є
bank reconciliation. No QR/payment links/transfers/bank API/Telegram
messages/AUTO.RIA requests; no production main/Render/env/runtime jobs/
claims/accounting changes, no deploys/resources або new automations.
WIP updates only non-force після fresh head й remote exact-tree check.
