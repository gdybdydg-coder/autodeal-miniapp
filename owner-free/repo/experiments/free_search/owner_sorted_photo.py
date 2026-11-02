"""Photo notifications only for the isolated sorted-search owner transport."""
import re
from .owner_card import _Photo, safe_url
from .owner_card_transport import TelegramCard


def notification(raw, text):
    parser=_Photo();parser.feed(raw['html'])
    photos=set(parser.urls)
    return {'text':text,'photo':next(iter(photos)) if len(photos)==1 else None,'url':raw['url']}


class SortedTelegramPhoto(TelegramCard):
    def __call__(self, recipient, card):
        if recipient!=self._owner:raise ValueError('invalid_owner_recipient')
        text,photo,url=card['text'],card['photo'],card['url']
        if not isinstance(text,str) or not 1<=len(text)<=4096:raise ValueError('invalid_owner_text')
        if not re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_[1-9][0-9]{0,11}\.html',url):raise ValueError('invalid_listing_url')
        if photo is not None and safe_url(photo)!=photo:raise ValueError('invalid_owner_photo')
        payload={'reply_markup':{'inline_keyboard':[[{'text':'🔗 Відкрити оголошення','url':url}]]}}
        # Conservative caption bound, including entities, avoids truncating content.
        if photo and len(text.encode('utf-16-le'))//2<=1024:
            result=self.request('sendPhoto',{**payload,'photo':photo,'caption':text})
            # Only an explicit Telegram photo rejection permits a text fallback.
            # A timeout/ambiguous response never triggers a second send.
            if result.get('ok') is not False or result.get('error_code')!=400:
                return result
        return self.request('sendMessage',{**payload,'text':text,'link_preview_options':{'is_disabled':True}})
