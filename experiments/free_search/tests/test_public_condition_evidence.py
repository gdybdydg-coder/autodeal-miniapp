from unittest import TestCase
from experiments.free_search.tests.test_public_details import BASE,ID,html
from experiments.free_search.public_condition_evidence import condition_evidence

class ConditionEvidenceTests(TestCase):
    def test_past_crash_is_not_current_repair_or_exclusion(self):
        result=condition_evidence(html(BASE)+'<span id="badgesDamaged">Був у ДТП</span>',ID)
        self.assertTrue(result['accident_history_asserted'])
        self.assertIsNone(result['current_repair_need_asserted'])
        self.assertFalse(result['exclude_whole_vehicle'])
        self.assertFalse(result['condition_adjusted'])

    def test_absence_and_hidden_badge_are_unknown(self):
        for extra in ['', '<div hidden><span id="badgesDamaged">Був у ДТП</span></div>']:
            self.assertIsNone(condition_evidence(html(BASE)+extra,ID)['accident_history_asserted'])

    def test_inspection_offer_is_not_sound_condition(self):
        result=condition_evidence(html(BASE)+'<div id="descTechStateValue">Продавець готовий до перевірки на СТО</div>',ID)
        self.assertTrue(result['inspection_offered'])
        self.assertIsNone(result['current_repair_need_asserted'])
        self.assertFalse(result['condition_verified'])

    def test_explicit_repair_assertions_preserve_negation(self):
        for text,value in [('Потребує ремонту',True),('Не потребує ремонту',False)]:
            result=condition_evidence(html(BASE)+f'<div id="descTechStateValue">{text}</div>',ID)
            self.assertIs(result['current_repair_need_asserted'],value)
            self.assertFalse(result['exclude_whole_vehicle'])

    def test_ambiguous_badge_and_wrong_id_fail_closed(self):
        result=condition_evidence(html(BASE)+'<span id="badgesDamaged">Був у ДТП</span>'*2,ID)
        self.assertIsNone(result['accident_history_asserted'])
        self.assertIn('ambiguous_badgesDamaged',result['issues'])
        with self.assertRaises(ValueError):condition_evidence(html(BASE),'99999999')
