"""Prepare a no-I/O RIA parameter query for a later separately approved budget.
An OLX listing identifier is NEVER an omniId. Dictionaries must be reviewed.
"""
from decimal import Decimal
import hashlib,json
DOC='https://docs-developers.ria.com/en/used-cars/average_price/auto_ria_average_price_ai'
ENDPOINT='https://developers.ria.com/auto/ai-avarage-price/'
FIELDS={'category':'categoryId','brand':'brandId','model':'modelId','generation':'generationId',
        'body':'bodyId','fuel':'fuelId','transmission':'gearBoxId'}

def prepare(car,dictionaries,now,*,period=168):
    missing=[];params={};provenance=[]
    for field,out in FIELDS.items():
        label='whole_passenger_car' if field=='category' else car.get(field)
        record=dictionaries.get(field,{}).get(label)
        if (not isinstance(record,dict) or record.get('reviewed') is not True
                or not isinstance(record.get('id'),str) or not record['id'].isdecimal()
                or not str(record.get('source','')).startswith(('https://developers.ria.com/','https://docs-developers.ria.com/'))
                or type(record.get('checked_at')) is not int or not 0<=now-record['checked_at']<=30*86400):
            missing.append(field);continue
        params[out]=record['id'];provenance.append({'field':field,'label':label,**record})
    for key in ('year','mileage_km','engine_cc'):
        if type(car.get(key)) is not int:missing.append(key)
    if not missing:
        params.update(year={'gte':str(car['year']-1),'lte':str(car['year']+1)},
                      mileage={'gte':str(Decimal(max(0,car['mileage_km']-30000))/1000),'lte':str(Decimal(car['mileage_km']+30000)/1000)},
                      engineVolume={'gte':str(Decimal(car['engine_cc'])/1000),'lte':str(Decimal(car['engine_cc'])/1000)})
    body={'langId':4,'period':period,'params':params} if not missing else None
    return {'status':'mapping_required' if missing else 'ready_for_budget_review','missing':missing,
            'endpoint':ENDPOINT,'method':'POST','body':body,'source_id_not_sent':car.get('id'),
            'query_cache_key':hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest() if body else None,
            'dictionary_evidence':provenance,'documentation':DOC,'expected_assessment_calls':1 if body else None,
            'price_per_call':None,'quota_units_per_call_verified':False,'account_method_permission_verified':False,
            'execution_authorized':False,'actual_provider_calls':0,
            'limits':['parameter_based_average_not_official_OLX_listing_appraisal',
                      'provider_returns_similarCars_and_statisticData',
                      'returned_peer_comparability_and_current_price_range_require_validation']}
