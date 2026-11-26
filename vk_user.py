"""VK user API transport. Access token is stored by the caller with Windows DPAPI."""
import json
import re
import secrets
import urllib.parse
import urllib.request
from pathlib import Path

API = 'https://api.vk.com/method/'
VERSION = '5.199'

class VKError(ValueError): pass
class VKPaused(VKError): pass

def call(method, token, **parameters):
    if not token: raise VKError('Подключите VK в мастере настройки.')
    data=urllib.parse.urlencode({'access_token':token,'v':VERSION,**parameters}).encode()
    req=urllib.request.Request(API+method,data=data,headers={'User-Agent':'YuriCRM/1.0'})
    try:
        with urllib.request.urlopen(req,timeout=20) as response:
            result=json.load(response)
    except Exception as exc:
        raise VKError('Не удалось связаться с VK. Проверьте интернет и повторите действие.') from exc
    if 'error' in result:
        code=int(result['error'].get('error_code',0))
        if code in (6,9,29): raise VKPaused('VK ограничил частоту отправки. Кампания остановлена.')
        if code in (7,15,901,902,917): raise VKError('VK не разрешил доступ к сообщениям или этому сообществу. Проверьте права токена и настройки сообщений.')
        if code==5: raise VKError('VK отклонил токен. Подключите аккаунт заново.')
        raise VKError('VK вернул ошибку '+str(code)+'. Проверьте доступ к сообщениям сообщества.')
    return result.get('response')

def identity(token):
    people=call('users.get',token)
    if not isinstance(people,list) or not people: raise VKError('Не удалось определить аккаунт VK.')
    p=people[0]
    return {'id':int(p['id']),'name':(p.get('first_name','')+' '+p.get('last_name','')).strip()}

def community(token, address):
    m=re.fullmatch(r'(?:https?://)?(?:m\.)?vk\.com/(club\d+|public\d+|[A-Za-z0-9_.]{5,32})/?',address.strip(),re.I)
    if not m: raise VKError('Укажите ссылку на деловое сообщество vk.com/…')
    groups=call('groups.getById',token,group_ids=m[1])
    if isinstance(groups,dict): groups=groups.get('groups',[])
    if not groups or groups[0].get('is_closed',0): raise VKError('Сообщество не найдено или закрыто.')
    return -int(groups[0]['id']),groups[0].get('name','')

def upload_message_photo(token,peer,image_path):
    image=Path(image_path)
    if not image.is_file() or image.stat().st_size>10_000_000: raise VKError('Скриншот демо отсутствует или слишком велик.')
    server=call('photos.getMessagesUploadServer',token,peer_id=peer)
    address=server.get('upload_url','') if isinstance(server,dict) else ''
    target=urllib.parse.urlparse(address)
    if target.scheme!='https' or not (target.hostname or '').endswith(('.vk.com','.vk-cdn.net')):
        raise VKError('VK не предоставил безопасный адрес загрузки изображения.')
    boundary='----YuriCRM'+secrets.token_hex(12)
    payload=(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="demo.png"\r\nContent-Type: image/png\r\n\r\n').encode()+image.read_bytes()+(f'\r\n--{boundary}--\r\n').encode()
    request=urllib.request.Request(address,data=payload,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
    try:
        with urllib.request.urlopen(request,timeout=30) as response: uploaded=json.load(response)
    except Exception as exc: raise VKError('VK не принял изображение демо. Проверьте сеть и повторите позже.') from exc
    saved=call('photos.saveMessagesPhoto',token,photo=uploaded['photo'],server=uploaded['server'],hash=uploaded['hash'])
    if not saved: raise VKError('VK не сохранил изображение демо.')
    photo=saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"+(f"_{photo['access_key']}" if photo.get('access_key') else '')

def send(token, peer, message, image_path=None):
    peer=int(peer)
    if peer>=0: raise VKError('Адресатом может быть только деловое сообщество VK.')
    attachment=upload_message_photo(token,peer,image_path) if image_path else ''
    response=call('messages.send',token,peer_id=peer,random_id=0,message=message,**({'attachment':attachment} if attachment else {}))
    if not isinstance(response,int): raise VKError('VK не подтвердил отправку.')
    return str(response)

def conversations(token):
    response=call('messages.getConversations',token,filter='all',count=100)
    return response.get('items',[]) if isinstance(response,dict) else []
