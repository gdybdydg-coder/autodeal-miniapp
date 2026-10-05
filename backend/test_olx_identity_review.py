"""Synthetic reviewed-photo evidence; no actual identity claims or source I/O."""
from copy import deepcopy
import hashlib
from itertools import combinations
from pathlib import Path
import socket
import sys
import unittest


def denied(*args, **kwargs):raise AssertionError('identity_review_offline_only')


socket.socket.connect=denied
socket.socket.connect_ex=denied
socket.socket.sendto=denied
socket.create_connection=denied
socket.getaddrinfo=denied
if hasattr(socket.socket,'sendmsg'):socket.socket.sendmsg=denied
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.olx_market import identity_review as identity

NOW=1791210000
REVIEWER='synthetic-reviewer'


def fixture():
    cars=[];roles={};members={};receipts={}
    for i,role in enumerate(('target','reference','holdout')):
        car={'source':'olx' if i==0 else 'auto_ria','id':str(100+i),
            'url':'https://www.olx.ua/d/obyavlenie/example-ID100.html' if i==0 else 'https://auto.ria.com/auto_skoda_octavia_'+str(100+i)+'.html',
            'brand':'Skoda','model':'Octavia','generation':'A5','generation_variant':'FL',
            'body':'wagon','fuel':'diesel','engine_cc':1600,'power_hp':105,
            'drive_type':'front','transmission':'manual','year':2010,'mileage_km':270000,
            'research_condition':'seller_declared_running','checked_at':NOW-20,
            'photos':['https://cdn.example.test/car'+str(i)+'/one.jpg',
                      'https://cdn.example.test/car'+str(i)+'/two.jpg']}
        key=identity.car_key(car);cars.append(car);roles[key]=role
        evidence=[]
        for p in car['photos']:
            url_hash=hashlib.sha256(p.encode()).hexdigest()
            content_hash=hashlib.sha256(('synthetic-image-bytes:'+p).encode()).hexdigest()
            evidence.append({'url_sha256':url_hash,'content_sha256':content_hash})
            receipts[url_hash]={'url_sha256':url_hash,'http_status':200,
                'truncated':False,'checked_at':NOW-10,'content_sha256':content_hash,
                'content_type':'image/jpeg','content_bytes':240000,
                'decoded':{'decoder':'Pillow','decoder_version':'12.0.0',
                    'decode_method':'Image.load','format':'JPEG','width':1280,'height':960}}
        members[key]={'role':role,'traits_sha256':identity.traits_binding(car),
            'checked_at':NOW-5,'reviewed_visible_vehicle':True,'conflicts':[],'photos':evidence}
    pairs=[]
    for left,right in combinations(sorted(roles),2):
        pairs.append({'left':left,'right':right,'status':'distinct','conflicts':[],
            'material_distinctions':[{'feature':'body_mark_pattern',
                'left_photo_sha256':members[left]['photos'][0]['content_sha256'],
                'right_photo_sha256':members[right]['photos'][0]['content_sha256'],
                'left_visible':'synthetic front-left panel mark',
                'right_visible':'synthetic right-rear panel mark'}]})
    record={'version':identity.VERSION,'method':identity.METHOD,'reviewer_id':REVIEWER,
        'checked_at':NOW-5,'population_sha256':identity.population_binding(cars,roles),
        'conflicts':[],'unknowns':[],'members':members,'pairs':pairs}
    return cars,roles,record,receipts


def check(data,*,now=NOW,trusted=(REVIEWER,)):
    cars,roles,record,receipts=data
    return identity.audit(cars,roles,record,now,trusted_reviewers=trusted,photo_receipts=receipts)


