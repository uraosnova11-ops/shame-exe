"""User-account MTProto integration. Telethon is loaded only when installed.

Credentials and StringSession are sealed with Windows DPAPI for the current OS user.
The event loop owns one TelegramClient; caller threads submit work to that loop.
"""
import asyncio
import base64
import ctypes
import json
import os
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from ctypes import wintypes
from pathlib import Path

class TelegramUnavailable(RuntimeError): pass
class TelegramPaused(RuntimeError): pass

class DATA_BLOB(ctypes.Structure):
    _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_byte))]

if os.name=='nt':
    _crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    _kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    for _name in ('CryptProtectData','CryptUnprotectData'):
        _fn=getattr(_crypt,_name);_fn.restype=wintypes.BOOL
        _fn.argtypes=[ctypes.POINTER(DATA_BLOB),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(DATA_BLOB)]
    _kernel.LocalFree.argtypes=[ctypes.c_void_p];_kernel.LocalFree.restype=ctypes.c_void_p

def _blob(data):
    buf=ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte))),buf

def protect(data):
    if os.name!='nt': raise TelegramUnavailable('����񭭮� �࠭���� ����㯭� ⮫쪮 � Windows')
    inp,keep=_blob(data);out=DATA_BLOB()
    if not _crypt.CryptProtectData(ctypes.byref(inp),None,None,None,None,0,ctypes.byref(out)):
        raise TelegramUnavailable('Windows �� ��������� ��࠭��� ������� ����. ������� �ਫ������ ��� ���筮� ���⭮� ������� Windows.')
    try: return ctypes.string_at(out.pbData,out.cbData)
    finally: _kernel.LocalFree(out.pbData)

def unprotect(data):
    if os.name!='nt': raise TelegramUnavailable('����񭭮� �࠭���� ����㯭� ⮫쪮 � Windows')
    inp,keep=_blob(data);out=DATA_BLOB()
    if not _crypt.CryptUnprotectData(ctypes.byref(inp),None,None,None,None,0,ctypes.byref(out)):
        raise TelegramUnavailable('Windows �� ᬮ��� ����஢��� ���� ��� ⥪�饣� ���짮��⥫�. �������� ������ ������.')
    try: return ctypes.string_at(out.pbData,out.cbData)
    finally: _kernel.LocalFree(out.pbData)

def friendly_error(e):
    name=type(e).__name__
    if name in ('FloodWaitError','FloodPremiumWaitError'):
        return 'Telegram ���ॡ���� ����. ��।� ��⠭������; �������� ��। ������ ����祭���.'
    if name in ('PeerFloodError','UserIsBlockedError','UserPrivacyRestrictedError','ChatWriteForbiddenError'):
        return 'Telegram ����⨫ ���饭�� �⮬� ������ ��� ��࠭�稫 ������. ��।� ��⠭������.'
    if name in ('PhoneCodeInvalidError','PhoneCodeExpiredError'):
        return '������ ��� ����祭�� ���. ������ ���� ��� � ������ �室.'
    if name=='PasswordHashInvalidError': return '������ ��஫� �����⠯��� �஢�ન.'
    if name=='ApiIdInvalidError': return '�஢���� API ID � API hash �� my.telegram.org.'
    return '�� 㤠���� �믮����� ������ Telegram. �஢���� ������祭��, ��ࠬ���� � ������ ������.'

