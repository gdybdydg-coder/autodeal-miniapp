"""Pure audit of explicitly performed photo reviews, never inferred identity.

The caller must supply trusted source photo receipts and a trusted reviewer
allowlist separately from the review manifest. This validates their binding;
it cannot certify an honest visual judgment or a physical inspection. No VIN,
contacts, photos, source I/O, environment or production state is accessed here.
Financial sample/control/readiness gates remain the caller's responsibility.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit

VERSION='olx-reviewed-vehicle-identity-v1'
METHOD='manual_material_vehicle_distinctions-v1'
MAX_AGE=24*3600
MAX_PHOTO_BYTES=8*1024*1024
MAX_PHOTO_SIDE=8192
MAX_PHOTO_PIXELS=40*1000*1000
IMAGE_FORMATS={'image/jpeg':'JPEG','image/png':'PNG','image/webp':'WEBP'}
TRAITS=('source','id','url','brand','model','generation','generation_variant',
    'body','fuel','fuel_subtype','transmission','engine_cc','power_hp','drive_type',
    'year','mileage_km','research_condition','modification','color','doors','vehicle_key',
    'vehicle_identity_verified','attribute_review')
MATERIAL_FEATURES=frozenset(('body_mark_pattern','exterior_configuration',
    'interior_configuration','visible_vehicle_colour'))
ROLES=frozenset(('target','reference','holdout'))


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
        allow_nan=False).encode()).hexdigest()


def car_key(car):
    if car.get('source') not in ('olx','auto_ria') or not isinstance(car.get('id'),str) or not car['id']:
        raise ValueError('identity_source_and_id_required')
    return car['source']+':'+car['id']


def traits_binding(car):
    return digest({k:car.get(k) for k in TRAITS})


def photo_url_hashes(car):
    photos=car.get('photos')
    if not isinstance(photos,list) or not photos:raise ValueError('identity_photos_missing')
    result=[]
    for url in photos:
        if not isinstance(url,str):raise ValueError('identity_photo_url_invalid')
        p=urlsplit(url)
        if p.scheme!='https' or not p.netloc or p.username or p.password or p.fragment:
            raise ValueError('identity_photo_url_invalid')
        result.append(hashlib.sha256(url.encode()).hexdigest())
    return sorted(set(result))


def population_binding(cars,roles):
    entries={}
    for car in cars:
        key=car_key(car)
        if key in entries:raise ValueError('identity_duplicate_source_id')
        entries[key]={'role':roles.get(key),'traits_sha256':traits_binding(car),
            'photo_url_sha256':photo_url_hashes(car)}
    if set(roles)!=set(entries) or any(v not in ROLES for v in roles.values()):
        raise ValueError('identity_population_roles_mismatch')
    if sum(v=='target' for v in roles.values())!=1:raise ValueError('identity_exactly_one_target_required')
    return digest(entries)


def _sha(value):
    return isinstance(value,str) and bool(re.fullmatch('[a-f0-9]{64}',value))


def _fresh(at,now):
    return type(at) is int and type(now) is int and 0<=now-at<=MAX_AGE


def _vin_corroborated(car):
    if car.get('vehicle_identity_verified') is True:return True
    review=car.get('attribute_review')
    evidence=review.get('evidence') if isinstance(review,dict) else None
    proof=evidence.get('vehicle_key') if isinstance(evidence,dict) else None
    return (isinstance(proof,dict) and proof.get('format_verified') is True
        and proof.get('basis')=='matching_visible_and_public_state_vin_claim')


def _image_receipt_valid(receipt,url_hash,content_hash,now,member_at):
    """Validate caller-trusted full decoder metadata, not a decode-ok boolean.

    The caller must actually execute Pillow Image.load on the complete bytes.
    These typed bounds do not cryptographically prove honest receipts; tighter
    request/byte budgets still belong to the independent source collector.
    """
    if not isinstance(receipt,dict):return False
    mime=receipt.get('content_type');decoded=receipt.get('decoded')
    size=receipt.get('content_bytes');at=receipt.get('checked_at')
    if (receipt.get('url_sha256')!=url_hash or receipt.get('http_status')!=200
            or receipt.get('truncated') is not False
            or receipt.get('content_sha256')!=content_hash
            or not _fresh(at,now) or not _fresh(member_at,now) or at>member_at
            or type(size) is not int or not 0<size<=MAX_PHOTO_BYTES
            or not isinstance(mime,str) or not isinstance(decoded,dict)):
        return False
    expected=IMAGE_FORMATS.get(mime.split(';',1)[0].strip().lower())
    width=decoded.get('width');height=decoded.get('height');version=decoded.get('decoder_version')
    return (expected is not None and decoded.get('format')==expected
        and decoded.get('decoder')=='Pillow' and decoded.get('decode_method')=='Image.load'
        and isinstance(version,str) and bool(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version))
        and type(width) is int and type(height) is int
        and 0<width<=MAX_PHOTO_SIDE and 0<height<=MAX_PHOTO_SIDE
        and width*height<=MAX_PHOTO_PIXELS)


def audit(cars,roles,record,now,*,trusted_reviewers=(),photo_receipts=None):
    """Audit the whole target/reference/holdout population, fail closed.

    Each pair needs explicit material distinctions in reviewed source photos;
    IDs, URLs, filenames, hashes, parking locations and seller differences are
    never vehicle distinctions. Receipts are caller-owned evidence of actual
    HTTP200 complete decoded JPEG/PNG/WebP bytes, not claims accepted from the
    review manifest. Receipt metadata is supplied by a trusted real collector;
    a hash or synthetic decoder declaration does not prove actual image bytes.
    """
    reasons=[];keys={};photos={};chosen={}
    out={'status':'identity_review_pending','reasons':[],
        'independent_advertised_vehicles_reviewed':0,'identity_keys':{},
        'physical_inspection_verified':False,'automatic_identity_inference':False}
    try:
        if (not isinstance(cars,list) or not 1<=len(cars)<=64
                or any(not isinstance(c,dict) for c in cars) or not isinstance(roles,dict)):
            raise ValueError('identity_population_schema_invalid')
        binding=population_binding(cars,roles)
        if not isinstance(record,dict):raise ValueError('identity_review_record_missing')
        if record.get('version')!=VERSION or record.get('method')!=METHOD:
            reasons.append('identity_review_method_invalid')
        reviewer=record.get('reviewer_id')
        if (not isinstance(trusted_reviewers,(tuple,list,set,frozenset))
                or not isinstance(reviewer,str) or not reviewer.strip()
                or reviewer not in trusted_reviewers):
            reasons.append('identity_reviewer_untrusted')
        if not _fresh(record.get('checked_at'),now):reasons.append('identity_review_stale_or_future')
        if record.get('population_sha256')!=binding:reasons.append('identity_population_binding_mismatch')
        if record.get('conflicts')!=[] or record.get('unknowns')!=[]:
            reasons.append('identity_review_unresolved')
        members=record.get('members')
        if not isinstance(members,dict) or set(members)!=set(roles):
            raise ValueError('identity_member_population_mismatch')
        receipts=photo_receipts if isinstance(photo_receipts,dict) else {}
        occupied={};all_content={};used_receipts={}
        for car in cars:
            key=car_key(car);keys[key]=car;member=members[key]
            if not isinstance(member,dict):raise ValueError('identity_member_record_invalid')
            if member.get('role')!=roles[key] or member.get('traits_sha256')!=traits_binding(car):
                reasons.append('identity_member_binding_mismatch')
            if member.get('reviewed_visible_vehicle') is not True or member.get('conflicts')!=[]:
                reasons.append('identity_member_unreviewed_or_conflicting')
            member_at=member.get('checked_at');review_at=record.get('checked_at')
            if (not _fresh(member_at,now) or not _fresh(review_at,now)
                    or member_at>review_at):
                reasons.append('identity_member_review_stale_or_out_of_order')
            source_photos=set(photo_url_hashes(car));evidence=member.get('photos')
            if not isinstance(evidence,list) or len(evidence)<2:
                raise ValueError('identity_two_reviewed_photos_required')
            urls=set();content=set()
            for photo in evidence:
                if not isinstance(photo,dict):raise ValueError('identity_photo_evidence_invalid')
                url_hash=photo.get('url_sha256');content_hash=photo.get('content_sha256')
                receipt=receipts.get(url_hash,{})
                if (url_hash not in source_photos or not _sha(content_hash)
                        or not _image_receipt_valid(receipt,url_hash,content_hash,now,member_at)):
                    reasons.append('identity_source_photo_receipt_unverified')
                else:used_receipts[url_hash]=receipt
                urls.add(url_hash);content.add(content_hash)
                if content_hash in all_content and all_content[content_hash]!=key:
                    reasons.append('identity_shared_source_photo')
                all_content[content_hash]=key
            if len(urls)<2 or len(content)<2:reasons.append('identity_two_distinct_source_photos_required')
            photos[key]=content
            vin=car.get('vehicle_key')
            if vin is not None:
                if not isinstance(vin,str) or not re.fullmatch('vin-sha256:[a-f0-9]{64}',vin):
                    reasons.append('identity_existing_vehicle_key_invalid')
                elif not _vin_corroborated(car):
                    reasons.append('identity_existing_vin_not_corroborated')
                elif vin in occupied:reasons.append('identity_known_crosspost_in_population')
                else:occupied[vin]=key;chosen[key]=vin
        expected={(a,b) for i,a in enumerate(sorted(keys)) for b in sorted(keys)[i+1:]}
        pairs=record.get('pairs');seen=set()
        if not isinstance(pairs,list):raise ValueError('identity_pair_review_missing')
        for pair in pairs:
            if not isinstance(pair,dict):raise ValueError('identity_pair_record_invalid')
            left,right=pair.get('left'),pair.get('right')
            if left not in keys or right not in keys or left==right:
                raise ValueError('identity_pair_outside_population')
            identity=tuple(sorted((left,right)))
            if identity in seen:reasons.append('identity_duplicate_pair_review')
            seen.add(identity)
            if pair.get('status')!='distinct' or pair.get('conflicts')!=[]:
                reasons.append('identity_pair_unknown_or_conflicting')
            distinctions=pair.get('material_distinctions')
            if not isinstance(distinctions,list) or not distinctions:
                raise ValueError('identity_material_vehicle_distinction_missing')
            for item in distinctions:
                if (not isinstance(item,dict) or item.get('feature') not in MATERIAL_FEATURES
                        or item.get('left_photo_sha256') not in photos[left]
                        or item.get('right_photo_sha256') not in photos[right]):
                    reasons.append('identity_material_vehicle_distinction_unverified');continue
                values=(item.get('left_visible'),item.get('right_visible'))
                if (any(not isinstance(v,str) or not v.strip() or len(v)>160 for v in values)
                        or values[0].strip().casefold()==values[1].strip().casefold()):
                    reasons.append('identity_material_vehicle_distinction_unverified')
        if seen!=expected:reasons.append('identity_pair_review_incomplete')
        if not reasons:
            review_hash=digest({'record':record,'source_photo_receipts':used_receipts})
            for key in keys:
                chosen.setdefault(key,'reviewed-vehicle-sha256:'+hashlib.sha256((review_hash+':'+key).encode()).hexdigest())
            out.update(status='manual_review_documented',independent_advertised_vehicles_reviewed=len(keys),
                identity_keys=chosen,population_sha256=binding,review_sha256=review_hash,
                source_photo_receipts_sha256=digest(used_receipts),
                method=METHOD,reviewer_id=reviewer,checked_at=record['checked_at'])
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        reasons.append(str(exc) if isinstance(exc,ValueError) else 'identity_review_schema_invalid')
    out['reasons']=sorted(set(reasons))
    return out
