"""Inspect source-declared filter parameters and links; never synthesize routes."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, parse_qsl
import re

FIELDS = {'page', 'countpage', 'lang_id', 'top', 'custom', 'abroad',
          'category_id', 'bodystyle', 'body_id', 'marka_id', 'model_id',
          's_yers', 'po_yers', 'type', 'gearbox', 'drive_type'}


class Links(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.queries, self.links = base, [], []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == 'script':
            for key in ('data-search-query', 'data-search_query'):
                if key in data:
                    try:
                        pairs = parse_qsl(data[key], strict_parsing=True, max_num_fields=80,
                                          keep_blank_values=True)
                        duplicate = len(dict(pairs)) != len(pairs)
                        self.queries.append({'fields':{k:v for k,v in pairs if k in FIELDS},
                                             'duplicate_fields':duplicate,
                                             'omitted_field_count':sum(k not in FIELDS for k,v in pairs)})
                    except ValueError:
                        self.queries.append({'invalid':True})
        if tag != 'a' or not data.get('href'):
            return
        url = urljoin(self.base, data['href'])
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.netloc != 'auto.ria.com' or parts.fragment
                or '%' in parts.path):
            return
        if parts.query and not (parts.path == '/uk/last/hour/' and
                                re.fullmatch(r'page=[1-9][0-9]{0,3}',parts.query)):
            return
        if parts.path.startswith('/uk/car/audi/a6/') or parts.path == '/uk/last/hour/':
            if url not in self.links and len(self.links) < 200:
                self.links.append(url)


def inspect_links(html, url):
    if not isinstance(html, str) or len(html.encode()) > 2500000:
        raise ValueError('html_oversize')
    parser = Links(url)
    parser.feed(html)
    return {'source_url':url, 'search_metadata':parser.queries,
            'observed_catalogue_links':parser.links,
            'automatic_fetch_authorized':False}
