"""Bounded public AUTO.RIA image downloads for Telegram multipart uploads."""
from collections import OrderedDict
from io import BytesIO
import re
import logging
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import warnings

import httpx
from PIL import Image, ImageOps

MAX_INPUT = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
CACHE_BYTES = 8 * 1024 * 1024
log = logging.getLogger(__name__)


def safe_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        return None
    try:
        url = urlsplit('https:' + value if value.startswith('//') else value)
        if (url.scheme not in {'http', 'https'} or url.username or url.password
                or url.port not in {None, 443} or url.query or url.fragment
                or not re.fullmatch(r'cdn\d*\.riastatic\.com', url.hostname or '')
                or not re.fullmatch(r'/photosnew/auto/photo/[a-zA-Z0-9_-]+\.(?:jpe?g|png|webp)', url.path)):
            return None
        return urlunsplit(('https', url.hostname, url.path, '', ''))
    except ValueError:
        return None


def from_details(data):
    photo = data.get('photoData')
    if not isinstance(photo, dict):
        return None
    return next((url for key in ('seoLinkM', 'seoLinkF', 'seoLinkSX')
                 if (url := safe_url(photo.get(key)))), None)


def jpeg(data):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as original:
            width, height = original.size
            if (width * height > MAX_PIXELS or min(width, height) < 1
                    or max(width, height) / min(width, height) > 20):
                raise ValueError('invalid_image_dimensions')
            original.load()
            image = ImageOps.exif_transpose(original).convert('RGB')
            image.thumbnail((1600, 1600))
            output = BytesIO()
            image.save(output, format='JPEG', quality=88)
            value = output.getvalue()
            if len(value) > MAX_INPUT:
                raise ValueError('image_too_large')
            return value


class PhotoLoader:
    def __init__(self):
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        # Same listing can fan out to several delivery workers concurrently.
        self.flights = {}

    def load(self, value):
        url = safe_url(value)
        if not url:
            log.warning('Telegram photo download unavailable reason=unapproved_url')
            return None
        with self.lock:
            cached = self.cache.get(url)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(url)
                return cached[1]
            if url in self.flights:
                event, leader = self.flights[url], False
            else:
                event, leader = threading.Event(), True
                self.flights[url] = event
        if not leader:
            event.wait(10)
            with self.lock:
                cached = self.cache.get(url)
                return cached[1] if cached and cached[0] > time.monotonic() else None
        try:
            value = self.download(url)
            if value:
                with self.lock:
                    self.cache[url] = (time.monotonic() + 300, value)
                    self.cache.move_to_end(url)
                    while len(self.cache) > 16 or sum(len(v[1]) for v in self.cache.values()) > CACHE_BYTES:
                        self.cache.popitem(last=False)
            return value
        finally:
            with self.lock:
                self.flights.pop(url, None)
                event.set()

    @staticmethod
    def download(url):
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(8, connect=4), follow_redirects=False) as client:
                for _ in range(3):
                    with client.stream('GET', url, headers={'Accept': 'image/jpeg,image/png,image/webp'}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = safe_url(response.headers.get('location'))
                            if not url or time.monotonic() - started > 8:
                                return None
                            continue
                        if response.status_code != 200:
                            log.warning('Telegram photo download unavailable reason=http_status status=%s', response.status_code)
                            return None
                        length = response.headers.get('content-length', '')
                        if length.isdecimal() and int(length) > MAX_INPUT:
                            return None
                        data = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            data.extend(chunk)
                            if len(data) > MAX_INPUT or time.monotonic() - started > 8:
                                return None
                        return jpeg(data)
        except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            log.warning('Telegram photo download unavailable reason=%s', type(exc).__name__)
            return None
        return None
