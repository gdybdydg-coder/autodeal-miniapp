"""Pure public AUTO.RIA HTML observations for the separate OLX comparison.

No transport, app, credentials, or production state. Public HTML receipts are
explicitly different from paid ``auto/info`` observations. Catalog/visible facts
are seller/provider claims, never sale prices or an independent inspection.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
from html.parser import HTMLParser
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from .ria_reference import FIELDS, engine_modification, public_url

VERSION = 'olx-ria-public-reference-v1'
MAX_BYTES = 2 * 1024 * 1024
MAX_AGE = 24 * 3600
CATALOG_VERSION = 'ria-public-generation-dictionary-v1'
BOUND_FIELDS = (*FIELDS, 'identity_review', 'source_attribute_evidence', 'photos')
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr'}


def _normal(value):
    return re.sub(r'\s+', ' ', value).strip()


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(size) + '}', value) is not None


class _Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children, self.closed = [], False

    def visible(self):
        n = self
        while n:
            if ('hidden' in n.attrs or n.attrs.get('aria-hidden') == 'true'
                    or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)',
                                 n.attrs.get('style') or '', re.I)):
                return False
            n = n.parent
        return True

    def text(self):
        return ''.join(c if isinstance(c, str) else c.text()
                       for c in self.children)


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        n = _Node(tag, attrs, self.stack[-1] if self.stack else None)
        self.nodes.append(n)
        if self.stack:
            self.stack[-1].children.append(n)
        if tag in VOID:
            n.closed = True
        else:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                self.stack[i].closed = True
                self.stack = self.stack[:i]
                return

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].children.append(data)

    def node(self, ident, required=True):
        nodes = [n for n in self.nodes if n.attrs.get('id') == ident]
        if len(nodes) != 1 or not nodes[0].closed or not nodes[0].visible():
            if required or nodes:
                raise ValueError('ria_public_visible_' + ident + '_unverified')
            return None
        return nodes[0]

    def text(self, ident, required=True):
        node = self.node(ident, required)
        return _normal(node.text()) if node else ''


def binding(car):
    facts = {k: car.get(k) for k in BOUND_FIELDS}
    evidence = car.get('ria_public_reference_evidence')
    facts['public_receipt_metadata'] = ({k: v for k, v in evidence.items() if k != 'binding'}
                                      if isinstance(evidence, dict) else None)
    return hashlib.sha256(json.dumps(facts,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _epoch(value):
    if not isinstance(value, str):
        raise ValueError('ria_public_receipt_timestamp_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed.timestamp()
    except ValueError as exc:
        raise ValueError('ria_public_receipt_timestamp_invalid') from exc


def generation_dictionary_from_public_html(data, receipt, *, now):
    """Corroborate the two previously reviewed catalog IDs, never year rules."""
    url = 'https://auto.ria.com/car/skoda/octavia/'
    if (not isinstance(data, bytes) or not isinstance(receipt, dict)
            or receipt.get('url') != url
            or receipt.get('source') != 'auto_ria_public_html'
            or receipt.get('http_status') != 200 or receipt.get('curl_exit') != 0
            or receipt.get('source_hold') is not None
            or not any(receipt.get(k) is False for k in
                       ('captcha_indicator', 'captcha_challenge_indicator'))
            or any(k in receipt and receipt[k] is not False for k in
                   ('captcha_indicator', 'captcha_challenge_indicator'))
            or receipt.get('redirects_followed') != 0 or receipt.get('get_calls') != 1
            or receipt.get('paid_api_calls') != 0 or receipt.get('body_bytes') != len(data)
            or not 0 < len(data) <= MAX_BYTES
            or receipt.get('body_sha256') != hashlib.sha256(data).hexdigest()
            or not _hex(receipt.get('reserved_commit'), 40)):
        raise ValueError('ria_public_catalog_receipt_unverified')
    started, completed = (_epoch(receipt.get(k)) for k in ('started_at', 'completed_at'))
    if (type(now) is not int or not 0 <= now - int(completed) <= MAX_AGE
            or not 0 <= completed - started <= 20):
        raise ValueError('ria_public_catalog_stale_or_future')
    document = _Document()
    try:
        document.feed(data.decode('utf-8', errors='strict'))
        document.close()
        if [n.attrs.get('href') for n in document.nodes
                if n.tag == 'link' and n.attrs.get('rel') == 'canonical'] != [url]:
            raise ValueError()
        states = []
        for n in document.nodes:
            if n.tag == 'script' and n.closed:
                for m in re.finditer(r'window\.__PINIA__\s*=\s*', n.text()):
                    state, end = json.JSONDecoder().raw_decode(n.text()[m.end():])
                    if n.text()[m.end()+end:].lstrip()[:1] != ';':
                        raise ValueError()
                    states.append(state)
        if len(states) != 1:
            raise ValueError()
        items = states[0]['catalog']['catalog']['rightRelinkPanel']['generationsNew']['items']
        wanted = {3133: ('A5', 'pre_FL', '/car/skoda/octavia/a5/'),
                  3607: ('A5', 'FL', '/car/skoda/octavia/a5-fl/')}
        mapping = {}
        for identifier, (generation, variant, link) in wanted.items():
            matches = [r for r in items if r.get('id') == identifier]
            if len(matches) != 1:
                raise ValueError()
            row = matches[0]
            label = row.get('name')
            visible = [n for n in document.nodes if n.tag == 'a' and n.closed and n.visible()
                       and n.attrs.get('href') in (link, 'https://auto.ria.com' + link)]
            if (not isinstance(label, str) or row.get('link') != link
                    or not visible or any(_normal(n.text()) != label for n in visible)
                    or row.get('nameMobile') != label or row.get('slangWord') != label):
                raise ValueError()
            mapping[label] = {'catalog_id': identifier, 'generation': generation,
                              'generation_variant': variant,
                              'url': 'https://auto.ria.com' + link}
    except (UnicodeError, KeyError, ValueError, TypeError, AttributeError):
        raise ValueError('ria_public_generation_dictionary_uncorroborated') from None
    result = {'version': CATALOG_VERSION, 'source_url': url,
        'checked_at': int(completed), 'body_sha256': receipt['body_sha256'],
        'reserved_commit': receipt['reserved_commit'], 'mapping': mapping}
    result['binding'] = hashlib.sha256(json.dumps(result, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return result


def _generation_from_dictionary(label, dictionary, now):
    if dictionary is None:
        return None
    if not isinstance(dictionary, dict):
        raise ValueError('ria_public_generation_dictionary_unverified')
    payload = {k: v for k, v in dictionary.items() if k != 'binding'}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    if (dictionary.get('version') != CATALOG_VERSION or dictionary.get('binding') != digest
            or type(dictionary.get('checked_at')) is not int
            or not 0 <= now - dictionary['checked_at'] <= MAX_AGE):
        raise ValueError('ria_public_generation_dictionary_unverified')
    return dictionary['mapping'].get(label.split(',')[0].strip())


def _own_description_condition_claim(description):
    """Two observed whole-car declarations, with explicit ownership/scope.

    A bare technical-and-visual claim is allowed only at the start of this
    complete listing description. A later clause must explicitly offer the
    seller's own Skoda. Engine-only praise, prior cars and negated claims do not
    establish a whole-car condition. No text leaves this pure adapter.
    """
    text = description.strip()
    patterns = (
        ('whole_car_technical_and_visual_good-v2',
         r'^(?P<claim>(?:(?:авто|автомобиль|машина)\s+)?в\s+технически\s+и\s+визуально\s+'
         r'хорошем\s+состоянии)(?=[.!?\n]|$|,\s*пригнан\s+из\s+Европы\b)'),
        ('own_skoda_serviced_no_investment-v2',
         r'(?:^|[.!?\n])\s*(?P<claim>предлагаю\s+свою\s+шкоду\s+в\s+отличном\s+состоянии,\s+'
         r'которая\s+полностью\s+обслужена\s+и\s+не\s+требует\s+вложений(?:[.!?]|$))'),
    )
    found = []
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            claim = match.group('claim').strip()
            if kind == 'whole_car_technical_and_visual_good-v2':
                # The actual observed claim continues with import/ownership
                # context. It does not permit an arbitrary assertion suffix.
                # Inspect its complete sentence for historical/other-car scope.
                sentence = re.match(r'[^.!?\n]*(?:[.!?]|$)', text)
                if sentence is None:
                    continue
                claim = sentence.group().strip()
                if re.search(r'\b(?:был[аои]?|раньше|ранее|раніше|колись|'
                             r'предыдущ\w*|попередн\w*|друг\w*|чуж\w*)\b|'
                             r'\bдо\s+(?:аварии|дтп|аварії)\b', claim, re.I):
                    continue
            found.append((kind, claim))
    if len(found) != 1:
        return None
    kind, claim = found[0]
    return {'policy': 'explicit_own_whole_car_description-v2', 'pattern': kind,
            'claim_sha256': hashlib.sha256(claim.encode()).hexdigest()}


def _validate_receipt(data, sid, receipt, checked_at, now, provenance):
    if not isinstance(receipt, dict) or not isinstance(provenance, dict):
        raise ValueError('ria_public_receipt_missing')
    url = receipt.get('url')
    identity = {'source': 'auto_ria', 'id': sid, 'url': url}
    if (not public_url(identity) or receipt.get('source') != 'auto_ria_public_html'
            or receipt.get('id') != sid or receipt.get('http_status') != 200
            or receipt.get('curl_exit') != 0 or receipt.get('source_hold') is not None
            or receipt.get('captcha_indicator') is not False
            or receipt.get('redirects_followed') != 0
            or receipt.get('get_calls') != 1 or receipt.get('paid_api_calls') != 0
            or type(receipt.get('body_bytes')) is not int
            or receipt['body_bytes'] != len(data)
            or not 0 < len(data) <= MAX_BYTES
            or receipt.get('body_sha256') != hashlib.sha256(data).hexdigest()
            or not _hex(receipt.get('reserved_commit'), 40)):
        raise ValueError('ria_public_transport_receipt_unverified')
    started, completed = (_epoch(receipt.get(k))
                          for k in ('started_at', 'completed_at'))
    if (completed < started or completed - started > 20
            or type(checked_at) is not int or checked_at != int(completed)
            or type(now) is not int or not 0 <= now - checked_at <= MAX_AGE):
        raise ValueError('ria_public_observation_stale_or_future')
    if (provenance.get('role') not in ('reference', 'holdout', 'source_feasibility')
            or not _hex(provenance.get('frozen_commit'), 40)
            or type(provenance.get('frozen_at')) is not int
            or provenance['frozen_at'] > started):
        raise ValueError('ria_public_split_not_frozen_before_observation')
    params = provenance.get('search_params')
    if (not isinstance(params, dict) or params.get('category_id') != 1
            or params.get('marka_id[0]') != 70 or params.get('model_id[0]') != 652
            or any(not isinstance(k, str)
                   or k.startswith(('price', 'state', 'city', 'region')) for k in params)):
        raise ValueError('ria_public_reference_search_price_or_region_capped')
    return url


def _state(document, sid, url):
    found = []
    for n in document.nodes:
        if n.tag != 'script' or not n.closed:
            continue
        for m in re.finditer(r'window\.__PINIA__\s*=\s*', n.text()):
            try:
                state, end = json.JSONDecoder().raw_decode(n.text()[m.end():])
                if n.text()[m.end() + end:].lstrip()[:1] != ';':
                    raise ValueError()
                found.append(state)
            except (ValueError, TypeError):
                raise ValueError('ria_public_state_malformed') from None
    if len(found) != 1:
        raise ValueError('ria_public_state_ambiguous')
    try:
        state = found[0]
        page = state['page']['structures'][urlsplit(url).path + '/']
        params = page['additionalParams']
        if (page.get('status') != 200 or params.get('autoId') != int(sid)
                or params.get('link') != url or params.get('isActive') is not True):
            raise ValueError()
        return page, params
    except (KeyError, TypeError, ValueError):
        raise ValueError('ria_public_active_state_identity_unverified') from None


def _templates(page, ident):
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get('id') == ident:
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(page.get('templates'))
    if len(found) != 1 or found[0].get('isHide') is not False:
        raise ValueError('ria_public_template_' + ident + '_unverified')
    return found[0]


def _label(document, page, ident, required=True):
    text = document.text(ident, required)
    if not text:
        return ''
    template = _templates(page, ident)
    elements = template.get('elements')
    if not isinstance(elements, list):
        raise ValueError('ria_public_template_text_unverified')
    declared = _normal(''.join(e.get('content', '') for e in elements
                             if e.get('type') == 'Text'))
    if declared != text:
        raise ValueError('ria_public_visible_state_attribute_conflict')
    return text


def reasons(car, now):
    """Admission reasons for this public channel; never an auto/info receipt."""
    if not isinstance(car, dict):
        return ['ria_public_listing_invalid']
    result = []
    evidence = car.get('ria_public_reference_evidence') or {}
    if not isinstance(evidence, dict):
        evidence = {}
    try:
        identity_valid = public_url(car)
        receipt_binding = binding(car)
    except (TypeError, ValueError, AttributeError):
        identity_valid, receipt_binding = False, None
    if (not identity_valid or evidence.get('version') != VERSION
            or evidence.get('method') != 'public_html'
            or evidence.get('http_status') != 200
            or evidence.get('binding') != receipt_binding
            or not _hex(evidence.get('body_sha256'), 64)
            or not _hex(evidence.get('reserved_commit'), 40)
            or type(evidence.get('body_bytes')) is not int
            or not 0 < evidence['body_bytes'] <= MAX_BYTES
            or type(evidence.get('paid_api_calls')) is not int
            or evidence['paid_api_calls'] != 0):
        result.append('ria_public_receipt_unverified')
    try:
        started, completed = (_epoch(evidence.get(k))
                              for k in ('started_at', 'completed_at'))
        if not 0 <= completed - started <= 20 or car.get('checked_at') != int(completed):
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        started = None
        result.append('ria_public_receipt_timestamp_unverified')
    provenance = car.get('reference_provenance')
    if not isinstance(provenance, dict):
        provenance = {}
    if (provenance.get('role') not in ('reference', 'holdout')
            or not _hex(provenance.get('frozen_commit'), 40)
            or type(provenance.get('frozen_at')) is not int
            or started is None or provenance['frozen_at'] > started):
        result.append('ria_public_reference_split_unverified')
    params = provenance.get('search_params')
    if (not isinstance(params, dict) or params.get('category_id') != 1
            or params.get('marka_id[0]') != 70 or params.get('model_id[0]') != 652
            or any(not isinstance(k, str)
                   or k.startswith(('price', 'state', 'city', 'region')) for k in params)):
        result.append('ria_public_reference_search_price_or_region_capped')
    if (type(now) is not int or type(car.get('checked_at')) is not int
            or not 0 <= now - car['checked_at'] <= MAX_AGE):
        result.append('ria_public_observation_stale_or_future')
    for field in ('brand', 'model', 'generation', 'generation_variant', 'body', 'fuel',
                  'transmission', 'drive_type', 'engine_cc', 'power_hp', 'year', 'mileage_km',
                  'research_condition'):
        if car.get(field) is None:
            result.append('ria_public_' + field + '_pending')
    if car.get('fuel') == 'gas_petrol' and car.get('fuel_subtype') not in ('propane', 'methane'):
        result.append('ria_public_fuel_subtype_pending')
    vehicle = car.get('vehicle_key')
    if (car.get('vehicle_identity_verified') is not True or not isinstance(vehicle, str)
            or not re.fullmatch(r'vin-sha256:[a-f0-9]{64}', vehicle)):
        result.append('ria_public_vehicle_identity_pending')
    eligibility = car.get('eligibility_review')
    if not isinstance(eligibility, dict):
        eligibility = {}
    if (car.get('currency') != 'USD' or car.get('category') != 'whole_passenger_car'
            or eligibility.get('status') != 'allowed'
            or eligibility.get('description_complete') is not True):
        result.append('ria_public_asking_or_eligibility_pending')
    for field, low, high in (('engine_cc', 500, 20000), ('power_hp', 1, 2000),
                              ('year', 1900, 2100), ('mileage_km', 0, 2000000)):
        if type(car.get(field)) is not int or not low <= car[field] <= high:
            result.append('ria_public_' + field + '_pending')
    if (car.get('brand') != 'Skoda' or car.get('model') != 'Octavia'
            or car.get('generation') != 'A5'
            or car.get('generation_variant') not in ('pre_FL', 'FL')
            or car.get('body') not in ('wagon', 'liftback', 'hatchback')
            or car.get('fuel') not in ('diesel', 'petrol', 'gas_petrol')
            or car.get('transmission') not in ('manual', 'automatic')
            or car.get('drive_type') not in ('front', 'back', 'full')):
        result.append('ria_public_attribute_dictionary_pending')
    condition = car.get('research_condition')
    if (not isinstance(condition, str) or not re.fullmatch(
            r'(?:seller_declared_running|running_reported_damage|running_body_repair|not_running)'
            r'(?::(?:body_dents|windshield_crack)(?:\+(?:body_dents|windshield_crack))*)?', condition)):
        result.append('ria_public_research_condition_pending')
    try:
        price = Decimal(car.get('price'))
        if not price.is_finite() or not Decimal(500) < price < Decimal(1000000):
            raise ValueError()
    except (ValueError, TypeError, InvalidOperation):
        result.append('ria_public_asking_price_invalid')
    attributes = car.get('source_attribute_evidence')
    if not isinstance(attributes, dict):
        attributes = {}
    dictionary = attributes.get('generation_dictionary')
    if dictionary is not None:
        try:
            payload = {k: v for k, v in dictionary.items() if k != 'binding'}
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if (dictionary.get('version') != CATALOG_VERSION or dictionary.get('binding') != digest
                    or type(dictionary.get('checked_at')) is not int or type(now) is not int
                    or not 0 <= now - dictionary['checked_at'] <= MAX_AGE):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            result.append('ria_public_generation_dictionary_unverified')
    return sorted(set(result))


def from_public_html(data, sid, checked_at, provenance, receipt, *, now,
                     generation_dictionary=None):
    """Public entry point with sanitized failures for malformed source structure."""
    try:
        return _from_public_html(data, sid, checked_at, provenance, receipt, now=now,
                                 generation_dictionary=generation_dictionary)
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError, InvalidOperation):
        raise ValueError('ria_public_document_malformed') from None


def _from_public_html(data, sid, checked_at, provenance, receipt, *, now,
                     generation_dictionary=None):
    """Project a complete reserved public document into sanitized source claims.

    Unknown critical attributes remain ``None`` plus explicit admission reasons.
    Body/title/year never supply a missing power, variant, fuel subtype or VIN.
    Distinct-photos review remains false until a separate human/visual review.
    A source_feasibility observation is parseable but reasons() always keeps it
    outside an admitted frozen reference/holdout cohort.
    """
    from ..valuation import vehicle_key as validate_vin
    from experiments.olx_offline.eligibility import review_listing
    from .observations import description_damage, octavia_variant
    if not isinstance(data, bytes) or not isinstance(sid, str):
        raise ValueError('ria_public_document_type_invalid')
    url = _validate_receipt(data, sid, receipt, checked_at, now, provenance)
    try:
        text = data.decode('utf-8', errors='strict')
    except UnicodeError:
        raise ValueError('ria_public_document_encoding_invalid') from None
    document = _Document()
    document.feed(text)
    document.close()
    canonical = [n.attrs.get('href') for n in document.nodes
                 if n.tag == 'link' and n.attrs.get('rel') == 'canonical']
    if canonical != [url]:
        raise ValueError('ria_public_canonical_identity_unverified')
    page, params = _state(document, sid, url)
    structured = []
    for n in document.nodes:
        if n.tag == 'script' and n.closed and n.attrs.get('type') == 'application/ld+json':
            try:
                item = json.loads(n.text())
            except ValueError:
                raise ValueError('ria_public_schema_malformed') from None
            if isinstance(item, dict) and item.get('@type') == 'Vehicle' and 'offers' in item:
                structured.append(item)
    if len(structured) != 1 or structured[0] != page.get('ldJSON'):
        raise ValueError('ria_public_vehicle_schema_ambiguous_or_conflicting')
    vehicle = structured[0]
    if any(vehicle.get(k) != url for k in ('@id', 'url', 'mainEntityOfPage')):
        raise ValueError('ria_public_schema_identity_conflict')
    if vehicle.get('brand', {}).get('name') != 'Skoda' or vehicle.get('model') != 'Octavia':
        raise ValueError('ria_public_reference_group_not_reviewed')
    title = _label(document, page, 'basicInfoTitle')
    year = vehicle.get('productionDate')
    if (type(year) is not int or not 1900 <= year <= 2100
            or title != 'Skoda Octavia ' + str(year)
            or params.get('title') != title or vehicle.get('name') != title):
        raise ValueError('ria_public_title_year_identity_conflict')
    offer = vehicle.get('offers', {})
    visible_price = _label(document, page, 'basicInfoPrice')
    price_tokens = re.findall(r'(?<!\w)([\d\s]+)\s*\$', visible_price)
    try:
        price = Decimal(str(offer.get('price')))
        declared_usd = Decimal(re.sub(r'\s+', '', params['prices']['USD']))
        displayed_usd = Decimal(re.sub(r'\s+', '', price_tokens[0])) if len(price_tokens) == 1 else None
    except (InvalidOperation, KeyError, TypeError):
        raise ValueError('ria_public_price_basis_unverified') from None
    if (not price.is_finite() or price <= 500 or price >= 1000000
            or price != declared_usd or price != displayed_usd
            or offer.get('priceCurrency') != 'USD'
            or offer.get('availability') != 'https://schema.org/InStock'):
        raise ValueError('ria_public_price_or_active_offer_conflict')
    description = vehicle.get('description')
    node = document.node('descDescription')
    template_description = _templates(page, 'descDescription').get('component', {}).get(
        'expandableText', {}).get('description', {}).get('content')
    # Buttons belong to the container; only its actual description span counts.
    description_nodes = [n for n in document.nodes if n.parent
        and n.tag == 'span' and n.closed and n.visible()
        and node in _ancestors(n) and 'ws-pre-wrap' in (n.attrs.get('class') or '').split()]
    if (not isinstance(description, str) or not description.strip()
            or not isinstance(template_description, str)
            or _normal(template_description) != _normal(description)
            or len(description_nodes) != 1
            or _normal(description_nodes[0].text()) != _normal(description)):
        raise ValueError('ria_public_full_description_unverified')
    body_label = _label(document, page, 'descCharacteristicsValue')
    body = {'Универсал': 'wagon', 'Універсал': 'wagon', 'Лифтбек': 'liftback',
            'Ліфтбек': 'liftback', 'Хэтчбек': 'hatchback', 'Хетчбек': 'hatchback'}.get(
                body_label.split('•')[0].strip())
    if vehicle.get('bodyType') not in ('Легковые', 'Легкові'):
        raise ValueError('ria_public_whole_passenger_category_unverified')
    eligibility = review_listing({'title': title, 'category': 'whole_passenger_car',
        'description': description, 'description_available': True})
    if eligibility['status'] != 'allowed':
        raise ValueError('ria_public_full_description_not_eligible')
    for clause in re.split(r'[.!?;,\n]', description.casefold()):
        for match in re.finditer(r'перш\w*\s+внес\w*|перв\w*\s+взнос\w*', clause):
            if (not re.search(r'(?:не|без)\s+(?:\w+\s+){0,2}$', clause[max(0, match.start()-30):match.start()])
                    and re.search(r'ціна|цена', clause)):
                raise ValueError('ria_public_initial_payment_not_full_asking')
    generation_label = _label(document, page, 'descGenerationBaseValue', False)
    generation = 'A5' if re.search(r'(?<!\w)A5(?!\w)', generation_label, re.I) else None
    variant = octavia_variant(generation_label) if generation == 'A5' else None
    dictionary_row = _generation_from_dictionary(generation_label, generation_dictionary, now)
    if dictionary_row:
        if generation != dictionary_row['generation'] or (variant
                and variant != dictionary_row['generation_variant']):
            raise ValueError('ria_public_generation_dictionary_label_conflict')
        variant = dictionary_row['generation_variant']
    # An A5 label alone stays unknown unless independently acquired public
    # dictionary corroborates its exact catalog ID. Year is deliberately unused.
    engine_label = _label(document, page, 'descEngineEngine', False)
    fuel_name, separator, engine_details = engine_label.partition(',')
    fuel_map = {'Бензин': ('petrol', None, 1), 'Дизель': ('diesel', None, 2),
        'Газ пропан-бутан / Бензин': ('gas_petrol', 'propane', 4),
        'Газ метан / Бензин': ('gas_petrol', 'methane', 8)}
    fuel, subtype, fuel_id = fuel_map.get(fuel_name.strip(), (None, None, None))
    if (fuel_name and (vehicle.get('fuelType') != fuel_name.strip()
            or vehicle.get('vehicleEngine', {}).get('fuelType') != fuel_name.strip())):
        raise ValueError('ria_public_fuel_attribute_conflict')
    volume_match = re.fullmatch(
        r'\s*(\d{1,2}(?:[.,]\d{1,3})?)\s*л\.?'
        r'(?:\s*,\s*(\([^()]+\)))?\s*', engine_details) if separator else None
    cc = int(Decimal(volume_match[1].replace(',', '.')) * 1000) if volume_match else None
    power_tokens = re.findall(r'\((\d{2,4})\s*(?:к\.?\s*с\.?|л\.?\s*с\.?)\)', generation_label)
    power = int(power_tokens[0]) if len(power_tokens) == 1 and 1 <= int(power_tokens[0]) <= 2000 else None
    visible_power = None
    visible_kw = None
    if volume_match and volume_match[2]:
        engine_power = re.fullmatch(
            r'\(\s*(\d{1,4}(?:[.,]\d+)?)\s*(?:к\.?\s*с\.?|л\.?\s*с\.?)'
            r'\s*/\s*(\d{1,4}(?:[.,]\d+)?)\s*кВт\s*\)', volume_match[2])
        if engine_power:
            visible_power = Decimal(engine_power[1].replace(',', '.'))
            visible_kw = engine_power[2].replace(',', '.')
            if not 1 <= visible_power <= 2000:
                raise ValueError('ria_public_power_attribute_conflict')
            if power is not None and visible_power.quantize(Decimal('1'), rounding=ROUND_HALF_UP) != power:
                raise ValueError('ria_public_power_attribute_conflict')
            # A missing catalog power can use an explicit INTEGER visible hp
            # claim. Fractional conversion values are retained as evidence;
            # they never invent an unobserved source catalog integer.
            if power is None and visible_power == visible_power.to_integral_value():
                power = int(visible_power)
    transmission_label = _label(document, page, 'descTransmissionTransmission', False)
    transmission = {'Ручная / Механика': 'manual', 'Ручна / Механіка': 'manual',
                    'Автомат': 'automatic'}.get(transmission_label)
    if transmission_label and vehicle.get('vehicleTransmission') != transmission_label:
        raise ValueError('ria_public_transmission_attribute_conflict')
    drive_label = _label(document, page, 'descDriveTypeDriveType', False)
    drive = {'Передний': 'front', 'Передній': 'front', 'Задний': 'back',
             'Задній': 'back', 'Полный': 'full', 'Повний': 'full'}.get(drive_label)
    mileage_label = _label(document, page, 'basicInfoTableMainInfo0')
    mileage_match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*тис\.\s*км|'
                                r'(\d+(?:[.,]\d+)?)\s*тыс\.\s*км', mileage_label)
    mileage = vehicle.get('mileageFromOdometer', {})
    mileage_km = mileage.get('value')
    if (not mileage_match or type(mileage_km) is not int or mileage.get('unitCode') != 'KMT'
            or mileage_km != int(Decimal(next(v for v in mileage_match.groups() if v).replace(',', '.')) * 1000)):
        raise ValueError('ria_public_mileage_attribute_conflict')
    technical_label = _label(document, page, 'descTechStateText', False)
    paint_label = _label(document, page, 'descPaintConditionValue', False)
    damage = description_damage([SimpleNamespace(attrs={'data-testid': 'ad_description'},
                                                 closed=True, text=lambda: description)])
    condition = ('seller_declared_running' if technical_label in
        ('Полностью неповрежденное', 'Повністю непошкоджене') else None)
    own_condition_claim = _own_description_condition_claim(description)
    description_condition_used = False
    if not technical_label and not paint_label and own_condition_claim:
        condition = 'seller_declared_running'
        description_condition_used = True
    if condition and paint_label.startswith(('Требует восстановления', 'Потребує відновлення',
                                             'Потрібно відновлення')):
        condition = 'running_body_repair'
    description_folded = description.casefold()
    unsupported_condition = []
    if re.search(r'\bне\s+на\s+ходу\b|\bне\s+(?:заводить\w*|заводит\w*)\b', description_folded):
        condition = 'not_running'
    for code, pattern in (
        ('engine_repair', r'\b(?:(?:потребує|потріб\w*|требует|нуж\w*)\s+(?:капітальн\w*\s+|капитальн\w*\s+)?ремонт\w*\s+(?:двигун\w*|двигател\w*)'
         r'|(?:двигун\w*|двигател\w*)\s+(?:потребує|требует)\s+(?:капітальн\w*\s+|капитальн\w*\s+)?ремонт\w*)'),
        ('body_repair', r'\b(?:(?:потребує|потріб\w*|требует|нуж\w*)\s+(?:кузовн\w*\s+ремонт\w*|ремонт\w*\s+кузов\w*)'
         r'|кузов\w*\s+(?:потребує|требует)\s+ремонт\w*)'),
        ('body_condition_nuance', r'\b(?:моменты|моменти|нюансы|нюанси)\s+(?:по|з|с)\s+кузов\w*'),
    ):
        for match in re.finditer(pattern, description_folded):
            if not re.search(r'(?:не|без|нет|немає|нема)\s+(?:\w+\s+){0,2}$',
                             description_folded[max(0, match.start()-40):match.start()]):
                unsupported_condition.append(code)
    if unsupported_condition:
        condition = None
    if condition and damage:
        condition = ('running_reported_damage' if condition == 'seller_declared_running'
                     else condition) + ':' + '+'.join(damage)
    vin = vehicle.get('vehicleIdentificationNumber')
    key = validate_vin(vin)
    visible_vin = document.text('badgesVin', False)
    if visible_vin and visible_vin != (vin or '').strip().upper():
        raise ValueError('ria_public_vehicle_identity_conflict')
    identity_verified = bool(key and visible_vin == vin.strip().upper()) if isinstance(vin, str) else False
    modification, modification_evidence = engine_modification(generation_label, {
        'engine_cc': cc, 'fuel_id': fuel_id}) if generation_label else (None, {})
    photos = []
    for img in (page.get('photoLdJSON') or {}).get('image', []):
        value = img.get('contentUrl')
        parsed = urlsplit(value or '')
        if (parsed.scheme == 'https' and re.fullmatch(r'cdn\d+\.riastatic\.com', parsed.netloc)
                and parsed.path.startswith('/photosnew/auto/photo/') and not parsed.query):
            photos.append(value)
    out = {'source': 'auto_ria', 'id': sid, 'url': url, 'price': str(price), 'currency': 'USD',
        'brand': 'Skoda', 'model': 'Octavia', 'generation': generation,
        'generation_variant': variant, 'body': body, 'fuel': fuel, 'fuel_subtype': subtype,
        'transmission': transmission, 'drive_type': drive, 'engine_cc': cc, 'power_hp': power,
        'year': year, 'mileage_km': mileage_km, 'research_condition': condition,
        'checked_at': checked_at, 'modification': modification,
        'source_modification_evidence': modification_evidence,
        'condition_evidence': {'basis': 'corroborated_complete_public_description_and_technical_label',
            'description_sha256': hashlib.sha256(description.encode()).hexdigest(),
            'description_damage_flags': damage,
            'unsupported_condition_flags': sorted(set(unsupported_condition)),
            'independently_verified': False},
        'category': 'whole_passenger_car',
        'eligibility_review': {'status': 'allowed', 'description_complete': True},
        'vehicle_key': 'vin-sha256:' + hashlib.sha256(vin.strip().upper().encode()).hexdigest()
                       if identity_verified else None,
        'vehicle_identity_verified': identity_verified,
        'identity_review': {'distinct_photos_reviewed': False}, 'photos': photos,
        'reference_provenance': deepcopy(provenance),
        'source_attribute_evidence': {'basis': 'public_ssr_pinia_and_visible_same_document',
            'generation_label_sha256': hashlib.sha256(generation_label.encode()).hexdigest(),
            'generation_id': dictionary_row['catalog_id'] if dictionary_row else None,
            'generation_variant_basis': 'corroborated_public_catalog_id' if dictionary_row else 'explicit_label_only',
            'generation_dictionary': deepcopy(generation_dictionary) if dictionary_row else None,
            'technical_label': technical_label, 'paint_label': paint_label}}
    out['source_attribute_evidence'].update(
        visible_power_hp=str(visible_power) if visible_power is not None else None,
        visible_power_kw=visible_kw,
        power_consistency_policy='round_explicit_hp_to_source_catalog_integer-v1',
        engine_volume_basis='explicit_visible_engine_volume' if cc else 'engine_volume_not_explicit')
    if description_condition_used:
        out['condition_evidence'].update(
            basis='corroborated_complete_public_own_description_claim',
            own_claim_policy=own_condition_claim['policy'],
            own_claim_pattern=own_condition_claim['pattern'],
            own_claim_sha256=own_condition_claim['claim_sha256'])
    out['ria_public_reference_evidence'] = {'version': VERSION, 'method': 'public_html',
        'http_status': 200, 'body_sha256': receipt['body_sha256'], 'body_bytes': len(data),
        'reserved_commit': receipt['reserved_commit'], 'paid_api_calls': 0,
        'started_at': receipt['started_at'], 'completed_at': receipt['completed_at']}
    out['ria_public_reference_evidence']['binding'] = binding(out)
    return out


def _ancestors(node):
    result = []
    while node.parent:
        node = node.parent
        result.append(node)
    return result
