from unittest import TestCase
from experiments.free_search.drivetrain_audit import audit_drivetrain,drivetrain_evidence
from experiments.free_search.tests.test_public_details import BASE,ID,html

class DrivetrainAuditTests(TestCase):
    def test_unknown_is_not_agreement(self):
        for a,b in [(None,None),('повний',None),(None,'повний')]:
            r=audit_drivetrain({'listing_id':'1','drive':a},[{'listing_id':'2','drive':b}])
            self.assertEqual(r['unknown_count'],1)
            self.assertEqual(r['same_count'],0)

    def test_known_conflict_is_comparator_concern_not_vehicle_ban(self):
        r=audit_drivetrain({'listing_id':'1','drive':'повний'},[{'listing_id':'2','drive':'передній'},{'listing_id':'3','drive':'повний'}])
        self.assertEqual((r['same_count'],r['conflict_count']),(1,1))
        self.assertFalse(r['exclude_whole_vehicle'])
        self.assertFalse(r['policy_changed'])

    def test_hidden_duplicate_and_unmapped_labels_stay_unknown(self):
        variants=['<div hidden><div id="descDriveTypeDriveType">Повний</div></div>',
                  '<div id="descDriveTypeDriveType">Повний</div>'*2,
                  '<div id="descDriveTypeDriveType">4motion</div>']
        for extra in variants:self.assertIsNone(drivetrain_evidence(html(BASE)+extra,ID)['drive'])
        self.assertEqual(drivetrain_evidence(html(BASE)+'<div id="descDriveTypeDriveType">Повний</div>',ID)['drive'],'повний')

    def test_repeated_or_target_id_cannot_inflate_audit(self):
        with self.assertRaises(ValueError):audit_drivetrain({'listing_id':'1'},[{'listing_id':'1'}])
        with self.assertRaises(ValueError):audit_drivetrain({'listing_id':'1'},[{'listing_id':'2'},{'listing_id':'2'}])

    def test_eight_nominal_peers_four_same_drive_remain_unknown(self):
        from experiments.free_search.drivetrain_audit import same_drive_scenario
        from experiments.free_search.valuation import estimate
        subject=dict(listing_id='100',brand='A',model='B',year=2016,price=10000,currency='USD',observed_at=1000)
        peers=[dict(subject,listing_id=str(i),price=15000+i*100) for i in range(1,9)]
        evidence=[dict(listing_id=str(i),drive='повний' if i<=4 else 'передній') for i in range(1,9)]
        self.assertEqual(estimate(subject,peers,1000).status,'estimated')
        result=same_drive_scenario(subject,peers,dict(listing_id='100',drive='повний'),evidence,1000)
        self.assertEqual(result['outcome']['status'],'unknown')
        self.assertEqual(result['outcome']['sample_count'],4)
        self.assertEqual(len(peers),8)
        self.assertFalse(result['listing_filter_changed'])

    def test_feature_evidence_cannot_belong_to_other_subject(self):
        from experiments.free_search.drivetrain_audit import same_drive_scenario
        with self.assertRaises(ValueError):same_drive_scenario({'listing_id':'1'},[],{'listing_id':'9'},[],1000)
