"""Offline OLX offer eligibility; seller claims are not customs verification.

The caller must supply the COMPLETE description and ``description_available=True``.
Only allowlisted facts and a digest enter the decision; raw description/contact data
are never returned. Unknown customs alone is neither a violation nor clearance.
An ``allowed`` result means these text rules found adequate, noncontradictory
evidence; it is not a guarantee against hidden facts or a dishonest seller.
"""
import hashlib
import json
import re
import unicodedata

VERSION = 'olx-eligibility-v2'
FACT_FIELDS = ('title', 'category', 'customs_status', 'customs_cleared',
               'sale_mode', 'description', 'description_available')
MAX_TEXT = 131072
WHOLE_CATEGORIES = {'whole_passenger_car', 'passenger_car', 'passenger_cars',
                    'легкові автомобілі', 'легковые автомобили'}
PART_CATEGORIES = {'parts', 'car_parts', 'auto_parts', 'spare_parts', 'запчастини',
                   'запчасти', 'автозапчастини', 'автозапчасти'}
OTHER_CATEGORIES = {'rental', 'rent', 'service', 'services', 'wanted'}

UNCLEARED = re.compile(
    r'\b(?:не\s*(?:розмитнен\w*|растаможен\w*|растаможон\w*)'
    r'|без\s+(?:розмитнен\w*|растамож\w*|мита|таможенн\w*\s+оформлен\w*)'
    r'|(?:цін\w*|цен\w*)\s+не\s+(?:включ\w*|врахов\w*|учит\w*)\s+(?:в\s+себ[ея]\s+)?(?:вартіст\w*\s+)?(?:розмитнен\w*|растамож\w*|мит[ао])'
    r'|(?:розмитнен\w*|растамож\w*)\s+не\s+(?:включ\w*|врахован\w*|оплачен\w*|вход\w*)'
    r'|(?:потребує|потребуємо|требует|потрібно|нужно)\s+(?:розмитнен\w*|растамож\w*)'
    r'|не\s+(?:пройш\w*|прош\w*)\s+(?:розмитнен\w*|растамож\w*))\b')
CLEARED = re.compile(
    r'\b(?:розмитнен(?:ий|а|е|і|ого|ою)|растаможен(?:ный|ная|ное|ные|а|о|ы)?'
    r'|українськ\w*\s+реєстраці\w*|украинск\w*\s+регистраци\w*)\b')
DISMANTLING = re.compile(
    r'\b(?:(?:під|на)\s+розб(?:ір|ор)\w*|(?:под|на)\s+разбор\w*'
    r'|(?:авто(?:моб[іи]ль)?|машина)\s+(?:у|в)\s+(?:розбор[іу]|разборе)\b'
    r'|на\s+запчаст(?:ини|ин[иуа]|і|и|ь)\b|по\s+запчаст(?:инах|ям|ям)\b'
    r'|донор(?:а|ом|у|ів|ы|ов|ський|ский)?\b'
    r'|(?:прода\w*|продаж\w*)\s+(?:по\s+частинах|частинами|частями))')
RISKY = re.compile(r'\b(?:євроблях\w*|евроблях\w*|(?:польськ\w*|литовськ\w*|польск\w*|литовск\w*)\s+номер\w*)\b')
PART_OFFER = re.compile(
    r'^\s*(?:прода\w*\s+)?(?:двигун|двигатель|капот|бампер|фара|фари|фары|двері|дверь|кпп|акпп|запчастини|запчасти|розборка|разборка)\b')
COMPONENT_PRICE = re.compile(
    r'\b(?:ціна|цена)\s+(?:лише\s+|только\s+)?(?:за|на)\s+(?:деталь|двигун|двигатель|капот|бампер|фару|двері|дверь|кпп|акпп)\b')


def _normal(value):
    return re.sub(r'\s+', ' ', re.sub(r'[-‐‑‒–—]+', ' ', unicodedata.normalize('NFKC', value).casefold())).strip()


def _snippet(value):
    # Redact before limiting length, so truncation cannot expose part of a token.
    value = re.sub(r'https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[a-z]{2,}', '[redacted]', value, flags=re.I)
    value = re.sub(r'\b[a-hj-npr-z0-9]{17}\b', '[redacted]', value, flags=re.I)
    value = re.sub(r'(?<!\w)\+?\d[\d\s().-]{5,}\d(?!\w)', '[redacted]', value)
    value = re.sub(r'\b(?:id\s*[:=#]?\s*\w+|[a-zа-яіїє]{2}\s*\d{4}\s*[a-zа-яіїє]{2})\b', '[redacted]', value, flags=re.I)
    return value[:96]


def fingerprint(raw):
    """Digest all relevant original facts, including every description character."""
    facts = {key: raw.get(key) for key in FACT_FIELDS}
    serialized = json.dumps({'version': VERSION, 'facts': facts}, ensure_ascii=False,
                            sort_keys=True, default=lambda x: str(type(x)))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _negated(text, match):
    # Deliberately local grammar: 'не працює, на розбір' is NOT a negation.
    before = re.split(r'[,;.!?\n]|\b(?:але|но|проте)\b', text[:match.start()])[-1]
    after = text[match.end():]
    return bool(
        re.search(r'\b(?:не|ні)\s+(?:(?:прода\w*|відда\w*|отда\w*|явля\w*|пропон\w*|предлаг\w*|є)\s+)?$', before)
        or re.match(r'\s+(?:не|ні)\s+(?:прода\w*|відда\w*|отда\w*|є\b|явля\w*|пропон\w*|предлаг\w*)', after)
        or re.match(r'\s*[:=]\s*(?:ні|нет|no)\b', after))


