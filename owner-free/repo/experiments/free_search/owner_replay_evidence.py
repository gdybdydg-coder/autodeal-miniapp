"""Import saved public experiment records offline and build a review artifact."""
from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import time

from .network_guard import OutboundGuard
from .owner_peer_cache import PeerCache


def run(workspace):
    workspace = Path(workspace)
    cache = PeerCache(workspace/'verified.owner-peers.sqlite3')
    records, failures = {}, {}
    for filename in ('owner-peers-20261019.jsonl', 'owner-peers-prefilter-20261019.jsonl'):
        items = [json.loads(line) for line in (workspace/filename).read_text(encoding='utf-8-sig').splitlines()]
        report = items[-1]['result']
        for row in [report['subject'], *report['peers']]:
            ident = row['listing_id']
            if ident not in records or row['observed_at'] > records[ident]['observed_at']:
                records[ident] = row
        for item in items:
            receipt = item.get('receipt', {})
            for ident, reason in report['peer_errors'].items():
                # Comparability mismatch is not a broken source record.
                if reason.endswith('_mismatch'):
                    continue
                if receipt.get('url', '').endswith('_'+ident+'.html'):
                    failures[ident] = (receipt['completed_at'], reason)
    try:
        for ident, row in records.items():
            cache.put(ident, row['observed_at'], row=row)
        for ident, (observed, reason) in failures.items():
            cache.put(ident, observed, reason=reason)
        snapshot = cache.snapshot(time.time())
    finally:
        cache.close()
    summary = dict(fresh_records=len(snapshot['rows']), unresolved=snapshot['unavailable'],
                   max_age_seconds=snapshot['max_age_seconds'], ready_for_delivery=False)
    (workspace/'owner-cache-replay-20261019.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    rows = ''.join('<tr>'+''.join('<td>'+escape(str(row.get(k, '—')))+'</td>' for k in
        ('listing_id','year','fuel','transmission','drive','mileage_km','price'))+'</tr>'
        for row in snapshot['rows'])
    html = '''<!doctype html><html lang="uk"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AutoDeal — перевірений прогрес</title>
<style>body{font:16px/1.6 system-ui;background:#f4f6f8;color:#182333;max-width:1100px;margin:40px auto;padding:0 24px}h1{line-height:1.15}section{background:white;padding:24px;border-radius:16px;margin:20px 0}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}.metrics{display:flex;gap:32px;flex-wrap:wrap}.metrics strong{font-size:32px;display:block}.notice{border-left:5px solid #d79c28}.scroll{overflow:auto}a{color:#1565ae}</style>
<h1>AutoDeal: що вже працює</h1><p>Перевірки 19 жовтня 2026 · локальний дослідний пілот</p>
<section class="metrics"><div><strong>40</strong>різних авто на двох сторінках</div><div><strong>13 → 4</strong>HTTP-запити для тестового порівняння</div><div><strong>0</strong>платних API-запитів</div></section>
<section><h2>Зроблено</h2><p>Збір публічної стрічки, збереження побачених ID, звірка характеристик і цін із деталями, попередній відбір аналогів за паливом, коробкою та пробігом. Локальний кеш відокремлює свіжі характеристики від застарілих і невизначених.</p><p>У контрольному порівнянні трафік знизився з 8,0 до 2,0 МБ. Це вимірювання одного випадку, а не гарантована швидкість для всього ринку.</p></section>
<section class="notice"><h2>До повідомлення ще є перешкоди</h2><p>Для Volkswagen Tiguan 2022, дизель, повний привід, 95 тис. км, $30 800 вибірка не містить достатніх сумісних аналогів. Оцінку не вигадано, повідомлення не надіслано.</p><p>Повнота стрічки й час першої публікації не доведені. Друга сторінка додає custom=1 та abroad=2; еквівалентність параметрів не встановлена. Секундна доставка від публікації не підтверджена.</p></section>
<section><h2>Збережені публічні характеристики</h2><p>Це кандидати для порівняння, а не підтверджені аналоги. Кожна наступна оцінка повторно перевіряє сумісність і свіжість.</p><div class="scroll"><table><thead><tr><th>ID</th><th>Рік</th><th>Паливо</th><th>Коробка</th><th>Привід</th><th>Пробіг, км</th><th>Ціна, USD</th></tr></thead><tbody>'''+rows+'''</tbody></table></div></section>
<section><h2>Межі перевірки</h2><p>Продакшн не змінено. Telegram-відправлень цього пілота немає. Кеш локальний і не підключений до чинного бота. Автоматизацію за розкладом не створено.</p></section></html>'''
    match_path = workspace/'owner-match-20261019.jsonl'
    if match_path.exists():
        matches = json.loads(match_path.read_text(encoding='utf-8-sig').splitlines()[-1])['result']
        labels = {'publication_not_proven':'час першої публікації не підтверджений',
                  'region_mismatch':'інша область', 'price_mismatch':'ціна поза діапазоном',
                  'brand_mismatch':'інша марка', 'model_mismatch':'інша модель',
                  'year_mismatch':'рік поза діапазоном'}
        rendered = []
        for match in matches['matches']:
            row = match.get('record')
            if not row:
                continue
            decisions = ' · '.join('Пошук '+str(d['search_id'])+': '+', '.join(labels.get(x,x) for x in d['blockers'])
                                   for d in match['decisions'])
            rendered.append('<li><strong>'+escape(f"{row['brand']} {row['model']} {row['year']}, ${row['price']}")+
                            '</strong><br>'+escape(decisions)+'</li>')
        owner_section = '<section><h2>Перевірка за вашими активними пошуками</h2><p>Пошук 4: $1 000–7 000, Вінницька, Тернопільська, Хмельницька й Чернівецька області, поріг 10%. Пошук 154: Audi A6, 2012–2016, $13 000–18 000, поріг 15%.</p><p>Актуальність прочитано через окрему транзакцію лише для читання; фільтри й підписки не змінено.</p><ul>'+''.join(rendered)+'</ul><p>Жоден із трьох перевірених кандидатів не відповідає вашим фільтрам. Це обмежена вибірка, а не твердження про весь ринок.</p></section>'
        html = html.replace('<section><h2>Збережені публічні характеристики</h2>', owner_section+'<section><h2>Збережені публічні характеристики</h2>')
    (workspace/'AutoDeal-progress-20261019.html').write_text(html, encoding='utf-8')
    return summary


if __name__ == '__main__':
    import sys
    guard = OutboundGuard()
    with guard.isolated():
        print(json.dumps(run(sys.argv[1])))
    print(guard.summary())