class IdentityReview(unittest.TestCase):
    def test_explicit_full_population_review_can_be_documented_without_vin(self):
        result=check(fixture())
        self.assertEqual(result['status'],'manual_review_documented')
        self.assertEqual(result['independent_advertised_vehicles_reviewed'],3)
        self.assertFalse(result['physical_inspection_verified'])
        self.assertFalse(result['automatic_identity_inference'])
        self.assertTrue(all(k.startswith('reviewed-vehicle-sha256:') for k in result['identity_keys'].values()))

    def test_counterfeit_or_omitted_reviewer_does_not_authorize_identity(self):
        data=fixture();data[2]['reviewer_id']='forged-reviewer'
        self.assertIn('identity_reviewer_untrusted',check(data)['reasons'])
        self.assertIn('identity_reviewer_untrusted',check(fixture(),trusted=())['reasons'])
        self.assertIn('identity_reviewer_untrusted',check(fixture(),trusted=REVIEWER)['reasons'])

    def test_malformed_population_or_missing_photo_receipts_holds(self):
        data=fixture()
        for cars,roles in ((None,data[1]),(data[0],None),([None],data[1])):
            result=identity.audit(cars,roles,data[2],NOW,trusted_reviewers=(REVIEWER,),photo_receipts=data[3])
            self.assertEqual(result['status'],'identity_review_pending')
        result=identity.audit(*data[:3],NOW,trusted_reviewers=(REVIEWER,))
        self.assertIn('identity_source_photo_receipt_unverified',result['reasons'])

    def test_id_or_hash_only_distinction_is_not_vehicle_evidence(self):
        for feature in ('different_ids','different_urls','photo_hash','parking_location'):
            with self.subTest(feature=feature):
                data=fixture();data[2]['pairs'][0]['material_distinctions'][0]['feature']=feature
                self.assertEqual(check(data)['status'],'identity_review_pending')

    def test_every_target_reference_control_pair_is_required(self):
        data=fixture();data[2]['pairs'].pop()
        self.assertIn('identity_pair_review_incomplete',check(data)['reasons'])

    def test_wrong_car_and_changed_traits_invalidate_review(self):
        for field,value in (('id','999'),('power_hp',110),('generation_variant','pre_FL'),('research_condition','running_body_repair')):
            with self.subTest(field=field):
                data=fixture();data[0][0][field]=value
                self.assertEqual(check(data)['status'],'identity_review_pending')

    def test_source_photo_url_and_content_are_bound_to_actual_receipts(self):
        for mutation in ('url','content','missing_receipt','truncated','receipt_future'):
            with self.subTest(mutation=mutation):
                data=fixture();member=next(iter(data[2]['members'].values()));photo=member['photos'][0]
                if mutation=='url':photo['url_sha256']='a'*64
                elif mutation=='content':photo['content_sha256']='b'*64
                elif mutation=='missing_receipt':data[3].clear()
                elif mutation=='truncated':data[3][photo['url_sha256']]['truncated']=True
                else:data[3][photo['url_sha256']]['checked_at']=NOW+1
                self.assertEqual(check(data)['status'],'identity_review_pending')

    def test_http_200_nonimage_or_invalid_decoder_evidence_is_held(self):
        for mutation in ('html','zero_bytes','boolean_bytes','oversize','wrong_url','nonimage_format',
                'zero_width','boolean_width','huge_height','pixel_limit','boolean_decoder',
                'missing_decoder','header_only','empty_decoder_version','mime_format_mismatch'):
            with self.subTest(mutation=mutation):
                data=fixture();receipt=next(iter(data[3].values()))
                if mutation=='html':receipt['content_type']='text/html'
                elif mutation=='zero_bytes':receipt['content_bytes']=0
                elif mutation=='boolean_bytes':receipt['content_bytes']=True
                elif mutation=='oversize':receipt['content_bytes']=8*1024*1024+1
                elif mutation=='wrong_url':receipt['url_sha256']='a'*64
                elif mutation=='nonimage_format':receipt['decoded']['format']='HTML'
                elif mutation=='zero_width':receipt['decoded']['width']=0
                elif mutation=='boolean_width':receipt['decoded']['width']=True
                elif mutation=='huge_height':receipt['decoded']['height']=100000
                elif mutation=='pixel_limit':receipt['decoded'].update(width=8192,height=8192)
                elif mutation=='boolean_decoder':receipt['decoded']=True
                elif mutation=='missing_decoder':receipt.pop('decoded')
                elif mutation=='header_only':receipt['decoded']['decode_method']='Image.open'
                elif mutation=='empty_decoder_version':receipt['decoded']['decoder_version']=''
                else:receipt['decoded']['format']='PNG'
                self.assertEqual(check(data)['status'],'identity_review_pending')

    def test_explicit_supported_image_formats_and_decoder_metadata_are_bound(self):
        for mime,format_name in (('image/jpeg','JPEG'),('image/png','PNG'),('image/webp','WEBP')):
            with self.subTest(mime=mime):
                data=fixture()
                for receipt in data[3].values():
                    receipt['content_type']=mime;receipt['decoded']['format']=format_name
                original=check(data)
                self.assertEqual(original['status'],'manual_review_documented')
                next(iter(data[3].values()))['decoded']['width']=1200
                revised=check(data)
                self.assertNotEqual(original['source_photo_receipts_sha256'],revised['source_photo_receipts_sha256'])
                self.assertNotEqual(original['review_sha256'],revised['review_sha256'])

    def test_member_review_time_is_fresh_and_after_photo_receipt(self):
        for mutation in ('missing','future','stale','before_receipt','after_population_review'):
            with self.subTest(mutation=mutation):
                data=fixture();member=next(iter(data[2]['members'].values()))
                if mutation=='missing':member.pop('checked_at')
                elif mutation=='future':member['checked_at']=NOW+1
                elif mutation=='stale':member['checked_at']=NOW-24*3600-1
                elif mutation=='before_receipt':member['checked_at']=NOW-11
                else:member['checked_at']=NOW-4
                self.assertEqual(check(data)['status'],'identity_review_pending')

    def test_stale_future_or_unknown_review_holds(self):
        for mutation in ('stale','future','unknown','conflict','unknown_pair'):
            with self.subTest(mutation=mutation):
                data=fixture()
                if mutation=='stale':data[2]['checked_at']=NOW-24*3600-1
                elif mutation=='future':data[2]['checked_at']=NOW+1
                elif mutation=='unknown':data[2]['unknowns']=['vehicle marks unclear']
                elif mutation=='conflict':data[2]['conflicts']=['possible crosspost']
                else:data[2]['pairs'][0]['status']='unknown'
                self.assertEqual(check(data)['status'],'identity_review_pending')

    def test_target_reference_role_swap_cannot_reuse_population_review(self):
        data=fixture();keys=list(data[1]);data[1][keys[0]],data[1][keys[1]]=data[1][keys[1]],data[1][keys[0]]
        self.assertIn('identity_population_binding_mismatch',check(data)['reasons'])

    def test_known_vin_crosspost_never_becomes_distinct_by_photo_claim(self):
        data=fixture();vin='vin-sha256:'+'a'*64
        for car in data[0][:2]:car.update(vehicle_key=vin,vehicle_identity_verified=True)
        for car in data[0]:data[2]['members'][identity.car_key(car)]['traits_sha256']=identity.traits_binding(car)
        data[2]['population_sha256']=identity.population_binding(data[0],data[1])
        self.assertIn('identity_known_crosspost_in_population',check(data)['reasons'])

    def test_corroborated_vin_key_is_preferred_without_physical_verification_claim(self):
        data=fixture();car=data[0][0];vin='vin-sha256:'+'c'*64
        car.update(vehicle_key=vin,vehicle_identity_verified=False,
            attribute_review={'evidence':{'vehicle_key':{'format_verified':True,
                'basis':'matching_visible_and_public_state_vin_claim','physical_identity_verified':False}}})
        data[2]['members'][identity.car_key(car)]['traits_sha256']=identity.traits_binding(car)
        data[2]['population_sha256']=identity.population_binding(data[0],data[1])
        result=check(data)
        self.assertEqual(result['identity_keys'][identity.car_key(car)],vin)
        self.assertFalse(result['physical_inspection_verified'])

    def test_malformed_vin_corroboration_is_held_not_exception(self):
        for proof in (None,'not-a-review',{'evidence':None},
                {'evidence':{'vehicle_key':'not-evidence'}}):
            with self.subTest(proof=proof):
                data=fixture();car=data[0][0]
                car.update(vehicle_key='vin-sha256:'+'d'*64,
                    vehicle_identity_verified=False,attribute_review=proof)
                data[2]['members'][identity.car_key(car)]['traits_sha256']=identity.traits_binding(car)
                data[2]['population_sha256']=identity.population_binding(data[0],data[1])
                self.assertIn('identity_existing_vin_not_corroborated',check(data)['reasons'])

    def test_reused_source_image_cannot_prove_distinct_vehicles(self):
        data=fixture();members=list(data[2]['members'].values());first=members[0]['photos'][0]['content_sha256']
        second=members[1]['photos'][0];old=second['content_sha256'];second['content_sha256']=first
        data[3][second['url_sha256']]['content_sha256']=first
        for pair in data[2]['pairs']:
            for d in pair['material_distinctions']:
                for side in ('left','right'):
                    if d[side+'_photo_sha256']==old:d[side+'_photo_sha256']=first
        self.assertIn('identity_shared_source_photo',check(data)['reasons'])

    def test_no_production_app_import(self):
        self.assertNotIn('backend.app',sys.modules)


if __name__=='__main__':unittest.main(verbosity=2)
