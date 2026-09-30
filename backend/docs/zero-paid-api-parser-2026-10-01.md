# Ізольований public-details parser — 01.10.2026

Продовження zero-paid-api-night-2026-10-01.md та zero-paid-api-plan-2026-09-30.md.
Цей етап завершений як offline прототип, а не production джерело.

## Результат

Додано experiments/free_search/public_details.py, README і 30 unittest тестів із
синтетичними fixtures. Немає network/client/provider/DB/backend/worker imports,
API credentials або доставки. Уся нова логіка поза backend startup.

Парсер повертає listing ID, brand/model/year/body/fuel/transmission,
odometer у підтверджених km, Decimal price/currency, явний stock status і condition.
Allowlist виключає VIN, продавця, телефони, description, photos і raw HTML.
Кожне прийняте поле має provenance public_jsonld: це твердження публічної
сторінки, не підтвердження актуальності. Missing і invalid відрізняються;
відсутні optional і damage не відхиляють авто. Конфліктні known values
відхиляються, не підміняються першим/останнім значенням.

Expected ID узгоджується з усіма заявленими Vehicle URLs/@id/mainEntityOfPage,
HTML canonical і optional Offer URLs; тільки HTTPS auto.ria.com. Фрагменти URL
для same-ID image/offer entities допустимі, query/foreign hosts/IDs — ні.
Image-only Vehicle block не стирає знайдені характеристики. Root list та @graph
підтримані, довільні nested рекомендації не обходяться. Foreign root Vehicle
консервативно відхиляє весь зразок, що може зменшувати recall.

USD/EUR/UAH зберігаються без invented conversion; non-USD має issue.
Unknown/preorder/відсутня availability — unknown, SoldOut/OutOfStock/Discontinued
— unavailable. Zero/negative/bool/NaN/infinite/абсурдно великі prices відхиляються.
Невідомі mileage units не конвертуються. Некоректний JSON, duplicate keys,
conflicting fields і bounded-input порушення відхиляються з кодом без raw values.

Ready-for-delivery завжди false. Category/region/custom-abroad/add-date,
positive current price proof і valuation ще не готові. Vehicle не доказ легковика.
Не повертати інформаційні картки чи міняти paid AI/confirmed_deals_only.

## Перевірки

`python -m unittest discover -s experiments/free_search/tests -v`:
**30 tests passed**, 0.020s, без мережі та DB. Це перевірка прототипу,
а не повний backend regression. Перед будь-яким майбутнім merge потрібна
повна перевірка; зараз merge/deploy заборонені користувачем.

Fresh remote main: 901d96fe1f7e10196155ef6dd329da9d62f2a250,
tree 0f1392a7cc7a3f5c8e2eba7af0851561b97e4ef2. Render dep-daumfo59fdbs739haga0
live на тому самому commit. Health ok, PostgreSQL connected, delivery available.
Source-status: monitor running/idle, 61 watching/successful groups, night3600s,
pending_jobs0, needs_attention=false, quota available. Lag1684s відповідає
годинному нічному режиму цього зразка, не гарантія майбутньої затримки.
Local ledger total229382, remaining872778; provider balance не читався.
Active-window/initial false, confirmed-only true. Production не змінено.

На цьому етапі **0 AUTO.RIA API calls та 0 AUTO.RIA public HTTP calls**;
лише read-only власний health/status і GitHub/Render reads. Попередній
bounded public sample описаний окремо в нічному checkpoint.

## Наступний крок

Перевірити current robots/terms і bounded public sample структур:
як scoped card привʼязує data-add-date/update-date, promotions, ціну/область,
як видно category/region/custom-abroad/date на detail page. Зберігати тільки
агрегати/санітизовані структурні fixtures, не seller data. Додати offline
card parser і publication proof validator з baseline, age limit, old-ID
repeat publication, old update-only rejection, duplicates/conflicts. Наступні
етапи: shared queue/restart та offline saved-filter matcher, /stop/epochs.
Terms/coverage/свіжість деталей/власна оцінка залишаються unproved; не
запускати live collector/notifications або новий Render service.
