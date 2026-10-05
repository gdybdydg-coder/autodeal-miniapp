# OLX лише для власника — актуальний checkpoint 05.10.2026, 22:50 Kyiv

Дозвіл власника чинний: `authorization.json`; повторне погодження цього owner-only запуску не потрібне. Дозвіл іншим клієнтам відсутній. Запуск ще **не виконано**: technical_ready=false, валідованого market profile немає, production/main/env/DB/webhook не змінені, Telegram attempts=0. Без оцінки контрольні повідомлення не надсилалися.

Використано нічні модулі та 74 збережені OLX-картки. Реальні збережені приклади відтворили дві помилки: власний опис рестайлінгу не потрапляв у generation_variant; російський paint label «Требуется восстановление» не визначав окремий ремонтний стан. Виправлено вузькими правилами; невідомі, суперечливі та заперечені claims не доповнюються типовими характеристиками. Replay74 залишається історичним, не доказом поточної доступності: 0/74 оцінок, max3/8 аналогів.

Новий безкоштовний канал повних public HTML AUTO.RIA має окремий адаптер `olx-ria-public-reference-v1`, binding body hash/receipt/visible state та явну ціну пропозиції USD. Він не підміняє paid auto/info, не є ціною продажу та не виконує paid requests. Primary public generation catalog підтвердив точні A5/pre_FL та A5/FL ID3133/3607; рік не використано як доказ покоління. Перша картка40106586 була й лишається source_feasibility, не reference.

Склад групи зафіксовано до цін повних карток у `d408c0eeeb85ff70f1a16a427c96a50c9b048c1f`: 11 reference та 3 holdout, namespace source:id. Перед шістьма деталями успішно зарезервовано I/O в `71dffc0b4b4d2541bc2c88f08e88f762f9ce256f`. Повні карти шести оголошень отримано 22:32–22:33 Kyiv, HTTP200. Ролі після результатів не змінено.

На цьому пакеті відтворено ще дві помилки public parser: engine label із третім компонентом потужності / fuel-only label; порядок «двигун/кузов потребує ремонту». Спочатку regression failures, потім мінімальні виправлення. За незмінними bytes/receipts: before2/6 parsed, after6/6. Лише 3/6 мають повні source attributes: holdout40219391, holdout40019461 із body_dents, reference39809027. У40133416/40343200 об’єм двигуна відсутній; у40391488 стан невідомий. Це не заповнено з nominal1.6D каталогу. До контролів сумісних придатних reference наразі max0/8; оцінено0/3 контролів; фото незалежності реально не перевірялися. Ціни8500/5000/7200/5350/7400/7800 — asking, не готові ринкові оцінки.

Свіжа OLX936176691 (22:06 Kyiv)6999USD/2012/255000km/1.6diesel/manual/front/A5 має corroborated derived VIN key, але невідомі power, condition та generation_variant. Ціна й джерело перевірені на час GET; картка вже не має send-freshness≤300s. Немає підтвердженої вигідності; не відправлена.

Підготовлено чистий offline identity_review validator із reviewer trust, декодованими photo receipts, точним population/pair coverage і взаємовиключенням VIN-crossposts/sharedimages. Реального photo review немає; модуль не підключено як readiness shortcut. Public screening має окремий channel dispatch, змішані/порожні докази утримуються без fallback на paid proof.

Фінальні незалежні suites: 566+20+77+2 = **665 tests**, 150+8+135 = **293 subtests**, усі pass. Barrier до імпорту; нові pure tests не імпортують app/.env/RIA transport. Старі backend tests використовують fixture SQLite; одна існуюча Starlette/httpx deprecation warning. Невдале змішування paid-adapter/no-RIA-import suite вирішено окремими процесами, без послаблення assertions. Synthetic, saved real та current live receipts розділено.

