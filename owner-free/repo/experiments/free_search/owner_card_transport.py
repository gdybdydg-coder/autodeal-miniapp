"""One-attempt owner card transport, including editing a known receipt in place.

No photo downloads, retries, fallback, credentials export or paid requests.
The sender's durable claim remains the authority for new sends.
"""
import json
import re
from .owner_card import safe_url
from .owner_telegram import TelegramText


def validate(card):
    if not isinstance(card,dict) or set(card)!={'text','photo','reply_markup'}:
        raise ValueError('invalid_owner_card')
    text=card['text']
    if (not isinstance(text,str) or not text.startswith('🧪 Тест\n')
            or len(text.encode('utf-16-le'))//2>1000):
        raise ValueError('invalid_owner_caption')
    photo=card['photo']
    if photo is not None and (not isinstance(photo,str) or safe_url(photo)!=photo):
        raise ValueError('invalid_owner_photo')
    try:
        buttons=card['reply_markup']['inline_keyboard']
        url=buttons[0][0]['url']
        if (len(buttons)!=1 or len(buttons[0])!=1 or
                buttons[0][0]['text']!='🔗 Відкрити оголошення' or
                not re.fullmatch(r'https://auto\.ria\.com/uk/auto_[A-Za-z0-9_-]+_[1-9][0-9]{0,11}\.html',url)):
            raise ValueError()
    except (KeyError,TypeError,IndexError,ValueError):
        raise ValueError('invalid_owner_button') from None
    return card


class TelegramCard(TelegramText):
    def request(self,method,payload,*,expected_message_id=None):
        if method not in ('sendPhoto','sendMessage','editMessageMedia','editMessageText'):
            raise ValueError('invalid_card_method')
        import httpx
        try:
            with httpx.Client(timeout=15,trust_env=False,follow_redirects=False) as client:
                with client.stream('POST','https://api.telegram.org/bot'+self._token+'/'+method,
                                   json={**payload,'chat_id':self._owner}) as response:
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body)>65536:return {'uncertain':True}
                    data=json.loads(body)
                    message=data.get('result') if isinstance(data,dict) else None
                    if (response.status_code==200 and data.get('ok') is True and isinstance(message,dict)
                            and (message.get('chat') or {}).get('id')==self._owner
                            and type(message.get('message_id')) is int
                            and (expected_message_id is None or message['message_id']==expected_message_id)):
                        return data
                    if isinstance(data,dict) and data.get('ok') is False and response.status_code<500:
                        return {'ok':False,'error_code':data.get('error_code')}
        except Exception:pass
        return {'uncertain':True}

    def __call__(self,recipient,card):
        if recipient!=self._owner:raise ValueError('invalid_owner_recipient')
        validate(card)
        payload={'reply_markup':card['reply_markup']}
        if card['photo']:
            return self.request('sendPhoto',{**payload,'photo':card['photo'],'caption':card['text']})
        return self.request('sendMessage',{**payload,'text':card['text'],
                                          'link_preview_options':{'is_disabled':True}})

    def edit(self,message_id,card):
        if type(message_id) is not int or message_id<=0:raise ValueError('invalid_message_id')
        validate(card)
        payload={'message_id':message_id,'reply_markup':card['reply_markup']}
        if card['photo']:
            return self.request('editMessageMedia',{**payload,'media':{
                'type':'photo','media':card['photo'],'caption':card['text']}},expected_message_id=message_id)
        return self.request('editMessageText',{**payload,'text':card['text'],
            'link_preview_options':{'is_disabled':True}},expected_message_id=message_id)
