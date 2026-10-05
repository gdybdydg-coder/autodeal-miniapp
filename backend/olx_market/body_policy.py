"""Narrow, documented comparison alias; source/filter body is never rewritten.

Skoda's own generation-II history calls the same non-estate body hatchback and
liftback. This is a manufacturer-level vocabulary mapping, not physical-car
verification. No sedan, other generation, year-based guess or wagon merging.
"""
SOURCES=(
 'https://www.skoda-storyboard.com/en/models/octavia/first-sketches-of-the-innovated-octavia-take-a-look/',
 'https://www.skoda-storyboard.com/en/press-kits/skoda-octavia-press-kit/history-60-years-of-the-skoda-octavia/',
 'https://cdn.skoda-storyboard.com/2023/03/230301_The-perfect-everyday-companion-25-years-of-the-Skoda-Octavia-Combi_06db1a55.pdf')

def body_policy(car):
    is_a5=(str(car.get('brand')).casefold()=='skoda' and str(car.get('model')).casefold()=='octavia' and car.get('generation')=='A5')
    body=car.get('body')
    return {'id':'skoda-octavia-a5-body-v1' if is_a5 else 'source-body-only',
            'source_body':body,'comparison_body':'liftback' if is_a5 and body=='hatchback' else body,
            'sources':list(SOURCES) if is_a5 else [],'reviewed_date':'2026-10-05' if is_a5 else None,
            'physical_body_verified':False}

def comparable_value(car,field):
    return body_policy(car)['comparison_body'] if field=='body' else car.get(field)
