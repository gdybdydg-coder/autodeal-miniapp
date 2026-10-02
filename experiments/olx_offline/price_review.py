"""Conservative review of supplied price evidence, not an OLX HTML parser.
Text can contradict a 'full' flag, but can NEVER establish a full price alone.
No automatic fraud classification or minimum market-price threshold.
"""
import re

SIGNALS = {
    'deposit': r'\b(?:аванс|завдаток|задаток|перший\s+внесок|первый\s+взнос)\b',
    'monthly': r'(?:/\s*(?:міс|мес|month)\b|\b(?:щомісячн\w*|ежемесячн\w*)\s+плат\w*)',
    'conditional': r'\b(?:ціна|цена)\s+(?:умовна|условная|договірна|договорная)\b',
    'part': r'\b(?:ціна|цена)\s+(?:за|на)\s+(?:деталь|двигун|двигатель|двері|дверь|капот)\b',
}


def review(raw):
    reasons=[]
    context=raw.get('price_context')
    if context is not None and not isinstance(context,str):
        reasons.append('invalid_price_context')
    elif context:
        if len(context)>4000:
            reasons.append('price_context_too_long')
        for label,pattern in SIGNALS.items():
            if re.search(pattern,context[:4000],re.IGNORECASE):
                reasons.append('price_context_'+label)
    if raw.get('price_kind')!='full':
        reasons.append('full_price_unconfirmed')
    return {'status':'needs_review' if reasons else 'structured_full_price',
            'reasons':sorted(set(reasons)), 'version':'price-review-v1',
            'text_is_not_proof':True}
