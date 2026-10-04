"""Pure proposed boundaries. Not imported or registered by the backend."""
from .pipeline import source_selection

# This is a reviewed fixture vocabulary, NOT an OLX numerical dictionary.
ALIASES = {'fuel': {'бензин':'petrol','дизель':'diesel','petrol':'petrol','diesel':'diesel'},
           'transmission': {'механічна':'manual','автоматична':'automatic','manual':'manual','automatic':'automatic'}}
ALIASES['fuel'].update({'газ / бензин':'gas_petrol', 'газ/бензин':'gas_petrol',
                       'гібрид':'hybrid', 'електро':'electric'})
ALIASES['transmission'].update({'варіатор':'cvt', 'типтронік':'tiptronic',
                              'роботизована':'robotized', 'редуктор':'reduction_gear'})
ALIASES['body'] = {'седан':'sedan', 'хетчбек':'hatchback', 'універсал':'wagon',
                   'унiверсал':'wagon', 'купе':'coupe', 'кабріолет':'convertible',
                   'позашляховик / кросовер':'suv', 'позашляховик':'suv',
                   'кросовер':'suv', 'мінівен':'minivan', 'пікап':'pickup',
                   'ліфтбек':'liftback'}

def name(value, field, mapping):
    if not isinstance(value,str) or value.isdecimal():
        raise ValueError('Platform IDs need a verified explicit dictionary')
    label = value.strip().casefold()
    if field == 'region':
        label = label.removesuffix(' область').strip()
    return mapping.get(field,{}).get(label,label)

def filters_from_backend(filters, *, currency, mapping=None):
    """Backend mileage bounds are thousands of km; fixture bounds are km.
    Currency must be supplied explicitly, never guessed from a numeric budget.
    """
    if currency not in ('USD','UAH','EUR'):raise ValueError('Explicit budget currency required')
    mapping=mapping or ALIASES
    out={'currency':currency}
    for field in ('brand','model','region','body','fuel','transmission'):
        value=filters.get(field)
        if value:
            out[field]=[name(v,field,mapping) for v in value] if isinstance(value,list) else name(value,field,mapping)
    for original,target,scale in (('price','price',1),('year','year',1),('mileage','mileage_km',1000)):
        for bound,suffix in (('from','min'),('to','max')):
            v=filters.get(original,{}).get(bound)
            if v is not None:out[target+'_'+suffix]=v*scale
    return {'filters':out,'min_discount':filters.get('minDiscount',15),'sources':source_selection(filters.get('source'))}


def access_gate(uid, *, billing_allowed, ready, search_enabled):
    """Legacy callback fixture; real integration uses paid_source_access.allowed.
    The experiment never imports Settings or connects to the production DB.
    ``billing_allowed`` is the historic callback argument name, not permission
    to substitute gift, administrator or preview access for a confirmed purchase.
    """
    return bool(billing_allowed(uid) and ready(uid) and search_enabled(uid))


def cross_source(a,b):
    if (a['source'],a['id'])==(b['source'],b['id']):return 'same_listing'
    if a['source']==b['source']:return 'distinct_listing'
    # vehicle_key must originate from independently VERIFIED same-vehicle evidence.
    if a.get('vehicle_key') and a['vehicle_key']==b.get('vehicle_key'):
        return 'same_vehicle_review_price_difference'
    if all(a.get(k) is not None and a.get(k)==b.get(k) for k in ('brand','model','year','price','currency')):
        return 'possible_duplicate_keep_both'
    return 'distinct_or_unknown'
