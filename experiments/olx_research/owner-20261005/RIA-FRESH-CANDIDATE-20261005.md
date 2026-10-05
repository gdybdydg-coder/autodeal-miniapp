# OLX: продовження після запиту 14:55 Kyiv

Мета: перевірити ще одного вже відомого кандидата з найповнішими фактичними полями, а не найдешевше оголошення. OLX936658970: у збереженій картці Octavia A5, 1.6 дизель, 105 к.с., механіка, передній привід, універсал, 2010, 270 тис. км, Хмельницька область, $6500. Ці історичні поля не вважаються поточними.

До I/O зарезервовано один full OLX GET, cap2MiB, timeout20s, retries0; один AI HTTP з існуючого погодженого залишку8/4, cap1MiB. Сумарний budget30/6 не збільшується. Довідники повторно не запитуються. Власник, чинна підтверджена оплата, ready, власний активний пошук#4/fingerprint та /stop перевіряються на сервері перед I/O. Немає sender, fake subscription, іншого отримувача, force-ready.

Гіпотеза reuse: існуючі порівняльні ValuationPeer observations можуть містити достатню поточну групу без нового API. Зовнішній read-only SQL через Render недоступний через закриту IP allowlist; правила мережі не змінено. Замість відкриття БД — read-only SELECT усередині вже чинного server context, max200 fresh15min Skoda Octavia reference rows, statement timeout3s. Зберігається лише whitelist санітизованих полів, жодних user logs, rawVIN/контактів. SourceCache автомобілі під клієнтськими ціновими/регіональними фільтрами не використовуються як ринкова вибірка.

Друга підтверджена перешкода: olx_owner_feed.profile_ready та market.screened досі приймають тільки OLX observations. Наявний RIA parameter adapter не є completed feed integration. Цей пакет не підміняє ці перевірки readiness=true; для запуску потрібен окремий перевірений cross-source reference adapter та реальна достатня вибірка.

A5 у заголовку визначає родину; pre-FL/FL не підставляється за роком. У цьому diagnostic AI запиті generationId пропущено та це залишається blocker. Повна картка і її eligibility перевіряються до оцінки. Байти HTTP зберігаються відразу, витрачені reservations не скидаються; завершена фаза4 не повторюється на restart. Немає змін AUTO.RIA, webhook, env чи його delivery state.
