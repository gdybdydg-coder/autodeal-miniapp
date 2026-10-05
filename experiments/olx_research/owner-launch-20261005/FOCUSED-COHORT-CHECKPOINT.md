# Продовження owner-only OLX — 05.10.2026 після 23:02 Europe/Kyiv

Дозвіл власника вже чинний. Іншим клієнтам дозвіл відсутній. Ця ітерація завершила раніше заморожений набір деталей, виправила підтверджений пропуск source claim та перевірила альтернативні наявні дані. OLX-доставку не активовано: мінімум сумісних аналогів та перевірка методу не пройдені. Production/main/env/DB/webhook не змінювалися; Telegram-викликів0.

## Реальні джерела та незмінний контроль

Перед I/O створено remote reservation `2dc1f452629e8b8cc877e2ee68e1c379d36460af`: 8 GET /16MiB, exact observed URLs, 2MiB/detail, timeout20s, pause4s, retries0. Членство та ролі залишилися від freeze `d408c0eeeb85ff70f1a16a427c96a50c9b048c1f`, до цін full cards. Controls40219391,40019461,40133416 не переміщено до reference. Feasibility40106586 також не reference.

Отримано8public AUTO.RIA HTML 05.10.2026 23:07:37–23:09:15 Kyiv: HTTP200, 4,387,616bodybytes, elapsed97.631s, retry0, paidAPI0, FX0, Telegram0. Captcha/auth обходу чи redirects немає. HTTP200 не є підтвердженням активного оголошення:40177989,40271690,40082502 не мають підтвердженої active same-ID картки.40434063 має реальний конфлікт142.8hp проти105hp;105kW не переозначено як105hp.

## Відтворення → виправлення → перевірка

39790008 та40271556 містять явні own whole-car claims про справний стан у повному видимому описі, але technical/paint labels відсутні. Старий public reader залишав стан невідомим. Додано вузьку corroborated own-description policy, hash claim та явне seller-declared походження. Це не незалежна фізична перевірка. Потужність, покоління, VIN або ремонтні дані не заповнюються припущеннями.

Заперечення, історичні й чужі claims, engine-only praise, кузовні нюанси та nonempty unknown/repaired labels не отримують clean condition. Під час review окремо перевірено небезпеку історичного suffix після позитивної фрази; виправлення та tests збережені поряд із модулем. Числа фінального test-run у `focused-verification.json`; старі665tests/293subtests залишаються історичною перевіркою попереднього commit, не додаються до поточного overlapping suite.

Runner тепер приймає додаткові immutable packets лише того самого freeze та відмовляє duplicate IDs. Network barrier встановлений до імпорту; app/.env/production transport не підвантажуються. Оригінальні timestamps, roles та receipts не переписані.

## Достатність після всіх14 frozen full-card attempts

10/14 parsed;5source-complete:3reference(39809027,39790008,40271556) та2controls(40219391,40019461). Другий control має body_dents і залишається окремим станом. Source-complete не означає, що всі5сумісні між собою: до контролів max2/8references, estimatedcontrols0/3, метод не обраний, stability невідома. Ринкових цін та вигідностей для owner OLX не створено. Прибрана вимога фото сама по собі цього не вирішила б.

Окремі asking prices повних references:

| AUTO.RIA ID | Asking USD | Year/km | Стан |
|---|---:|---|---|
|39809027|7400|2010/305000|seller-declared running|
|39790008|7199|2011/261000|seller-declared running|
|40271556|8300|2012/254000|seller-declared running|

Посилання, binding, час джерела, inclusion/exclusion reasons та чесні знаменники є у `focused-all14-details-sanitized.json` і `focused-all14-evaluation.json`. Ці числа — ціни пропозицій, не готові market estimates чи sale prices. `focused-all14-before-own-claim-fix.json` — явно історичний before snapshot. Повні HTML/VIN/контакти не внесені в git.

Для closest saved owner936749861(5900USD,2011/300k,Khmelnytskyi,1.6diesel/manual/front/liftback) бракує явних power/generation variant, є кузовна невизначеність, немає підтвердженої crosspost independence. Навіть upper bound за year±1/km±30000 у цьому frozen наборі —4reference до інших критичних перевірок;3controls не можна позичати.936176691 також не має повних power/variant/condition та send-freshness≤300s. Ці saved приклади не доказ нинішньої доступності.

## Альтернативні дані перевірено

Public SSR має12active own-listing-bound `averagePrice` widgets із15receipted full HTML. Hashes15/15 збігаються. Число прив’язане до власного RIA ID, але не показане числом у visible price row; там лише кнопка «Средняя цена». Валюта, range, sample count/IDs, method/date та condition adjustment у цьому вузлі відсутні. `public-price-widget-discovery.json` зберігає факти й невідомі поля. Не перенесено own-RIA average на окремий OLX-target і не названо це перевіреним альтернативним каналом.