class UserTelegram:
    def __init__(self, path, on_message=None):
        self.path=Path(path);self.on_message=on_message;self.loop=None;self.client=None
        self.thread=None;self.pending=None;self.profile=None;self.last_error=''
        self._ready=threading.Event()

    def _read(self):
        if not self.path.exists(): return None
        try: return json.loads(unprotect(self.path.read_bytes()))
        except Exception: raise TelegramUnavailable('�� 㤠���� ������ ��࠭��� ���� Windows. �������� ������ ������.')

    def _write(self, obj):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix('.tmp');tmp.write_bytes(protect(json.dumps(obj).encode()))
        os.replace(tmp,self.path)

    def start(self):
        if self.thread: return
        self.thread=threading.Thread(target=self._main,daemon=True,name='TelegramUser')
        self.thread.start();self._ready.wait(5)

    def _main(self):
        self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop);self._ready.set()
        try: self.loop.run_until_complete(self._restore())
        except Exception as e: self.last_error=friendly_error(e)
        self.loop.run_forever()

    def call(self, coro, timeout=45):
        self.start()
        try: return asyncio.run_coroutine_threadsafe(coro,self.loop).result(timeout)
        except FutureTimeout: raise TelegramUnavailable('Telegram �� �⢥⨫ ���६�; �஢���� ��� � ����� � �ਫ������.')

    def status(self):
        return {'connected':bool(self.profile),'profile':self.profile,'error':self.last_error,'available':self._available()}

    def _available(self):
        try: import telethon;return True
        except ImportError: return False

    async def _connect(self, saved):
        from telethon import TelegramClient,events
        from telethon.sessions import StringSession
        if self.client: await self.client.disconnect()
        self.client=TelegramClient(StringSession(saved.get('session','')),int(saved['api_id']),saved['api_hash'],
          auto_reconnect=True,request_retries=2,connection_retries=2,flood_sleep_threshold=0,
          device_model='Yuri CRM',app_version='0.2')
        await self.client.connect()
        if self.on_message:
            @self.client.on(events.NewMessage(incoming=True))
            async def receive(ev):
                if ev.is_private:
                    try: self.on_message(str(ev.sender_id),ev.raw_text,str(ev.id))
                    except Exception: pass

    async def _restore(self):
        saved=self._read()
        if not saved: return
        await self._connect(saved)
        if await self.client.is_user_authorized(): await self._set_profile(saved)
        else: self.last_error='����� Telegram ������⢨⥫쭠. �������� ������ ������.'

    async def _set_profile(self,saved):
        me=await self.client.get_me()
        if not me or me.bot: raise TelegramUnavailable('�������� ���짮��⥫�᪨� ������, � �� ���.')
        self.profile={'id':me.id,'name':' '.join(filter(None,[me.first_name,me.last_name])),
          'username':me.username or '', 'phone':saved.get('phone','')}
        saved['session']=self.client.session.save();self._write(saved);self.last_error=''

    async def _begin(self,api_id,api_hash,phone):
        if not re.fullmatch(r'\+\d{8,15}',phone): raise TelegramUnavailable('������ ����� � ����㭠த��� �ଠ�, ���ਬ�� +79001234567.')
        if not str(api_id).isdigit() or not re.fullmatch('[a-fA-F0-9]{32}',api_hash):
            raise TelegramUnavailable('������ API ID � API hash �� my.telegram.org.')
        saved={'api_id':int(api_id),'api_hash':api_hash,'phone':phone,'session':''}
        try:
            await self._connect(saved)
            sent=await self.client.send_code_request(phone)
            saved['session']=self.client.session.save();self._write(saved)
            self.pending={'phone_hash':sent.phone_code_hash,'phone':phone}
            return {'step':'code','hint':'������ ���, ����祭�� � Telegram ��� ��㣨� ᯮᮡ��, 㪠����� Telegram.'}
        except Exception as e:
            if isinstance(e,TelegramUnavailable): raise
            raise TelegramUnavailable(friendly_error(e))

    async def _code(self,code):
        if not self.pending: raise TelegramUnavailable('���砫� ������ ��� �室� � �ਫ������.')
        saved=self._read()
        try:
            from telethon.errors import SessionPasswordNeededError
            try: await self.client.sign_in(phone=saved['phone'],code=code,phone_code_hash=self.pending['phone_hash'])
            except SessionPasswordNeededError: return {'step':'password','hint':'������ ��஫� �����⠯��� �஢�ન.'}
            await self._set_profile(saved);self.pending=None
            return {'step':'connected','profile':self.profile}
        except Exception as e:
            if isinstance(e,TelegramUnavailable): raise
            raise TelegramUnavailable(friendly_error(e))

    async def _password(self,password):
        if not self.pending: raise TelegramUnavailable('���砫� ������ ��� �室� � �ਫ������.')
        saved=self._read()
        try:
            await self.client.sign_in(password=password)
            await self._set_profile(saved);self.pending=None
            return {'step':'connected','profile':self.profile}
        except Exception as e: raise TelegramUnavailable(friendly_error(e))

    async def _send(self,recipient,body,image_path=None):
        from telethon.tl.types import User
        if not self.profile or not self.client: raise TelegramUnavailable('���砫� �������� ���� Telegram-������.')
        recipient=recipient.strip()
        if not re.fullmatch(r'@[A-Za-z0-9_]{5,32}',recipient):
            raise TelegramUnavailable('������ ���⢥ত�� ������� @username, ��� ����� ⥫�䮭� ��� ��宦��� �����.')
        try:
            peer=await self.client.get_entity(recipient)
            if not isinstance(peer,User) or peer.bot: raise TelegramUnavailable('���⠪� ������ ���� ���짮��⥫�᪨� ������ �����⮬, � �� ��⮬ ��� �������.')
            msg=await self.client.send_file(peer,image_path,caption=body) if image_path else await self.client.send_message(peer,body)
            return str(msg.id),str(peer.id)
        except TelegramUnavailable: raise
        except Exception as e:
            self.last_error=friendly_error(e)
            if type(e).__name__ in ('FloodWaitError','FloodPremiumWaitError','PeerFloodError'):
                raise TelegramPaused(self.last_error)
            raise TelegramUnavailable(self.last_error)

    async def _disconnect(self):
        if self.client:
            try: await self.client.log_out()
            except Exception: await self.client.disconnect()
        self.client=None;self.profile=None;self.pending=None
        self.path.unlink(missing_ok=True)
        return {'ok':True}

    def begin(self,api_id,api_hash,phone): return self.call(self._begin(api_id,api_hash,phone))
    def code(self,code): return self.call(self._code(code))
    def password(self,password): return self.call(self._password(password))
    def send(self,recipient,body,image_path=None): return self.call(self._send(recipient,body,image_path))
    def disconnect(self): return self.call(self._disconnect())

