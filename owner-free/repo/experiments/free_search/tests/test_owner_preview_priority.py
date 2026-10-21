import unittest
from types import SimpleNamespace
from experiments.free_search.public_cards import parse_public_cards
from experiments.free_search.owner_continuous_intake import detail_priority


class PreviewPriorityTests(unittest.TestCase):
    def test_visible_city_excludes_hidden_distance_and_remote_city(self):
        text='''<section class="ticket-item" data-advertisement-id="123">
        <a class="m-link-ticket" href="https://auto.ria.com/uk/auto_skoda_octavia_123.html"></a>
        <li class="item-char view-location js-location"><i></i> Тернопіль
          <span class="js-remote-location" style="display: none;">(30 км від <span>Київ</span>)</span></li>
        </section>'''
        self.assertEqual(parse_public_cards(text)[0].preview_city,'Тернопіль')

    def test_region_hint_reorders_but_keeps_all_candidates(self):
        rows=[{'id':str(i),'first_seen':100+i} for i in range(1,4)]
        previews={'1':SimpleNamespace(preview_city='Київ'),'2':SimpleNamespace(preview_city='Тернопіль')}
        searches=[{'filters':{'region':['Тернопільська область']}}]
        ordered=sorted(rows,key=lambda r:detail_priority(r,previews,searches))
        self.assertEqual([r['id'] for r in ordered],['2','1','3'])
