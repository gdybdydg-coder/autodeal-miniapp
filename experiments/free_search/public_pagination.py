"""Offline public-page metadata checks; no requests or automatic wider crawl.

Page numbers do not prove contiguous coverage when size/scope changes. Source
metadata is an assertion, not proof of an atomic snapshot or publication time.
"""
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qsl, urlsplit


def route_page(url):
    try:
        u = urlsplit(url)
    except (TypeError, ValueError):
        return None
    if u.scheme != 'https' or u.netloc != 'auto.ria.com' or u.path != '/uk/last/hour/' or u.fragment:
        return None
    if not u.query:
        return 0
    m = re.fullmatch(r'page=([1-9][0-9]{0,3})', u.query)
    return int(m[1])-1 if m and int(m[1]) <= 1000 else None


@dataclass(frozen=True)
class PageMetadata:
    url: str
    page_index: int | None
    page_size: int | None
    scope_digest: str | None
    next_url: str | None
    terminal_declared: bool
    issues: tuple[str, ...]


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.queries = []
        self.next_links = []
        self.disabled_next = 0
        self.pagination_present = False

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        classes = set((data.get('class') or '').split())
        if data.get('id') == 'pagination':
            self.pagination_present = True
        if tag == 'script':
            for key in ('data-search-query', 'data-search_query'):
                if key in data:
                    self.queries.append(data[key])
        if tag == 'a' and {'page-link', 'js-next'} <= classes:
            if 'disabled' in classes or data.get('aria-disabled') == 'true':
                self.disabled_next += 1
            else:
                self.next_links.append(data.get('href'))


def inspect_page(html, url):
    if not isinstance(html, str) or len(html.encode('utf-8')) > 2_500_000:
        raise ValueError('html_oversize')
    requested = route_page(url)
    if requested is None:
        raise ValueError('unsupported_public_route')
    parser = _Metadata()
    parser.feed(html)
    issues, queries = set(), set()
    for raw in parser.queries:
        try:
            if not isinstance(raw, str) or len(raw) > 4096:
                raise ValueError()
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=80)
            if len(dict(pairs)) != len(pairs):
                raise ValueError()
            fields = dict(pairs)
            if not re.fullmatch(r'0|[1-9][0-9]{0,3}', fields.get('page','')):
                raise ValueError()
            if not re.fullmatch(r'[1-9][0-9]{0,2}', fields.get('countpage','')):
                raise ValueError()
            index, size = int(fields['page']), int(fields['countpage'])
            if index > 999 or size > 200:
                raise ValueError()
            scope = sorted((k,v) for k,v in pairs if k not in ('page','countpage'))
            digest = sha256(json.dumps(scope,ensure_ascii=True).encode()).hexdigest()
            queries.add((index,size,digest))
        except (ValueError, TypeError):
            issues.add('invalid_pagination_metadata')
    index = size = digest = None
    if not queries:
        issues.add('pagination_metadata_missing')
    elif len(queries) != 1:
        issues.add('conflicting_pagination_metadata')
    else:
        index,size,digest = next(iter(queries))
        if index != requested:
            issues.add('route_page_mismatch')
    next_url = None
    if not parser.pagination_present:
        issues.add('pagination_navigation_missing')
    if len(parser.next_links) > 1 or parser.disabled_next > 1 or (parser.next_links and parser.disabled_next):
        issues.add('conflicting_next_page')
    elif parser.next_links:
        next_url = parser.next_links[0]
        if route_page(next_url) != requested+1:
            issues.add('invalid_next_page')
            next_url = None
    elif not parser.disabled_next:
        issues.add('terminal_page_not_proven')
    terminal = bool(parser.pagination_present and parser.disabled_next == 1 and not parser.next_links and not issues)
    return PageMetadata(url,index,size,digest,next_url,terminal,tuple(sorted(issues)))


def assess_scan(pages):
    """Metadata continuity only. Never report source-wide coverage as proven."""
    reasons = set()
    if not pages:
        reasons.add('no_pages')
    elif pages[0].page_index != 0:
        reasons.add('scan_did_not_start_at_head')
    for page in pages:
        reasons.update(page.issues)
    for previous,current in zip(pages,pages[1:]):
        if previous.page_index is None or current.page_index != previous.page_index+1:
            reasons.add('page_sequence_gap')
        if previous.next_url is None or route_page(previous.next_url) != route_page(current.url):
            reasons.add('next_link_not_followed')
        if previous.page_size != current.page_size:
            reasons.add('page_size_changed')
        if previous.scope_digest != current.scope_digest:
            # Missing versus explicit defaults are not assumed equivalent.
            reasons.add('scope_metadata_changed')
    if pages and not pages[-1].terminal_declared:
        reasons.add('source_end_not_observed')
    return {'state':'incomplete' if reasons else 'declared_end_observed',
            'reasons':sorted(reasons), 'whole_market_recall':None,
            'atomic_snapshot_proven':False, 'automatic_request_authorized':False}


def nominal_windows(pages):
    """Explain offset-pagination risks, not actual source ranks or recall.

    Assumes offset = page_index * page_size. This formula is a diagnostic
    hypothesis until verified against the source. Scope changes and a moving
    feed make actual coverage unknowable from these intervals alone.
    """
    windows, overlaps, gaps = [], [], []
    for page in pages:
        if (page.issues or type(page.page_index) is not int or
                type(page.page_size) is not int or page.page_index < 0 or
                not 1 <= page.page_size <= 200):
            return {'state': 'unknown', 'reason': 'invalid_page_metadata',
                    'whole_market_recall': None}
        start = page.page_index * page.page_size
        windows.append({'url': page.url, 'start': start,
                        'end_exclusive': start + page.page_size})
    end = 0
    for window in sorted(windows, key=lambda item: item['start']):
        start, stop = window['start'], window['end_exclusive']
        if start > end:
            gaps.append([end, start])
        elif start < end:
            overlaps.append([start, min(end, stop)])
        end = max(end, stop)
    return {'state': 'diagnostic_only', 'assumption': 'offset=page_index*page_size',
            'windows': windows, 'nominal_overlaps': overlaps,
            'nominal_gaps_before_last_observed_end': gaps,
            'same_scope_metadata': len({p.scope_digest for p in pages}) == 1,
            'actual_rank_coverage_proven': False, 'whole_market_recall': None}
