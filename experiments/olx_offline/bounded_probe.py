"""Explicit opt-in public research probe. No daemon, retries, cookies or redirects.

Supply only URLs actually observed in documentation/pages. Raw bodies stay in an
explicit PRIVATE scratch directory; publish the metadata, never the raw HTML.
Regular collection permission and real feed completeness are NOT established.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from urllib.parse import parse_qs, urlsplit, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError
from .fx import nbu_url, instant, KYIV
from .run_guard import RunGuard


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_url(url, now):
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.username or parts.password or parts.port or parts.fragment:
        raise ValueError('Plain HTTPS public URL required')
    if parts.hostname=='bank.gov.ua':
        if url!=nbu_url(instant(now).astimezone(KYIV).date()):
            raise ValueError('Only documented current-day USD quote is allowed')
        return 256*1024
    if parts.hostname not in ('www.olx.ua','olx.ua'):
        raise ValueError('Research hostname not allowed')
    detail=parts.path.startswith(('/d/uk/obyavlenie/','/d/obyavlenie/')) and parts.path.endswith('.html')
    search=parts.path in ('/uk/transport/legkovye-avtomobili/','/transport/legkovye-avtomobili/')
    query=parse_qs(parts.query,keep_blank_values=True)
    if not (detail or search) or (detail and parts.query):
        raise ValueError('Only observed passenger-car pages or clean ad URLs allowed')
    if search:
        # Exact public UI contract observed 2026-10-03, not a guessed API.
        # currency is display currency only; it never proves asking currency/FX.
        if set(query)-{'page','search[order]','currency'} or any(len(v)!=1 for v in query.values()):
            raise ValueError('Unverified query parameters are forbidden')
        if 'page' in query and (not query['page'][0].isdigit() or int(query['page'][0])<1):
            raise ValueError('Positive page required')
        if 'search[order]' in query and query['search[order]']!=['created_at:desc']:
            raise ValueError('Only observed newest sort is allowed')
        if 'currency' in query and query['currency']!=['UAH']:
            raise ValueError('Only observed display-currency parameter is allowed')
    return 2*1024*1024


def fetch_once(url, guard, *, open_url=None, max_html_bytes=2*1024*1024):
    # A larger foreground sample is explicit and still charged against the
    # total guard budget. Never enlarge NBU limits or retry a truncated response.
    if type(max_html_bytes) is not int or not 1<=max_html_bytes<=4*1024*1024:
        raise ValueError('Explicit HTML cap must be positive and at most 4 MiB')
    now=time.time();cap=validate_url(url,now)
    if urlsplit(url).hostname in ('olx.ua','www.olx.ua'):cap=max_html_bytes
    reservation=guard.reserve_request(urlsplit(url).hostname,cap)
    started=datetime.now(timezone.utc).isoformat();start=time.monotonic()
    report={'url':url,'started_utc':started,'attempts':1,'body_cap':cap,'redirects_followed':0,'retries':0}
    body=b''
    def timed_out(*_):raise TimeoutError('Overall HTTP deadline exceeded')
    previous=signal.signal(signal.SIGALRM,timed_out)
    timeout=min(20,guard.remaining_seconds())
    signal.setitimer(signal.ITIMER_REAL,timeout)
    try:
        opener=open_url or build_opener(NoRedirect).open
        request=Request(url,headers={'User-Agent':'AutoDeal-Research/0.2 (bounded public feasibility check)'})
        with opener(request,timeout=timeout) as response:
            body=response.read(cap)
            report.update(status=response.status,bytes_read=len(body),at_byte_cap=len(body)==cap,
                          content_type=response.headers.get_content_type(),sha256=hashlib.sha256(body).hexdigest())
    except HTTPError as exc:
        report.update(status=exc.code,bytes_read=0,http_error=True)
        location=exc.headers.get('Location')
        if location:
            location=urljoin(url,location)
            try:validate_url(location,now);report['observed_redirect']=location
            except ValueError:report['redirect_not_in_public_allowlist']=True
        exc.close()
    except (URLError,TimeoutError,OSError) as exc:
        report.update(status=None,bytes_read=0,error_type=type(exc).__name__)
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
        guard.record_response(reservation,len(body))
    report.update(seconds=round(time.monotonic()-start,3),finished_utc=datetime.now(timezone.utc).isoformat())
    return report,body


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--url',action='append',required=True,help='Actually observed public URL; no invented endpoints')
    ap.add_argument('--private-work-root',required=True)
    ap.add_argument('--allow-network',action='store_true')
    ap.add_argument('--max-html-bytes',type=int,default=2*1024*1024,help='Explicit sample cap, at most 4 MiB; charged to the existing 8 MiB run limit')
    ap.add_argument('--deadline',help='Explicit aware ISO deadline for a newly authorized foreground run; default keeps the expired night cutoff')
    args=ap.parse_args()
    if not args.allow_network:ap.error('Network is disabled without explicit --allow-network')
    if not 1<=args.max_html_bytes<=4*1024*1024:ap.error('HTML cap must be positive and at most 4 MiB')
    root=Path(args.private_work_root).resolve()
    if root==Path.cwd() or Path.cwd() in root.parents:
        ap.error('Private raw-body directory must be outside the checkout')
    reports=[]
    guard_options={}
    if args.deadline:
        deadline=datetime.fromisoformat(args.deadline)
        if deadline.tzinfo is None:ap.error('Deadline must include timezone')
        if not 0 < deadline.timestamp()-time.time() <= 1800:ap.error('Foreground deadline must be within the next 30 minutes')
        guard_options['hard_stop']=deadline.timestamp()
    with RunGuard(root,persist_state=True,**guard_options) as guard:
        # Validate the complete manifest before issuing its first request.
        for url in args.url:validate_url(url,time.time())
        if len(args.url)>5:ap.error('At most four OLX URLs plus one NBU URL')
        for i,url in enumerate(args.url):
            if i:
                if guard.remaining_seconds()<6:break
                time.sleep(5);guard.checkpoint()
            metadata,body=fetch_once(url,guard,max_html_bytes=args.max_html_bytes);reports.append(metadata)
            if body:
                path=root/('response-'+str(i)+'.bin')
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as f:f.write(body)
            (root/'probe-metadata.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n')
            if metadata.get('status') in (401,403,429):break
    print(json.dumps(reports,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
