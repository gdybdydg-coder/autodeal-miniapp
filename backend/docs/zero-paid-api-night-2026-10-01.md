# Нічна тестова версія без платних AUTO.RIA запитів — 01.10.2026

## Авторизація і межі

Користувач о 00:17 Kyiv уточнив: у самому боті нічого не змінювати; він далі працює на платних запитах. Готуємо окрему тестову версію і працюємо над нею вночі. Це переважає попередній дозвіл звичайного deploy для даної роботи. Заборонені зміни main, чинного Render service/env, production jobs/subscriptions/epochs/claims/ledger; deploy і тестові Telegram повідомлення не робити. GitHub WIP та локальні offline перевірки дозволені. Платні API виклики для цього дослідження не потрібні.

План: zero-paid-api-plan-2026-09-30.md. Автоматична задача підтверджено створена і оновлена: 7 годинних запусків, починаючи приблизно 01:30 Kyiv, flexible schedule. Це дискретні запуски, а не безперервний процес чи гарантія завершення всього плану. У кожному запуску перевіряти актуальний remote head та продовжувати з останнього checkpoint, не затерти чужі зміни.

## Перевірка перед роботою

Remote main 901d96fe1f7e10196155ef6dd329da9d62f2a250, tree 0f1392a7cc7a3f5c8e2eba7af0851561b97e4ef2. Render dep-daumfo59fdbs739haga0 live, той самий commit. /health: ok, PostgreSQL connected, delivery available. Production не змінювали.

## Перший bounded зразок

З scratch поза Render, User-Agent AUTODeal-Research/0.1, без cookies/credentials, redirect follow вимкнено для сторінок, timeout25s, тіло<=2.5MB. Загалом 5 звичайних HTTP запитів: robots.txt, дві спроби public first page і дві detail page; 0 API викликів. Повтор потрібний через виправлення локального parser prototype (href=None і date не на section); це помилка прототипу, не помилка AUTO.RIA. Сирі HTML, VIN/телефони/продавці не збережені; збережено лише агрегати та назви полів.

robots.txt отримано. Група User-agent:* не забороняє /uk/last/hour/ або конкретні /uk/auto_*.html; забороняє, серед іншого, /api/, graphql, bff, blocks_search_ajax. Robots не є підтвердженням умов комерційного повторного використання; ці умови ще дослідити. У разі denial/429 не обходити, не використовувати приховані маршрути.

Виправлений зразок public first page: 100 ticket cards, 100 elements data-add-date, 99 distinct matching detail links. Це структура одного зразка, а не coverage. Дати лежать не обовʼязково на section; parser має привʼязувати child атрибути до правильного card scope.

Detail page має видимий JSON-LD Vehicle: brand, model, productionDate, bodyType, fuelType, vehicleTransmission, mileageFromOdometer(value/unitCode), offers(price/priceCurrency/availability/itemCondition), vehicleEngine(fuelType). Окремий Vehicle містить images. VIN/description/ratings є в джерелі, але їх не збирати/не зберігати. Наявність ключа не підтверджує коректність значення. Sample detail не має data-add-date; доказ свіжої публікації залишатиметься зі scoped card і окремо звіряти видиму detail дату, якщо доступна.

Отже: є потенційне безкоштовне джерело деталей для offline парсера. НЕ підтверджені повнота/валюта/пасажирська категорія/регіон/custom-abroad/точна актуальність статусу чи дата. JSON-LD Vehicle само по собі не означає легковик. Не підставляти breadcrumb категорію без перевірки, не використовувати description для стану як блокування.

## Наступний конкретний крок

Створити ізольований experimental модуль public detail parser з синтетичними sanitized fixtures і тестами, без імпорту app/worker/models/production DB, без httpx provider/API ключів. Input: HTML та очікуваний canonical ID. Output: обмежена typed структура з provenance/missing/conflicts; brand/model/year/body/fuel/transmission/mileage та позитивна offer price+currency+availability. Відкидати чужий ID/host, некоректний JSON, ціну<=0/NaN/inf, неправильні units, конфліктні primary Vehicle/Offer; не зливати чужі авто або перетворювати currency навмання. Зберегти distinction absent vs contradicted. Не створювати MonitorJob/Delivery, не робити API fallback. Винести прототип у experiments/, не включати в production startup.

Meaningful тести: кілька LD blocks для одного авто + image block; чужий ID; duplicate conflicting offers; sold/unavailable/unknown; missing optional fuel/transmission; priceCurrency неUSD; odometer units; promoted/nonpassenger card; стара add-date/свіже update-date; baseline/restart; /stop/epoch/dedupe offline у наступних етапах. Усі мережеві тести fixtures; не качати каталог. Після цього перевірити відсутні region/category/custom/date на кількох bounded дозволених зразках і умови використання. Охоплення і власна оцінка ще не реалізовані. Paid AI production незмінна.