Старі12paid auto/info projections не зберегли price0/12, power0/12 або повні raw receipts0/12. Drive12/12 не робить їх достатніми. Після condition screening upper bound старої групи7reference+2controls ще до інших unknowns. Synthetic API fixtures не використані як real observations. Нових paid calls немає; старий budgetdeadline16:00 не відкрито.

Read-only Render DB schema lookup заблоковано ДО SQL: external IP allowlist порожній, hosted MCP не має допустимого IP. Не відкривали доступ/allowlist, не читали secrets/connection strings, не змінювали DB. Current cache rows/counts невідомі. За кодом SourceCache зберігає parsed projection, а не повні power/drive/description/body-binding proofs; не оголошено це готовими аналогами.

Saved OLX discovery знайшов920333425(explicitFL/105hp/1.6diesel/wagon,2009/330k,Starokostiantyniv) із unrepaired paint traces та932435534(1.9diesel/manual/wagon,2006/328k,Nemyriv) без power/variant. URL спостережені буквально; нові GET не виконано, UAHasking історична.936798200 має43k vs430k mileage conflict. Не витрачаємо бюджет на непідтверджену придатність або штучне збільшення вибірки. Реальних збережених bytes фото/receipts не знайдено; photo review0.

## Бюджет та захист AUTO.RIA

Поточний OWNER scenario консервативно включає всі попередні денні owner episodes:51/60GET,121,896,960/167,772,160reservedbytes(116.25/160MiB), залишок9GET/43.75MiB. У фіксованійUTC20:00/Kyiv23:00 годині8/12. Known actual body lower bound66,477,286bytes, повні actualdaybytes невідомі через старий probe receipt. Резерви не повернено. Окремий stopped NIGHT29GET/68MiB не відкрито; це не глобальний календарний budget reset. `focused-source-budget.json` містить точну арифметику.

Fresh Render get_service/list_deploys: main-only autoDeploy=yes, previews off, live commit2d81e13ab04077133eca0a668a575e2d571b6cd7/deploydep-db1pi6vavr4c73d1mog0. Research push не deployment. Workflow triggers для цієї research branch відсутні. Direct byte comparison178/183existingbackendpy незмінні;5changed лишеOLX, public adapter новийOLX-only. App, AUTO.RIA пошук/формула/доставка/черги/квоти/оплата/stats/webhook/setup незмінні.

Остання надана structured health05.10.2026 23:02:06.620Kyiv/log8d93065d-9b8a-412c-b223-045727c8900f: runningtrue,strictpaidtrue,owner confirmedpaid/ready/1enabledsearch, night3600s23–08; day110s08–18/evening60s18–23 Kyiv, limits4500h/90000d/1102160total. Pending17reserved_for_new_publications,27accepted/hour. Останній owner AUTO.RIA acceptedID40173304 о22:48:23.773Kyiv; latestclient22:59:15.184. Це Telegram acceptance AUTO.RIA, не OLX і не прочитання. Whitelist збережено у `production-focused-postcheck.json`. OLXenvflags повторно не читалися; enable/deploy цього task0, старий offstartup — історичний.

## Стан запуску та точний наступний крок

Prepared code — так; deployed ці зміни — ні; owner permission — так; client permission — ні; technical readiness — false; реально підтверджені OLX sends —0. Немає listingID/time/result для OLX Telegram, бо викликів не було. Не надсилали неоцінені картки й не створювали fake profile/subscription/technical_ready.

Найближчий доказовий крок: знайти owner target із явно повною необхідною конфігурацією, потім price-blind freeze щільної сумісної групи та ≥8поточних незалежних reference/окремі estimable controls. Уже невдалу групу не збільшувати ціною несумісних кузовів/repair/років/km/невідомих даних. Publicaveragepopup — окрема prospective гіпотеза, що потребує видимого output/валюти/підстав та target compatibility, а не готова заміна. Наявні дозволи owner-only зберігаються.

Лише після real profile: мінімальні6runtimeOLXfiles, disableddeploy, liveisolationcheck, paidactivepinnedsearch#4/privategetChat, activation exactly protectedowner, до3totalTGattempts із окремим confirmed/rejected/uncertain ledger. Oldcanary/batch не replay; source+ID+recipient dedupe; uncertainphoto не textfallback.

Погоджені prospective limits: staticreviewedfeed≤24h,600s08–23/3600s23–08Kyiv,12GET/h60/day160MiB/day2MiB/detail4spause20stimeout1controlledretry,paidRIA0; /olx_stop тількиOLX, /stop не обходиться. Регулярний discovery/ChatGPTbackground не ввімкнено, automation не створено. Час першого повідомлення не обіцяний без достатніх даних.
