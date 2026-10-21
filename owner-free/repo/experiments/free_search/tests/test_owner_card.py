import json
from unittest import TestCase
from unittest.mock import patch
import test_owner_queue_sender as fixtures
from experiments.free_search.owner_card import card
from experiments.free_search.owner_card_transport import TelegramCard
from experiments.free_search.owner_price_rule import evaluate
from experiments.free_search.owner_pilot import PublicPage,_page


class OwnerCardTests(TestCase):
    def setUp(self):
        fixtures.QueueSenderTests.setUp(self)
        self.valuation=self.queue.evidence(self.page.listing_id,'range')['source']
        # Use the source's actual lower bound, not a fixture assumption.
        self.valuation.update(evaluate(self.valuation['lower_usd'],_page(self.page,self.now,300)['price'],15))

    def test_compact_fields_and_public_formula(self):
        result=card(self.page,self.valuation,self.review,self.now)
        self.assertTrue(result['text'].startswith('🧪 Тест\n\n🚘 '))
        self.assertIn('💰 Ціна:',result['text'])
        self.assertIn('📊 Ринкова ціна:',result['text'])
        self.assertIn('🔥 Вигода:',result['text'])
        self.assertEqual(result['reply_markup']['inline_keyboard'][0][0]['url'],self.page.url)
        for clutter in ('UTC','Максимальна','Після резерву','ПОРІВНЯННЯ','дослідний'):
            self.assertNotIn(clutter,result['text'])

    def test_wrong_formula_and_identity_block(self):
        for bad in ({'reference_usd':'100000'}, {'source_id':'999'}, {'basis':'paid_api'}):
            with self.assertRaises(ValueError):card(self.page,{**self.valuation,**bad},self.review,self.now)

    def test_condition_notice_and_photo_bound_to_reviewed_page(self):
        url='https://cdn2.riastatic.com/photosnew/auto/photo/skoda__123hd.webp'
        page=PublicPage(self.page.listing_id,self.page.url,
            self.page.html+f'<meta property="og:image" content="{url}">',self.page.received_at)
        review={**fixtures.reviewed(page,self.now,['Sills corroded']), 'card_notice':'Корозія порогів і арок.'}
        result=card(page,self.valuation,review,self.now)
        self.assertEqual(result['photo'],url)
        self.assertIn('⚠️ Корозія порогів і арок.',result['text'])
        with self.assertRaises(ValueError):card(page,self.valuation,self.review,self.now)

    def test_transport_edits_same_receipt_without_send_or_fallback(self):
        transport=TelegramCard('123:'+'a'*25,111)
        result=card(self.page,self.valuation,self.review,self.now)
        result['photo']='https://cdn2.riastatic.com/photosnew/auto/photo/test__1hd.webp'
        with patch.object(transport,'request',return_value={'uncertain':True}) as request:
            self.assertEqual(transport.edit(28057,result),{'uncertain':True})
            self.assertEqual(request.call_count,1)
            args,kw=request.call_args
            self.assertEqual(args[0],'editMessageMedia')
            self.assertEqual(args[1]['message_id'],28057)
            self.assertEqual(kw['expected_message_id'],28057)
        with patch.object(transport,'request') as request:
            with self.assertRaises(ValueError):transport(222,result)
            self.assertFalse(request.called)

    def test_caption_does_not_parse_listing_html(self):
        result=card(self.page,self.valuation,self.review,self.now)
        transport=TelegramCard('123:'+'a'*25,111)
        with patch.object(transport,'request',return_value={'uncertain':True}) as request:
            transport(111,result)
            self.assertNotIn('parse_mode',request.call_args.args[1])

    def test_response_wrong_chat_and_ambiguous_reply_never_retries(self):
        transport=TelegramCard('123:'+'a'*25,111)
        class Response:
            status_code=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_bytes(self):
                yield json.dumps({'ok':True,'result':{'message_id':28057,'chat':{'id':222}}}).encode()
        with patch('httpx.Client') as client:
            client.return_value.__enter__.return_value.stream.return_value=Response()
            self.assertEqual(transport.request('editMessageText',{'text':'test'},expected_message_id=28057),
                             {'uncertain':True})
            self.assertEqual(client.return_value.__enter__.return_value.stream.call_count,1)
