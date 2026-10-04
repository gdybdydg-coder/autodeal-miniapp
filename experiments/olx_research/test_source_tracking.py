from .source_tracking import parse_page
import copy
from experiments.olx_offline.detail_snapshot import parse_detail_snapshot
from experiments.olx_offline.test_observed_price_provenance import AD,page,NOW
from .observations import enrich,asking_price_reasons


def test_observed_ru_pagination_keeps_year_mileage_and_gear():
    html='''<html lang="ru"><body><div data-testid="l-card" id="936643567">
    <a data-testid="card-title-link" href="https://www.olx.ua/d/uk/obyavlenie/example-IDexample.html">Skoda Octavia A5</a>
    <p data-testid="ad-price">6000 $</p><span>2008  370 тыс.км.</span>
    <span>1.90 л.</span><span>Механическая</span><span>Дизель</span>
    </div></body></html>'''.encode()
    c=parse_page(html,fetched_at=1791145600)['listings'][0]
    assert (c['year'],c['mileage_km'],c['transmission'])==(2008,370000,'manual')


def russian_detail(customs='Да'):
    ad=copy.deepcopy(AD);ad['params'][0]['value']='Простая продажа'
    raw=page(ad=ad,terms='Простая продажа',description='Продается целиком. Не на разбор.',vehicle_extra={'name':'Skoda Octavia A5','brand':'Skoda','model':'Octavia','productionDate':'2008'})
    raw=raw.decode().replace('Умови продажу','Условия продажи').replace('Розмитнена: Так','Растаможена: '+customs)
    return raw.replace('</html>', '<p>Пробег: 370 тыс.км.</p><p>Объем двигателя: 1.90 л.</p><p>Коробка передач: Механическая</p><p>Вид топлива: Дизель</p><p>Тип кузова: Универсал</p><p>Техническое состояние: На ходу, технически исправна</p><p>Лакокрасочное покрытие: Не бит, не крашен</p></html>').encode()


def test_ru_detail_keeps_attributes_price_and_technical_state():
    raw=russian_detail();c=enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']
    assert (c['mileage_km'],c['engine_cc'],c['transmission'],c['body'])==(370000,1900,'manual','wagon')
    assert c['research_condition']=='seller_declared_running'
    assert asking_price_reasons(c)==[]


def test_ru_explicit_uncleared_is_not_treated_as_unknown():
    raw=russian_detail('Нет');c=enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']
    assert c['eligibility_review']['status']=='excluded'


def test_ru_exchange_only_is_not_regular_sale():
    ad=copy.deepcopy(AD);ad['params'][0].update(value='Возможен обмен',normalizedValue=['possible_exchange'])
    raw=page(ad=ad,terms='Возможен обмен').replace('Умови продажу'.encode(),'Условия продажи'.encode())
    c=enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']
    assert 'ordinary_sale_not_corroborated' in asking_price_reasons(c)


def test_missing_sale_state_stays_held_without_parser_crash():
    ad=copy.deepcopy(AD);ad['params']=[]
    raw=page(ad=ad)
    c=enrich(raw,parse_detail_snapshot(raw,fetched_at=NOW,truncated=False))['listing']
    assert 'ordinary_sale_not_corroborated' in asking_price_reasons(c)
