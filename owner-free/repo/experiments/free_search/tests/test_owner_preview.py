import unittest
from experiments.free_search.owner_preview import parse_previews, reject_reason
from experiments.free_search.owner_peer_probe import freeze
from experiments.free_search.tests.test_owner_observations import card


class PreviewTests(unittest.TestCase):
    subject = dict(fuel='Дизель', transmission='Автомат', mileage_km=95000)

    def test_prose_and_price_do_not_override_icon_fields(self):
        html = '''<section class="ticket-item" data-advertisement-id="1">
          <li class="item-char"><i class="icon-fuel"></i>Бензин, 1.98 л.</li>
          <li class="item-char"><i class="icon-akp"></i>Автомат</li>
          <li class="item-char"><i class="icon-mileage"></i>73 тис. км</li>
          <p>Дизель 95000 км дешево $1 seller private text</p></section>'''
        row = parse_previews(html)['1']
        self.assertEqual(set(row), {'fuel', 'transmission', 'mileage'})
        self.assertEqual(reject_reason(row, self.subject), 'preview_fuel_mismatch')

    def test_unknown_is_retained_for_detail(self):
        self.assertIsNone(reject_reason({'fuel': 'unknown', 'mileage': 'unknown'}, self.subject))
        self.assertEqual(len(freeze([card()], '9', {}, self.subject)), 1)

    def test_hidden_content_cannot_supply_filter_value(self):
        html = '''<section class="ticket-item" data-advertisement-id="1">
          <li class="item-char"><i class="icon-fuel"></i>
          <span style="display:none">Бензин</span><script>Бензин</script>Дизель</li></section>'''
        self.assertEqual(parse_previews(html)['1']['fuel'], 'Дизель')

    def test_mileage_matches_existing_tolerance(self):
        self.assertIsNone(reject_reason({'mileage': '145 тис. км'}, self.subject))
        self.assertEqual(reject_reason({'mileage': '145,1 тис. км'}, self.subject),
                         'preview_mileage_mismatch')

    def test_selection_reasons_preserved_without_price_ranking(self):
        rejected = {}
        result = freeze([card('1'), card('2')], '9',
                        {'1': {'fuel': 'Бензин'}, '2': {'fuel': 'Дизель'}}, self.subject, rejected)
        self.assertEqual([x[0] for x in result], ['2'])
        self.assertEqual(rejected, {'1': 'preview_fuel_mismatch'})
