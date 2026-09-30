# Public-card publication proof — 01.10.2026

Продовження isolated public-details parser. Додано лише до WIP; production main,
Render/env, jobs, subscriptions, claims, Telegram та API accounting не змінені.

## Реалізовано

`experiments/free_search/public_cards.py` — standard-library-only offline parser
public cards і чистий restart-safe state transition. Він не імпортує backend,
мережевий клієнт, БД, credentials або delivery.

Card evidence містить тільки listing ID, canonical listing URL, data-add-date,
окремий data-update-date, optional preview USD, promotion flag, issue codes і
provenance. Продавець/VIN/опис/телефон/фото/raw HTML не повертаються.

Перша успішна snapshot створює baseline і не дає кандидатів. Наступний ID стає
кандидатом лише коли card не promoted, структура/URL/add-date узгоджені, а
`max(baseline, observed_at-3600) < add_date <= observed_at`. Update-date,
first-seen, підняття або зміна preview price свідомо не є доказом публікації.
Preview price не є підтвердженням актуальної ціни.

Accepted `(ID, add_date)` зберігається. Restart roundtrip не повторює кандидата.
Старий ID може знову пройти тільки з реально новішим add-date після baseline;
та сама/старіша дата, навіть зі свіжим update-date або іншою ціною, не проходить.
Clock regression, майбутня/застаріла дата, рекламна картка, duplicate ID,
invalid/ambiguous Kyiv DST date, чужий host/ID або query URL fail closed/skip.

Candidate не має valuation/delivery і означає лише право на наступну безплатну
перевірку detail/category/region/filter. Це не recovery старого каталогу й не
змінює confirmed-deals-only.

## Перевірки

23 нові synthetic/offline tests passed за 0.021s. Разом із попереднім details
parser: **53 isolated tests passed** після відновлення exact remote WIP файлів у
свіжий scratch. Тести охоплюють baseline, restart dedupe, genuine old-ID repeat,
update-only/reprice/raise rejection, promotions, future/stale dates, URL/ID,
missing optional preview та відсутність network/production imports.

На цьому етапі 0 AUTO.RIA API calls і 0 AUTO.RIA public HTTP calls. Перевірено
fresh remote: main `901d96fe1f7e10196155ef6dd329da9d62f2a250`, WIP parent
`9955af485604f39b6b375175a2aadb5662305fe2`, Render
`dep-daumfo59fdbs739haga0` live на main. Production health ok, PostgreSQL
connected, delivery available; monitor running/idle, 62 groups watching,
pending_jobs 0, active-window/initial false, confirmed-only true, quota available.
Нічний lag є snapshot у годинному режимі, не delivery guarantee.

## Що ще не доведено

Public cards не підтверджують passenger category, область, abroad/custom,
повну відповідність фільтрам, актуальний active/sold status або detail price.
Дві сторінки не доводять whole-market recall. Terms/дозволене комерційне
повторне використання потребує окремої перевірки. Власної оцінки ще немає.

Наступний етап: bounded structural audit кількох дозволених detail/card samples
без збереження персональних даних; визначити наявність category/region/custom,
видимої дати та узгодження current price. Потім offline filter matcher, де
missing optional не блокує, known contradiction/abroad/custom/непозитивна ціна
блокують. Жодного deploy або тестового Telegram потоку.
