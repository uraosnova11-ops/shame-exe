"""VK user API transport. Access token is stored by the caller with Windows DPAPI."""
import json
import re
import urllib.parse
import urllib.request

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

def send(token, peer, message):
    peer=int(peer)
    if peer>=0: raise VKError('Адресатом может быть только деловое сообщество VK.')
    response=call('messages.send',token,peer_id=peer,random_id=0,message=message)
    if not isinstance(response,int): raise VKError('VK не подтвердил отправку.')
    return str(response)

def conversations(token):
    response=call('messages.getConversations',token,filter='all',count=100)
    return response.get('items',[]) if isinstance(response,dict) else []
