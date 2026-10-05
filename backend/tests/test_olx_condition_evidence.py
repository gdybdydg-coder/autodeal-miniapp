"""Whole cars remain eligible; unresolved visible damage is a separate cohort."""
from html import escape
import pytest
from backend.olx_market.observations import enrich


def observed(description):
    html=('<p>Техническое состояние: На ходу, технически исправна</p>'
          '<p>Лакокрасочное покрытие: Незначительные следы эксплуатации (мелкие царапины, сколы)</p>'
          '<div data-testid="ad_description">'+escape(description)+'</div>').encode()
    parsed={'listing':{'id':'936658970','source':'olx',
        'eligibility_review':{'status':'allowed'},'brand':'Skoda','model':'Octavia',
        'title':'Skoda Octavia A5','condition':'normal'},
        'summary':{'download_truncated':False}}
    return enrich(html,parsed)['listing']


def test_saved_real_damage_cue_is_not_clean_running_evidence():
    # Sanitized cue from the freshly retrieved full description; no seller data.
    car=observed('З мінусів після граду є тріщина на лобовому та місцями незначні вмятинки.')
    assert car['eligibility_review']['status']=='allowed'
    assert car['research_condition']=='running_reported_damage:body_dents+windshield_crack'
    assert car['research_evidence']['condition']['description_damage_flags']==['body_dents','windshield_crack']


@pytest.mark.parametrize('description',[
    "Без вм'ятин після граду, без тріщини на лобовому.",
    "Немає тріщин на лобовому та немає вм'ятин.",
    "Нет трещин на лобовом, нет вмятин.",
    "Була тріщина на лобовому, скло замінено. Вм'ятини після граду відремонтовані.",
    "Вмятины после града устранены. Лобовое стекло заменено.",
])
def test_negated_or_completed_damage_is_not_assumed_current(description):
    car=observed(description)
    assert car['eligibility_review']['status']=='allowed'
    assert car['research_condition']=='seller_declared_running'


@pytest.mark.parametrize('description,expected',[
    ("Лобове з тріщиною, скло не замінено.",'windshield_crack'),
    ("Вм'ятини після граду не відремонтовані.",'body_dents'),
    ("Лобовое с трещиной. Вмятины на кузове.",'body_dents+windshield_crack'),
    ("Лобове замінено, але залишились вм'ятини після граду.",'body_dents'),
])
def test_current_damage_has_distinct_condition_without_forbidding_whole_car(description,expected):
    car=observed(description)
    assert car['eligibility_review']['status']=='allowed'
    assert car['research_condition']=='running_reported_damage:'+expected