Бюджет цієї дозволеної роботи: **12 GET / 7,189,083 body bytes** (11 public RIA +1OLX), paidRIA0, FX0, Telegram0, retries0. Усі GET зарезервовані до I/O; 12/h використано. Попереднє денне використання не обнулене; старий нічний і paid budget не відкриті. Початковий широкий captcha token matcher у OLX отримав vocabulary із configuration, але повна same-ID карта, ціна й опис були доступні; challenge не було, додаткового I/O чи обходу не було. Receipts зберігають це уточнення.

Production: Render main-only autoDeploy=yes, previews off; відповідний workflow не запускається для research branch. Live/main незмінні `2d81e13ab04077133eca0a668a575e2d571b6cd7`, deploy `dep-db1pi6vavr4c73d1mog0`. Structured health22:46:56 Kyiv/log a3732da8-fcf9-4e0b-b016-7a95741638af: running=true, підтверджена оплата/ready/1 enabled search власника, strict_paid=true, admincarcopies=false, paymentnotices=true, pending11 reserved_for_new_publications. Last owner Telegram acceptance22:29:55 Kyiv, client22:41:51 Kyiv; 26 accepted/hour. Це AUTO.RIA, не OLX і не доказ прочитання. Стан черги природно змінюється, не має бути нульовим.

178/183 існуючих backend.py побайтово незмінні щодо live commit; 5 змінених тільки OLX: ria_reference/evaluation/valuation/observations/owner_feed. App, робочі AUTO.RIA transport/search/valuation/monitor/schedule/quota, платежі, /stats та Telegram setup незмінні. Night3600 23–08/day110 08–18/evening60 18–23 Kyiv, квоти4500h/90000d незмінні. Актуальні hashes і whitelisted health у сусідніх manifest/postcheck. OLX flags не читалися повторно через secrets/env; off startup12:34:35 UTC є історичним. У цій роботі enable/deploy/env write не було.

Наступний точний крок: зберегти frozen roles і перевірити решту8 reference лише за наступною успішною reservation та доступним погодженим бюджетом; не передавати known incomplete/damaged controls до reference для збільшення вибірки. Якщо поточні frozen controls неоцінювані, окремий НОВИЙ price-blind набір контролів потрібно зафіксувати до нових цін, а0/3 старого залишити у звіті. Потрібні ≥8 незалежних сумісних reference, ≥3 estimable untouched controls, actual photo-review, stable method і повна актуальна придатна OLX-card за незмінним search#4. Після цього — лише6 потрібних runtime OLX файлів, disabled deployment, runtime recipient/private getChat/access checks, activation exactly owner, до3 TG attempts; незалежний ledger/status/stop. Ці етапи не названо виконаними.

Погоджена майбутня конфігурація: static reviewed feed ≤24h,600s08–23/3600s23–08 Kyiv;12GET/h60/day160MiB/day2MiB/detail4spause20stimeout1controlled retry; cap3 totalTGattempts. /olx_stop зупиняє тільки OLX; /stop не обходиться. Регулярний source discovery наразі не ввімкнений, нову automation не створено.

---

Історичний початок цієї роботи, 21:57 Kyiv:

Дозвіл на погоджений контрольний запуск отримано. Іншим клієнтам дозвіл відсутній. Технічна готовність окрема: поки false; profile відсутній, 0/74 оцінок, max3/8, 0/3 контрольних оцінок. Не підміняти дозвіл готовністю.

Нова гіпотеза: перевірити одну вже спостережену public HTML картку AUTO.RIA як безкоштовний канал аналогів. Перед I/O закомічено рівно 1 GET / 2 MiB. Старий paid budget не відкривається, paid API=0, Telegram=0 до придатної оцінки. На 401/403/429/CAPTCHA канал ставиться на hold. Автоматичні redirect/retry відсутні.

AUTO.RIA main/live=2d81e13ab04077133eca0a668a575e2d571b6cd7; main-only autoDeploy, previews off; research push ізольований. Структурований snapshot 22:01:05 Kyiv: running=true, pending_jobs={}, strict_paid=true, ніч3600; last_owner_accepted_at=1791223594.4669101; час перераховується окремо.
