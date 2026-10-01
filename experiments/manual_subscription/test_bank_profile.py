import json
import os
import tempfile
import unittest
from pathlib import Path

from bank_profile import RecipientProfile, load_profile, normalize_iban, ROOT, MAX_PROFILE_BYTES

# All-zero institution/BBAN: deliberately non-routing, NOT someone's account.
TEST_IBAN = 'UA89' + '0' * 25


def fixture_data():
    return {'schema_version':1, 'recipient_name':'Тестовий отримувач AUTODeal',
            'recipient_code':'0'*10, 'iban':TEST_IBAN, 'bank_name':'ТЕСТОВИЙ БАНК — НЕ ПЕРЕКАЗУВАТИ',
            'mode':'test_only', 'tariff_confirmed':False}


def private_fixture(path, data=None):
    path.write_text(json.dumps(data or fixture_data(),ensure_ascii=False))
    path.chmod(0o600)
    return path


class BankProfileTests(unittest.TestCase):
    def test_format_checksum_and_whitespace_normalization(self):
        grouped = ' '.join(TEST_IBAN[i:i+4] for i in range(0,len(TEST_IBAN),4))
        self.assertEqual(normalize_iban(grouped.lower().replace(' ','\u00a0')),TEST_IBAN)
        self.assertEqual(normalize_iban('\n'+grouped+'\t'),TEST_IBAN)
        for bad in [None,TEST_IBAN+'0',TEST_IBAN[:-1],TEST_IBAN.replace('UA89','UA90'),
                    TEST_IBAN.replace('UA','UА'),TEST_IBAN[:-1]+'/',TEST_IBAN[:10]+'０'*19]:
            with self.assertRaises(ValueError):
                normalize_iban(bad)

    def test_schema_cannot_enable_collection_or_choose_tariff(self):
        for changes in [{'tariff_confirmed':True},{'tariff_confirmed':0},{'mode':'production'},
                        {'schema_version':True},{'amount':1},{'recipient_code':12345678}]:
            with self.assertRaises(ValueError):
                RecipientProfile.parse({**fixture_data(),**changes})

    def test_labels_and_recipient_code_are_validated_without_values_in_errors(self):
        for key,value in [('recipient_name',''),('bank_name','x'*201),
                          ('recipient_name','name\nsecond'),('recipient_code','0'*9),
                          ('recipient_code','１２３４５６７８')]:
            with self.assertRaises(ValueError):
                RecipientProfile.parse({**fixture_data(),key:value})
        for length in (8,10):
            self.assertEqual(RecipientProfile.parse({**fixture_data(),'recipient_code':'0'*length}).recipient_code,'0'*length)

    def test_instruction_has_exact_order_snapshot_and_explicit_unpriced_test_guard(self):
        recipient = RecipientProfile.parse(fixture_data())
        order = 'AD-'+'A'*16
        first = recipient.instruction(order,249,30)
        self.assertIn(order,first['purpose'])
        self.assertEqual((first['amount'],first['days'],first['currency']),(249,30,'UAH'))
        self.assertFalse(first['payments_enabled'])
        self.assertFalse(first['tariff_confirmed'])
        second = recipient.instruction('AD-'+'B'*16,100,7)
        self.assertNotEqual(first['purpose'],second['purpose'])
        self.assertIn('7 днів',second['purpose'])
        with self.assertRaises(ValueError):
            recipient.instruction('<script>',249,30)

    def test_private_profile_load_and_errors_do_not_dump_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = private_fixture(Path(tmp)/'recipient.private.json')
            self.assertEqual(load_profile(path).iban,TEST_IBAN)
            invalid = {**fixture_data(),'iban':TEST_IBAN.replace('UA89','UA90')}
            private_fixture(path,invalid)
            with self.assertRaises(ValueError) as caught:
                load_profile(path)
            self.assertNotIn(invalid['iban'],str(caught.exception))
            self.assertNotIn(invalid['recipient_name'],str(caught.exception))

    def test_profile_permissions_size_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = private_fixture(Path(tmp)/'recipient.private.json')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_profile(path)
            path.chmod(0o600)
            link = Path(tmp)/'link.private.json';link.symlink_to(path)
            with self.assertRaises(OSError):
                load_profile(link)
            path.write_bytes(b' '* (MAX_PROFILE_BYTES+1))
            with self.assertRaises(ValueError):
                load_profile(path)
            with self.assertRaises(ValueError):
                load_profile(ROOT/'private-recipient.private.json')

    def test_duplicate_json_fields_are_rejected_before_destination_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = private_fixture(Path(tmp)/'recipient.private.json')
            raw = json.dumps(fixture_data())
            path.write_text(raw[:-1]+',"iban":"'+TEST_IBAN+'"}')
            with self.assertRaises(ValueError):
                load_profile(path)


if __name__ == '__main__':
    unittest.main()