def review_listing(raw):
    """Pure decision with status, reason codes, redacted evidence, and fingerprint.

    Structured vocabulary: category=whole_passenger_car/parts/rental/service/wanted,
    customs_status=cleared/uncleared/unknown; customs_cleared is optional bool;
    sale_mode=whole/donor/dismantling/parts. Localized factual phrases also work.
    Missing/partial description always holds for review. Unknown customs alone
    remains unknown; it does not reject an otherwise adequately described car.
    """
    if not isinstance(raw, dict):
        raise TypeError('Listing must be a mapping of allowlisted facts')
    evidence = []
    reasons = set()
    positive, forbidden, negated_parts = set(), set(), False
    invalid = False

    def add(reason, field, text=''):
        reasons.add(reason)
        item = {'reason': reason, 'field': field, 'snippet': _snippet(text)}
        if len(evidence) < 24 and item not in evidence:
            evidence.append(item)

    description = raw.get('description')
    description_complete = raw.get('description_available') is True and isinstance(description, str) and bool(description.strip())
    if not description_complete:
        add('description_unavailable', 'description_available')

    category = _normal(raw['category']) if isinstance(raw.get('category'), str) else ''
    if category in PART_CATEGORIES:
        forbidden.add('parts'); add('parts_category', 'category', category)
    elif category in OTHER_CATEGORIES:
        forbidden.add('non_vehicle'); add('not_vehicle_sale', 'category', category)
    elif category not in WHOLE_CATEGORIES:
        add('vehicle_category_unconfirmed', 'category', category)

    customs = _normal(raw['customs_status']).replace('_', ' ') if isinstance(raw.get('customs_status'), str) else ''
    if customs in ('cleared', 'customs cleared'):
        positive.add('customs'); add('customs_cleared_declared', 'customs_status', customs)
    elif customs in ('uncleared', 'not cleared', 'not customs cleared'):
        forbidden.add('customs'); add('uncleared_declared', 'customs_status', customs)
    if type(raw.get('customs_cleared')) is bool:
        if raw['customs_cleared']:
            positive.add('customs'); add('customs_cleared_declared', 'customs_cleared', 'true')
        else:
            forbidden.add('customs'); add('uncleared_declared', 'customs_cleared', 'false')
    elif raw.get('customs_cleared') is not None:
        invalid = True; add('invalid_evidence_field', 'customs_cleared')

    mode = _normal(raw['sale_mode']).replace('_', ' ') if isinstance(raw.get('sale_mode'), str) else ''
    if mode in ('donor', 'dismantling', 'parts', 'for parts'):
        forbidden.add('parts'); add('dismantling_declared', 'sale_mode', mode)

    for field in ('title', 'category', 'customs_status', 'sale_mode', 'description'):
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            invalid = True; add('invalid_evidence_field' if not isinstance(value, str) else 'evidence_too_large', field)
            continue
        text = _normal(value)
        bad_spans = []
        for match in UNCLEARED.finditer(text):
            bad_spans.append(match.span())
            # 'не нерозмитнений' is ambiguous, never an affirmative clearance.
            if _negated(text, match):
                add('ambiguous_customs_negation', field, text[match.start():match.end()])
            else:
                forbidden.add('customs'); add('uncleared_text', field, match[0])
        for match in CLEARED.finditer(text):
            if any(start <= match.start() < end for start, end in bad_spans):
                continue
            if _negated(text, match) or re.search(r'\bбез\s+$', text[:match.start()]):
                add('customs_clearance_denied', field, match[0])
            else:
                positive.add('customs'); add('customs_cleared_declared', field, match[0])
        for match in DISMANTLING.finditer(text):
            if _negated(text, match):
                negated_parts = True; add('dismantling_explicitly_denied', field, match[0])
            else:
                forbidden.add('parts'); add('dismantling_text', field, match[0])
        for match in RISKY.finditer(text):
            if not _negated(text, match):
                add('customs_risk_signal', field, match[0])
        for match in COMPONENT_PRICE.finditer(text):
            if not _negated(text, match):
                forbidden.add('parts'); add('component_price_text', field, match[0])
        # A title offering a component is held, not inferred from repair descriptions.
        if field == 'title' and (match := PART_OFFER.search(text)):
            add('possible_component_offer', field, match[0])

    if 'customs' in positive and 'customs' in forbidden:
        add('contradictory_customs_evidence', 'customs_status/title/description')
    if negated_parts and 'parts' in forbidden:
        add('contradictory_sale_evidence', 'sale_mode/title/description')
    if 'customs' not in positive and 'customs' not in forbidden:
        add('customs_unknown', 'customs_status/description')
    ambiguous = any(reason in reasons for reason in (
        'contradictory_customs_evidence', 'contradictory_sale_evidence',
        'ambiguous_customs_negation', 'customs_clearance_denied'))
    if ambiguous or invalid or not description_complete:
        status = 'needs_review'
    elif forbidden:
        status = 'excluded'
    elif reasons.intersection({'customs_risk_signal', 'vehicle_category_unconfirmed', 'possible_component_offer'}):
        status = 'needs_review'
    else:
        status = 'allowed'
        add('no_forbidden_offer_evidence', 'title/category/description')
    customs_state = ('conflicting' if 'customs' in positive and 'customs' in forbidden
                     else 'uncleared_declared' if 'customs' in forbidden
                     else 'cleared_declared' if 'customs' in positive else 'unknown')
    return {'status': status, 'reasons': sorted(reasons), 'evidence': evidence,
            'used_fields': [key for key in FACT_FIELDS if raw.get(key) is not None],
            'fingerprint': fingerprint(raw), 'customs_status': customs_state,
            'customs_independently_verified': False, 'description_complete': description_complete,
            'version': VERSION}
