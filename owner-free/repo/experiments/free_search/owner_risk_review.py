"""Explicit reviewed disclosures for the isolated queued sender.

This binds a review to evidence; it does not infer an accident-free history.
The caller must review visible history/condition and supply factual findings.
No source content is executed and findings are escaped for Telegram HTML.
"""
import hashlib
from html import escape
import math


def review_text(review,page,now):
    if not isinstance(review,dict):raise ValueError('condition_review_required')
    if (review.get('listing_id')!=page.listing_id or review.get('listing_url')!=page.url
            or review.get('detail_sha256')!=hashlib.sha256(page.html.encode()).hexdigest()):
        raise ValueError('condition_review_evidence_mismatch')
    observed=review.get('observed_at')
    if (type(observed) not in (int,float) or not math.isfinite(observed)
            or not 0<=now-observed<=300):raise ValueError('condition_review_stale')
    if review.get('visible_history_checked') is not True:
        raise ValueError('visible_history_review_required')
    findings=review.get('findings')
    if (not isinstance(findings,list) or len(findings)>5 or
            any(not isinstance(s,str) or not s.strip() or len(s)>240 for s in findings)):
        raise ValueError('invalid_condition_findings')
    # Even an empty reviewed list is not evidence of good condition.
    text='\n\nВідомості про стан (за доступними даними оголошення):'
    text+=''.join('\n• '+escape(s.strip()) for s in findings) if findings else '\nОкремих підтверджених відомостей про пошкодження не зафіксовано.'
    return text+'\nПовноту історії та якість ремонту не підтверджено. Потрібна перевірка авто.'
