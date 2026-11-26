"""Юрий / local prospecting CRM. Python standard library only."""
import csv
import html
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import shutil
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from telegram_user import UserTelegram, TelegramUnavailable, TelegramPaused, protect, unprotect

ROOT = Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
DATA = Path(os.environ.get('YURI_DATA_DIR', str(Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'YuriCRM')))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'app.sqlite3'
if not DB.exists() and (ROOT/'data'/'app.sqlite3').exists() and (ROOT/'data').resolve()!=DATA.resolve():
    shutil.copy2(ROOT/'data'/'app.sqlite3',DB)
    if (ROOT/'data'/'demos').exists(): shutil.copytree(ROOT/'data'/'demos',DATA/'demos',dirs_exist_ok=True)
HOST = os.environ.get('YURI_HOST', '127.0.0.1')
PORT = int(os.environ.get('YURI_PORT', '8787'))
PUBLIC_URL = os.environ.get('YURI_PUBLIC_URL', '').rstrip('/')
TZ = timezone(timedelta(hours=3))  # Moscow has used UTC+3 year-round since 2014.
STOP = threading.Event()
PAUSE_SEND = threading.Event()

DEFAULT = dict(name='Юрий', city='Ростов-на-Дону', niches='пекарня,салон красоты,барбершоп,кафе,цветы,автосервис', districts='', review_threshold=30, daily_limit=10, demo_daily=5, demo_retention_days=30, allowed_hours='10-18', services='Создание и обновление сайтов для малого бизнеса', portfolio='', prices='', promises='', max_analysis_per_day=30, daily_budget_rub=0, auto=False, telegram=True, max=False, yandex_storage_allowed=False, test_sent=False, wizard_step=1, public_url='')
TG = UserTelegram(DATA/'telegram.dpapi')
SECRETS_FILE = DATA/'keys.dpapi'

def secret_store():
    if not SECRETS_FILE.exists(): return {}
    try: return json.loads(unprotect(SECRETS_FILE.read_bytes()))
    except Exception: return {}
def save_secrets(x):
    tmp=SECRETS_FILE.with_suffix('.tmp');tmp.write_bytes(protect(json.dumps(x).encode()))
    os.replace(tmp,SECRETS_FILE)
def telegram_inbound(chat,text,remote):
    with db() as c: inbound(c,'telegram',chat,text,remote)
TG.on_message=telegram_inbound

def now(): return datetime.now(TZ).isoformat(timespec='seconds')
def today(): return datetime.now(TZ).date().isoformat()
class Connection(sqlite3.Connection):
    def __exit__(self, *exc):
        try: return super().__exit__(*exc)
        finally: self.close()

def db():
    c = sqlite3.connect(DB, timeout=30, isolation_level=None, check_same_thread=False, factory=Connection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA foreign_keys=ON')
    return c

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS companies(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, address TEXT DEFAULT '',
          source TEXT NOT NULL, source_id TEXT NOT NULL, map_url TEXT DEFAULT '', website TEXT DEFAULT '',
          contacts TEXT DEFAULT '', telegram_public TEXT DEFAULT '', reviews INTEGER, rating REAL,
          checked_at TEXT, status TEXT DEFAULT 'найдена', score INTEGER DEFAULT 0, reasons TEXT DEFAULT '[]',
          analysis TEXT DEFAULT '{}', demo_path TEXT DEFAULT '', demo_at TEXT DEFAULT '',
          channel TEXT DEFAULT '', chat_id TEXT DEFAULT '', chat_verified INTEGER DEFAULT 0,
          consent INTEGER DEFAULT 0, consent_at TEXT DEFAULT '', code TEXT NOT NULL,
          do_not_contact INTEGER DEFAULT 0, last_error TEXT DEFAULT '',
          UNIQUE(source,source_id));
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, company_id INTEGER,
          state TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0, error TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
          channel TEXT NOT NULL, direction TEXT NOT NULL, body TEXT NOT NULL, remote_id TEXT DEFAULT '',
          state TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(company_id,channel,direction,remote_id));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS counters(day TEXT NOT NULL, kind TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(day,kind));
        CREATE TABLE IF NOT EXISTS offsets(channel TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        c.execute('INSERT OR IGNORE INTO settings VALUES(1,?)',(json.dumps(DEFAULT,ensure_ascii=False),))
        cols=[r['name'] for r in c.execute('PRAGMA table_info(companies)')]
        if 'telegram_username' not in cols:
            c.execute("ALTER TABLE companies ADD COLUMN telegram_username TEXT DEFAULT ''")
            c.execute("ALTER TABLE companies ADD COLUMN contact_evidence TEXT DEFAULT ''")
            # Existing Bot API chat IDs must never be re-used as user-account recipients.
            c.execute("UPDATE companies SET channel='telegram_bot_legacy',chat_verified=0,consent=0 WHERE channel='telegram'")
        old=settings(c)
        merged={**DEFAULT,**old}
        c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(merged,ensure_ascii=False),))
        c.execute("UPDATE jobs SET state='queued',next_at=0 WHERE state='running'")

def settings(c): return json.loads(c.execute('SELECT body FROM settings WHERE id=1').fetchone()['body'])
def event(c, action, detail): c.execute('INSERT INTO events(at,action,detail) VALUES(?,?,?)',(now(),action,str(detail)[:500]))
def count(c, kind):
    r=c.execute('SELECT value FROM counters WHERE day=? AND kind=?',(today(),kind)).fetchone()
    return r['value'] if r else 0
def bump(c, kind): c.execute('INSERT INTO counters VALUES(?,?,1) ON CONFLICT(day,kind) DO UPDATE SET value=value+1',(today(),kind))
def queue(c, kind, cid):
    if not c.execute("SELECT 1 FROM jobs WHERE kind=? AND company_id=? AND state IN ('queued','running')",(kind,cid)).fetchone():
        c.execute('INSERT INTO jobs(kind,company_id,created_at) VALUES(?,?,?)',(kind,cid,now()))
def label(d): return re.sub(r'[^\w]+',' ',d.lower(),flags=re.U).strip()
def key_name(name, address): return label(name)+'|'+label(address)

def add_company(c, x):
    name=str(x.get('name','')).strip()[:160]; category=str(x.get('category','')).strip()[:100]
    if not name or not category: raise ValueError('Нужны название и категория')
    source=str(x.get('source','import'))[:30]; address=str(x.get('address',''))[:250]
    sid=str(x.get('source_id') or key_name(name,address))[:200]
    existing=c.execute('SELECT id FROM companies WHERE source=? AND source_id=?',(source,sid)).fetchone()
    if existing: return existing['id']
    duplicate=c.execute('SELECT id FROM companies WHERE lower(name)=lower(?) AND lower(address)=lower(?)',(name,address)).fetchone()
    if duplicate: return duplicate['id']
    vals=(name,category,address,source,sid,str(x.get('map_url',''))[:500],str(x.get('website',''))[:500],str(x.get('contacts',''))[:500],str(x.get('telegram_public',''))[:150],x.get('reviews'),x.get('rating'),now(),secrets.token_urlsafe(16))
    cur=c.execute('''INSERT INTO companies(name,category,address,source,source_id,map_url,website,contacts,telegram_public,reviews,rating,checked_at,code)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',vals)
    m=re.search(r'(?:https?://)?t\.me/([A-Za-z0-9_]{5,32})',str(x.get('telegram_public','')))
    if m: c.execute('UPDATE companies SET telegram_username=? WHERE id=?',('@'+m[1],cur.lastrowid))
    event(c,'company_added',f'{cur.lastrowid}: {name} ({source})')
    if settings(c)['auto']: queue(c,'analyze',cur.lastrowid)
    return cur.lastrowid

def http_json(url, headers=None, data=None, timeout=15):
    req=urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
      headers={'User-Agent':'YuriCRM/0.1',**({'Content-Type':'application/json'} if data is not None else {}),**(headers or {})})
    with urllib.request.urlopen(req,timeout=timeout) as r: return json.load(r)

def search_2gis(c, query):
    token=secret_store().get('DGIS_KEY') or os.environ.get('DGIS_KEY')
    if not token: raise ValueError('Укажите DGIS_KEY')
    added=0
    for page in range(1,6):
        params=urllib.parse.urlencode(dict(q=query+' Ростов-на-Дону',point='39.7015,47.2357',radius=20000,
          type='branch',fields='items.full_address_name,items.rubrics,items.contact_groups,items.reviews,items.org,items.brand',page_size=50,page=page,key=token))
        try: result=http_json('https://catalog.api.2gis.com/3.0/items?'+params)
        except urllib.error.HTTPError as e:
            if e.code not in (400,403): raise
            # Optional contact and review fields can require a separately enabled key permission.
            params=urllib.parse.urlencode(dict(q=query+' Ростов-на-Дону',point='39.7015,47.2357',radius=20000,type='branch',fields='items.full_address_name,items.rubrics,items.org',page_size=50,page=page,key=token))
            result=http_json('https://catalog.api.2gis.com/3.0/items?'+params)
        if result.get('meta',{}).get('code',200)!=200: raise ValueError(str(result.get('meta'))[:300])
        items=result.get('result',{}).get('items',[])
        for it in items:
            if (it.get('org') or {}).get('branch_count',1)>=5: continue
            address=it.get('full_address_name') or it.get('address_name') or ''
            if 'ростов' not in address.lower() and it.get('full_address_name'): continue
            districts=[d.strip().lower() for d in settings(c)['districts'].split(',') if d.strip()]
            if districts and not any(d in address.lower() for d in districts): continue
            contacts=[]; site=''; tg=''
            for group in it.get('contact_groups') or []:
                for contact in group.get('contacts') or []:
                    value=contact.get('value',''); kind=contact.get('type','')
                    if value: contacts.append(f'{kind}: {value}')
                    if kind in ('website','web','site') and not site: site=value
                    if 't.me/' in value: tg=value
            rubrics=it.get('rubrics') or []
            category=(rubrics[0].get('name') if rubrics else query) or query
            review=it.get('reviews') or {}
            before=c.execute('SELECT COUNT(*) n FROM companies').fetchone()['n']
            add_company(c,dict(name=it.get('name'),category=category,address=address,source='2gis',source_id=it.get('id'),map_url=f"https://2gis.ru/rostov/firm/{it.get('id')}",website=site,contacts='; '.join(contacts),telegram_public=tg,reviews=review.get('review_count'),rating=review.get('rating')))
            added+=c.execute('SELECT COUNT(*) n FROM companies').fetchone()['n']-before
        if len(items)<50: break
    event(c,'search_2gis',f'{query}: +{added}')
    return added

def search_yandex(c, query):
    s=settings(c)
    if not s['yandex_storage_allowed']: raise ValueError('Сохранение данных Яндекса доступно только при договорном разрешении; включите флаг после проверки лицензии')
    token=secret_store().get('YANDEX_MAPS_KEY') or os.environ.get('YANDEX_MAPS_KEY')
    if not token: raise ValueError('Укажите YANDEX_MAPS_KEY')
    params=urllib.parse.urlencode(dict(text=query+' Ростов-на-Дону',type='biz',lang='ru_RU',results=50,apikey=token))
    data=http_json('https://search-maps.yandex.ru/v1/?'+params)
    added=0
    for ft in data.get('features',[]):
        m=ft.get('properties',{}).get('CompanyMetaData',{})
        a=m.get('address','')
        if 'ростов' not in a.lower(): continue
        districts=[d.strip().lower() for d in s['districts'].split(',') if d.strip()]
        if districts and not any(d in a.lower() for d in districts): continue
        before=c.execute('SELECT COUNT(*) n FROM companies').fetchone()['n']
        add_company(c,dict(name=m.get('name'),category=(m.get('Categories') or [{}])[0].get('name') or query,address=a,source='yandex',source_id=m.get('id'),map_url=f"https://yandex.ru/maps/org/{m.get('id')}/",website=m.get('url',''),contacts='; '.join(f"{p.get('type','phone')}: {p.get('formatted','')}" for p in m.get('Phones',[]))))
        added+=c.execute('SELECT COUNT(*) n FROM companies').fetchone()['n']-before
    event(c,'search_yandex',f'{query}: +{added}')
    return added

def safe_url(url):
    p=urllib.parse.urlparse(url if '://' in url else 'https://'+url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password: raise ValueError('Недопустимый адрес сайта')
    try:
        ips={x[4][0] for x in socket.getaddrinfo(p.hostname,None)}
        if not ips or any(not ipaddress.ip_address(i).is_global for i in ips): raise ValueError('Адрес сайта ведёт во внутреннюю сеть')
    except socket.gaierror: raise ValueError('Домен сайта не найден')
    return p.geturl()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        safe_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def inspect_site(url):
    findings=[]; details={}
    if not url: return {'findings':[{'type':'fact','text':'Сайт в доступных данных не указан'}], 'details':{},'status':'no_site'}
    url=safe_url(url)
    start=time.monotonic()
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; YuriSiteCheck/1.0)'})
        with urllib.request.build_opener(NoRedirect).open(req,timeout=10) as r:
            ct=r.headers.get('Content-Type',''); code=r.status
            body=r.read(400000).decode('utf-8','replace') if 'html' in ct.lower() else ''
            details={'http_status':code,'response_seconds':round(time.monotonic()-start,2),'content_type':ct,'final_url':r.url}
        if not body: findings.append({'type':'technical','text':'Ответ сайта не содержит HTML'})
        if body and not re.search(r'<meta[^>]+name=["\']viewport',body,re.I): findings.append({'type':'technical','text':'Не найден meta viewport: адаптивность требует ручной проверки'})
        if body and not re.search(r'<title\b[^>]*>\s*\S',body,re.I): findings.append({'type':'technical','text':'Не найден заполненный заголовок страницы'})
        if body and not re.search(r'(tel:|mailto:|t\.me/|max\.ru/)',body,re.I): findings.append({'type':'observation','text':'На первой странице не найдены ссылки на телефон, почту или мессенджер'})
        if details['response_seconds']>4: findings.append({'type':'technical','text':f"Ответ сервера занял {details['response_seconds']} с (один замер, не оценка Core Web Vitals)"})
        return {'findings':findings,'details':details,'status':'checked'}
    except Exception as e:
        return {'findings':[{'type':'technical','text':'Не удалось открыть сайт: '+str(e)[:150]}], 'details':details,'status':'unavailable'}

def priority(row, analysis, s):
    reasons=[]; score=0
    if not row['website']: score+=35; reasons.append('Сайт не указан: возможна полезная главная страница')
    if row['reviews'] is not None and row['reviews']<=int(s['review_threshold']): score+=15; reasons.append(f"Отзывов {row['reviews']} (порог {s['review_threshold']}); это не указывает на возраст или выручку")
    if analysis['status']=='unavailable': score+=25; reasons.append('Сайт недоступен при проверке')
    for f in analysis['findings']:
        if f['type']=='technical' and analysis['status']=='checked': score+=7; reasons.append(f['text'])
    if row['source']=='2gis' and not row['contacts']: reasons.append('Контакты не получены: для них может требоваться отдельный доступ API')
    if re.search(r'(^|\W)(сеть|франшиза|филиал)(\W|$)',row['name'],re.I): score-=50; reasons.append('Возможно сеть: требуется проверка')
    return max(0,score),reasons

def render_demo(row, analysis):
    name=html.escape(row['name']); cat=row['category'].lower(); addr=html.escape(row['address'])
    if re.search('пекар|кондитер|кофе|кафе|ресторан',cat):
        kind='bakery'; eyebrow='Тёплая городская история'; title=f'Встречайте {name}'; subtitle='Место, которое хочется открыть для себя'; blocks=['Знакомство','Ассортимент','Как нас найти']; accent='#b75936'; dark='#33201a'; bg='#f9eee0'; shape='◒'
    elif re.search('салон|красот|барбер|парикмах|студия',cat):
        kind='beauty'; eyebrow='Пространство для себя'; title=name; subtitle='Стиль начинается с внимания к деталям'; blocks=['О студии','Направления','Контакты']; accent='#8c6b79'; dark='#27262b'; bg='#f3eff2'; shape='✦'
    elif re.search('авто|мастер|ремонт|сервис',cat):
        kind='service'; eyebrow='Локальная мастерская'; title=name; subtitle='Понятная информация об услугах и связи'; blocks=['О мастерской','Услуги','Адрес']; accent='#b0e164'; dark='#172524'; bg='#e7ede7'; shape='↗'
    else:
        kind='local'; eyebrow='Местный бизнес · Ростов-на-Дону'; title=name; subtitle='Удобная страница для знакомства с компанией'; blocks=['О компании','Направления','Контакты']; accent='#637a62'; dark='#1f302c'; bg='#e9f0e9'; shape='✳'
    card=''.join(f'<article><span>0{i}</span><h3>{html.escape(b)}</h3><p>Демонстрационный текст. Сведения и материалы добавляются после согласования.</p></article>' for i,b in enumerate(blocks,1))
    address=f'<p class="address">{addr}</p>' if addr else '<p class="address">Адрес уточняется</p>'
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><title>Концепция — {name}</title><style>
    :root{{--a:{accent};--d:{dark};--b:{bg}}}*{{box-sizing:border-box}}body{{margin:0;background:var(--b);color:var(--d);font-family:Arial,sans-serif}}.notice{{padding:12px 5%;background:var(--d);color:white;font:12px Arial;letter-spacing:.08em;text-align:center}}header{{display:flex;justify-content:space-between;align-items:center;padding:25px 6%;border-bottom:1px solid #7774}}.brand{{font-weight:800;font-size:20px}}nav{{display:flex;gap:25px;font-size:13px}}.hero{{min-height:560px;display:grid;grid-template-columns:1.1fr .9fr;align-items:center;padding:5% 6%;gap:5%}}.eyebrow{{font-size:13px;text-transform:uppercase;letter-spacing:.25em}}h1{{font-family:Georgia,serif;font-size:clamp(54px,8vw,118px);line-height:.94;letter-spacing:-.06em;margin:24px 0}}.subtitle{{font-size:21px;line-height:1.4;max-width:500px}}a.button{{display:inline-block;background:var(--a);color:{'#172524' if kind=='service' else 'white'};padding:17px 24px;text-decoration:none;border-radius:100px;margin-top:22px;font-weight:700}}.art{{height:430px;background:var(--a);border-radius:{'50% 50% 8px 8px' if kind=='beauty' else '44% 44% 6% 6%' if kind=='bakery' else '10px'};display:grid;place-items:center;color:var(--b);font-size:220px;box-shadow:24px 24px 0 #0001}}.caption{{font-size:12px;margin-top:32px;opacity:.7}}section{{padding:65px 6%}}section h2{{font:46px Georgia,serif;margin:0 0 32px}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:15px}}article{{border-top:1px solid var(--d);padding:20px 8px;min-height:190px}}article span{{font-size:12px;opacity:.6}}article h3{{font:27px Georgia,serif}}article p{{font-size:14px;line-height:1.5;max-width:260px}}footer{{padding:40px 6%;background:var(--d);color:var(--b)}}.address{{opacity:.8}}@media(max-width:700px){{nav{{display:none}}.hero{{grid-template-columns:1fr;min-height:unset;padding:65px 6%}}.art{{height:280px;font-size:130px;order:-1}}h1{{font-size:58px}}.grid{{grid-template-columns:1fr}}section h2{{font-size:36px}}}}
    </style></head><body><div class="notice">НЕЗАВИСИМАЯ КОНЦЕПЦИЯ РЕДИЗАЙНА · НЕ ОФИЦИАЛЬНЫЙ САЙТ</div><header><div class="brand">{name}</div><nav><span>О компании</span><span>Направления</span><span>Контакты</span></nav></header><main><div class="hero"><div><div class="eyebrow">{eyebrow}</div><h1>{title}</h1><p class="subtitle">{subtitle}</p><a class="button" href="#contacts">Посмотреть контакты</a><p class="caption">Демонстрационная графика · не фотографии компании</p></div><div class="art" aria-label="Демонстрационная абстрактная графика">{shape}</div></div><section><h2>Всё важное — на виду</h2><div class="grid">{card}</div></section></main><footer id="contacts"><strong>{name}</strong>{address}<small>Концепция для обсуждения · без форм заявок и сбора данных</small></footer></body></html>'''

def screenshot(page):
    browser=next((p for p in [shutil.which('chrome'),r'C:\Program Files\Google\Chrome\Application\chrome.exe',shutil.which('msedge'),r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'] if p and Path(p).exists()),None)
    if not browser: return False
    target=page.with_suffix('.png')
    try:
        with tempfile.TemporaryDirectory() as profile:
            subprocess.run([browser,'--headless=new','--disable-gpu','--no-sandbox','--no-first-run','--disable-extensions','--hide-scrollbars',f'--user-data-dir={profile}','--window-size=1440,900',f'--screenshot={target}',page.as_uri()],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=25,check=True)
        return target.exists()
    except (OSError,subprocess.SubprocessError): return False

def make_message(row, s):
    public=(s.get('public_url') or PUBLIC_URL).rstrip('/')
    if not row['demo_path']: raise ValueError('Сначала создайте демостраницу')
    image=DATA/'demos'/Path(row['demo_path']).with_suffix('.png').name
    if public and not public.startswith('https://'): raise ValueError('Публичный URL должен использовать HTTPS')
    if not public and not image.exists(): raise ValueError('Нет публичной HTTPS-ссылки и скриншота демо. Проверьте установленный Chrome или Edge.')
    niche=row['category'].lower()
    focus='ассортименте и удобном просмотре с телефона' if re.search('пекар|кафе|цвет',niche) else 'услугах и записи с телефона' if re.search('салон|барбер',niche) else 'понятном описании услуг и быстром переходе к контактам'
    demo=f"независимый пример страницы для «{row['name']}»: {public}{row['demo_path']}" if public else f"независимый пример страницы для «{row['name']}» — прикрепил скриншот"
    return f"Здравствуйте! Меня зовут {s['name']}, я занимаюсь созданием сайтов для бизнеса в Ростове-на-Дону. Подготовил {demo}. Сделал акцент на {focus}. Если вам интересно, можем обсудить сайт для вас. Если такие предложения не нужны, напишите «стоп»."

def send(channel, chat, body, image_path=None):
    if channel=='telegram':
        return TG.send(chat,body,image_path)
    if channel=='max':
        token=secret_store().get('MAX_BOT_TOKEN') or os.environ.get('MAX_BOT_TOKEN')
        if not token: raise ValueError('Не настроен MAX_BOT_TOKEN')
        x=http_json('https://platform-api2.max.ru/messages?'+urllib.parse.urlencode({'user_id':chat}),headers={'Authorization':token},data={'text':body})
        return str(x.get('message',{}).get('body',{}).get('mid') or x.get('message',{}).get('id') or x.get('mid') or '')
    raise ValueError('Канал не поддерживается')

def perform(c, job):
    cid=job['company_id']; row=c.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
    if not row: return
    s=settings(c)
    if job['kind']=='analyze':
        if count(c,'analysis')>=int(s['max_analysis_per_day']): raise ValueError('Дневной лимит анализа')
        a=inspect_site(row['website']); score,reasons=priority(row,a,s)
        c.execute('UPDATE companies SET analysis=?,score=?,reasons=?,status=?,checked_at=? WHERE id=?',(json.dumps(a,ensure_ascii=False),score,json.dumps(reasons,ensure_ascii=False),'проверена',now(),cid))
        bump(c,'analysis'); event(c,'analyzed',f'{cid}: {score}')
        if s['auto'] and score>=20 and not row['do_not_contact']: queue(c,'demo',cid)
    elif job['kind']=='demo':
        if count(c,'demo')>=int(s['demo_daily']): raise ValueError('Дневной лимит демо')
        page=render_demo(row,json.loads(row['analysis']))
        directory=DATA/'demos'; directory.mkdir(exist_ok=True)
        path=directory/f'{cid}-{row["code"]}.html'; path.write_text(page,encoding='utf-8')
        image_ok=screenshot(path)
        c.execute('UPDATE companies SET demo_path=?,demo_at=?,status=? WHERE id=?',(f'/demo/{path.name}',now(),'создан пример',cid))
        bump(c,'demo'); event(c,'demo_created',f'{cid}; screenshot={image_ok}')
        if s['auto'] and row['chat_verified'] and row['consent'] and not row['do_not_contact']:
            queue(c,'send',cid)
            c.execute("UPDATE companies SET status='в очереди' WHERE id=?",(cid,))
    elif job['kind']=='send':
        if PAUSE_SEND.is_set() or not s['auto'] or not s.get(row['channel'],False): raise ValueError('Кампания или канал выключены')
        recipient=row['telegram_username'] if row['channel']=='telegram' else row['chat_id']
        if row['do_not_contact'] or not (row['chat_verified'] and row['consent'] and recipient): raise ValueError('Нет подтверждённого делового контакта и разрешения')
        if c.execute("SELECT 1 FROM messages WHERE company_id=? AND direction='out' AND state IN ('sent','unknown')",(cid,)).fetchone(): return
        if count(c,'sent')>=int(s['daily_limit']): raise ValueError('Дневной лимит отправки')
        hour=datetime.now(TZ).hour; hrs=re.fullmatch(r'(\d{1,2})-(\d{1,2})',s['allowed_hours'])
        if not hrs or not int(hrs[1])<=hour<int(hrs[2]): raise ValueError('Вне разрешённых часов (Москва)')
        body=make_message(row,s)
        image_path=None
        if row['channel']=='telegram' and not (s.get('public_url') or PUBLIC_URL):
            image_path=str(DATA/'demos'/Path(row['demo_path']).with_suffix('.png').name)
        mid=c.execute("INSERT INTO messages(company_id,channel,direction,body,state,created_at) VALUES(?,?,'out',?,'unknown',?)",(cid,row['channel'],body,now())).lastrowid
        # An ambiguous timeout must never result in an automatic retry of an outward message.
        try:
            if PAUSE_SEND.is_set(): raise ValueError('Отправка остановлена')
            result=send(row['channel'],recipient,body,image_path)
            remote,peer_id=result if isinstance(result,tuple) else (result,None)
        except Exception as e:
            c.execute('UPDATE messages SET state=? WHERE id=?',('unknown',mid))
            if isinstance(e,TelegramPaused):
                PAUSE_SEND.set();s['auto']=False
                c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
                event(c,'auto_stopped','Ограничение Telegram; очередь остановлена')
            raise RuntimeError('Исход отправки требует ручной проверки: '+str(e)[:160])
        c.execute("UPDATE messages SET state='sent',remote_id=? WHERE id=?",(remote,mid))
        c.execute("UPDATE companies SET status='отправлено',chat_id=COALESCE(?,chat_id) WHERE id=?",(peer_id,cid))
        bump(c,'sent'); event(c,'sent',f'{cid}: {row["channel"]} {remote}')

def worker():
    while not STOP.wait(2):
        try:
            with db() as c:
                job=c.execute("SELECT * FROM jobs WHERE state='queued' AND next_at<=? ORDER BY id LIMIT 1",(time.time(),)).fetchone()
                if not job: continue
                c.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?",(job['id'],))
                try:
                    perform(c,job)
                    c.execute("UPDATE jobs SET state='done',error='' WHERE id=?",(job['id'],))
                except Exception as e:
                    err=str(e)[:250]; transient=('timed out','429','503','Дневной лимит','Вне разрешённых часов','Кампания или канал выключены')
                    retry=job['kind']!='send' and job['attempts']<3 and any(t in err for t in transient)
                    c.execute('UPDATE jobs SET state=?,next_at=?,error=? WHERE id=?',('queued' if retry else 'failed',time.time()+min(3600,60*2**job['attempts']) if retry else 0,err,job['id']))
                    c.execute('UPDATE companies SET last_error=? WHERE id=?',(err,job['company_id']))
                    event(c,'job_error',f"{job['kind']} {job['company_id']}: {err}")
                    if job['kind']=='send' and ('429' in err or '403' in err or 'ограничил аккаунт' in err or 'потребовал паузу' in err):
                        PAUSE_SEND.set()
                        s=settings(c); s['auto']=False
                        c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
                        event(c,'auto_stopped','Ограничение канала; очередь остановлена')
        except Exception as e: print('worker:',e,flush=True)

def inbound(c, channel, chat, text, remote_id):
    if not chat or not text: return
    row=c.execute('SELECT * FROM companies WHERE channel=? AND chat_id=?',(channel,str(chat))).fetchone()
    if not row:
        m=re.fullmatch(r'/start\s+([A-Za-z0-9_\-]+)',text.strip())
        if m:
            row=c.execute('SELECT * FROM companies WHERE code=?',(m[1],)).fetchone()
            if row:
                c.execute('UPDATE companies SET channel=?,chat_id=?,chat_verified=0 WHERE id=?',(channel,str(chat),row['id']))
                event(c,'chat_started',f'{row["id"]}: ожидание проверки принадлежности компании')
        return
    if remote_id and c.execute("SELECT 1 FROM messages WHERE channel=? AND direction='in' AND remote_id=?",(channel,str(remote_id))).fetchone(): return
    c.execute("INSERT INTO messages(company_id,channel,direction,body,remote_id,state,created_at) VALUES(?,?, 'in',?,?,'received',?)",(row['id'],channel,text,str(remote_id),now()))
    t=text.strip().lower()
    if t in ('стоп','stop','не пишите','отписаться','/stop'):
        c.execute("UPDATE companies SET do_not_contact=1,consent=0,status='отказ' WHERE id=?",(row['id'],))
        c.execute("UPDATE jobs SET state='cancelled' WHERE company_id=? AND kind='send' AND state='queued'",(row['id'],))
    elif t in ('/allow','согласен','можете прислать пример'):
        c.execute('UPDATE companies SET consent=1,consent_at=? WHERE id=?',(now(),row['id']))
        if settings(c)['auto'] and row['chat_verified'] and row['demo_path']: queue(c,'send',row['id'])
    elif row['status']=='отправлено':
        status='заинтересован' if re.search(r'интерес|обсуд|давайте|сколько стоит|цена',t) else 'получен ответ'
        c.execute('UPDATE companies SET status=? WHERE id=?',(status,row['id']))
    event(c,'inbound',f'{row["id"]}: {channel}')

def poll_max(c):
    token=secret_store().get('MAX_BOT_TOKEN') or os.environ.get('MAX_BOT_TOKEN')
    if not token: return
    marker=c.execute("SELECT value FROM offsets WHERE channel='max'").fetchone()
    params=urllib.parse.urlencode({'limit':50,**({'marker':marker['value']} if marker else {})})
    data=http_json('https://platform-api2.max.ru/updates?'+params,headers={'Authorization':token},timeout=12)
    for u in data.get('updates',[]):
        m=u.get('message',{}); b=m.get('body',{})
        if u.get('update_type')=='message_created': inbound(c,'max',m.get('sender',{}).get('user_id'),b.get('text',''),b.get('mid'))
        elif u.get('update_type')=='bot_started': inbound(c,'max',u.get('user',{}).get('user_id'),'/start '+u.get('payload',''),'start-'+str(u.get('timestamp')))
    if data.get('marker'): c.execute("INSERT INTO offsets VALUES('max',?) ON CONFLICT(channel) DO UPDATE SET value=excluded.value",(str(data['marker']),))

def polling():
    while not STOP.wait(12):
        with db() as c:
            s=settings(c)
            for channel,fn in [('max',poll_max)]:
                if s.get(channel):
                    try: fn(c)
                    except Exception as e: event(c,'poll_error',f'{channel}: {str(e)[:120]}')

def discovery():
    while not STOP.wait(60):
        try:
            with db() as c:
                s=settings(c)
                if s['auto']:
                    for niche in [v.strip() for v in s['niches'].split(',') if v.strip()][:20]:
                        for channel,key,fn in [('2gis','DGIS_KEY',search_2gis),('yandex','YANDEX_MAPS_KEY',search_yandex)]:
                            if not (secret_store().get(key) or os.environ.get(key)) or (channel=='yandex' and not s['yandex_storage_allowed']): continue
                            marker=f'search:{channel}:{niche}'
                            if c.execute('SELECT 1 FROM counters WHERE day=? AND kind=?',(today(),marker)).fetchone(): continue
                            try: fn(c,niche);bump(c,marker)
                            except Exception as e: event(c,'discovery_error',f'{channel}/{niche}: {str(e)[:120]}')
                # Removing generated pages also removes screenshots. No other data is deleted.
                days=max(1,int(s['demo_retention_days']))
                cutoff=time.time()-days*86400
                for p in (DATA/'demos').glob('*') if (DATA/'demos').exists() else []:
                    if p.stat().st_mtime<cutoff: p.unlink(missing_ok=True)
        except Exception as e: print('discovery:',e,flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def respond(self,data,status=200):
        body=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def read(self):
        n=int(self.headers.get('Content-Length',0))
        if n>2_000_000: raise ValueError('Слишком большой запрос')
        return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        path=urllib.parse.urlparse(self.path).path
        if path=='/':
            data=(ROOT/'index.html').read_bytes(); self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if path.startswith('/demo/') and re.fullmatch(r'/demo/[\w-]+\.html',path):
            file=DATA/'demos'/path.rsplit('/',1)[-1]
            if not file.exists(): self.send_error(404);return
            data=file.read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('X-Robots-Tag','noindex, nofollow, noarchive');self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'");self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if path.startswith('/demo/') and re.fullmatch(r'/demo/[\w-]+\.png',path):
            file=DATA/'demos'/path.rsplit('/',1)[-1]
            if not file.exists(): self.send_error(404);return
            data=file.read_bytes();self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('X-Robots-Tag','noindex, nofollow, noarchive');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        if path!='/api/state': self.send_error(404);return
        with db() as c:
            companies=[dict(r) for r in c.execute('SELECT * FROM companies ORDER BY score DESC,id DESC LIMIT 500')]
            for r in companies:
                for k in ('analysis','reasons'): r[k]=json.loads(r[k])
                r.pop('code',None)
            secrets_now=secret_store()
            self.respond(dict(settings=settings(c),companies=companies,jobs=[dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT 100')],messages=[dict(r) for r in c.execute('SELECT * FROM messages ORDER BY id DESC LIMIT 200')],events=[dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 50')],counters={x:count(c,x) for x in ('analysis','demo','sent')},public_url=settings(c).get('public_url') or PUBLIC_URL,telegram_user=TG.status(),integrations={x:bool(secrets_now.get(x) or os.environ.get(x)) for x in ('DGIS_KEY','YANDEX_MAPS_KEY','MAX_BOT_TOKEN')}))
    def do_POST(self):
        try:
            x=self.read(); path=urllib.parse.urlparse(self.path).path
            with db() as c:
                if path=='/api/telegram/begin':
                    out=TG.begin(x.get('api_id',''),x.get('api_hash',''),x.get('phone',''))
                    event(c,'telegram_login','Код запрошен')
                elif path=='/api/telegram/code':
                    out=TG.code(x.get('code',''))
                    event(c,'telegram_login',out['step'])
                elif path=='/api/telegram/password':
                    out=TG.password(x.get('password',''))
                    event(c,'telegram_login',out['step'])
                elif path=='/api/telegram/disconnect':
                    PAUSE_SEND.set()
                    out=TG.disconnect()
                    s=settings(c);s['auto']=False;s['test_sent']=False
                    c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
                    event(c,'telegram_disconnect','Сессия удалена')
                elif path=='/api/telegram/test':
                    if not TG.status()['connected']: raise ValueError('Сначала подключите личный Telegram-аккаунт')
                    recipient=str(x.get('recipient','')).strip()
                    if not x.get('confirmed'): raise ValueError('Подтвердите, что тестовый получатель согласен получить сообщение')
                    if not re.fullmatch(r'@[A-Za-z0-9_]{5,32}',recipient): raise ValueError('Введите @username тестового пользователя')
                    mid,peer=TG.send(recipient,'Тест приложения Юрия. Это проверка отправки с моего пользовательского аккаунта Telegram. Ответьте «получено», если сообщение пришло.')
                    s=settings(c);s['test_sent']=True
                    c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),))
                    event(c,'test_send',f'Отправлено Telegram сообщение #{mid} тестовому ID {peer}')
                    out={'accepted':True,'message_id':mid,'recipient_id':peer,'note':'Telegram принял сообщение; подтвердите получение на втором аккаунте.'}
                elif path=='/api/integrations':
                    secrets_now=secret_store()
                    for k in ('DGIS_KEY','YANDEX_MAPS_KEY','MAX_BOT_TOKEN'):
                        if k in x:
                            if x[k]: secrets_now[k]=str(x[k]).strip()
                            else: secrets_now.pop(k,None)
                    save_secrets(secrets_now);event(c,'integrations','Обновлены ключи внешних сервисов');out={'ok':True}
                elif path=='/api/settings':
                    s=settings(c)
                    for k,v in x.items():
                        if k in s and k not in ('test_sent',): s[k]=v
                    if s['auto'] and (not TG.status()['connected'] or not s['test_sent']):
                        raise ValueError('Перед включением кампании подключите личный Telegram и отправьте тестовое сообщение')
                    if not s['auto']: PAUSE_SEND.set()
                    else: PAUSE_SEND.clear()
                    c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s,ensure_ascii=False),));event(c,'settings','Изменены настройки');out={'ok':True}
                    if s['auto']:
                        for r in c.execute('SELECT id,score,demo_path,chat_verified,consent,do_not_contact FROM companies'):
                            if r['do_not_contact']: continue
                            if r['demo_path'] and r['chat_verified'] and r['consent']: queue(c,'send',r['id'])
                            elif not r['demo_path'] and r['score']>=20: queue(c,'demo',r['id'])
                elif path=='/api/import':
                    records=x.get('companies',[])
                    if not isinstance(records,list) or len(records)>500: raise ValueError('Передайте массив до 500 компаний за запрос')
                    ids=[add_company(c,r) for r in records];out={'ids':ids}
                elif path=='/api/search':
                    query=str(x.get('query','')).strip()[:80]; provider=x.get('provider')
                    if not query: raise ValueError('Введите нишу')
                    n=search_2gis(c,query) if provider=='2gis' else search_yandex(c,query) if provider=='yandex' else None
                    if n is None: raise ValueError('Неизвестный источник')
                    out={'added':n}
                elif path in ('/api/analyze','/api/demo','/api/enqueue'):
                    cid=int(x['id']); row=c.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
                    if not row: raise ValueError('Компания не найдена')
                    if path=='/api/enqueue':
                        if not row['demo_path']: raise ValueError('Сначала создайте демо')
                        if not row['consent'] or not row['chat_verified'] or row['do_not_contact']: raise ValueError('Нужны разрешение и подтверждённый деловой чат')
                        queue(c,'send',cid);c.execute("UPDATE companies SET status='в очереди' WHERE id=?",(cid,))
                    else: queue(c,'analyze' if path=='/api/analyze' else 'demo',cid)
                    out={'ok':True}
                elif path=='/api/chat':
                    cid=int(x['id']); row=c.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
                    if not row: raise ValueError('Компания не найдена')
                    channel=x.get('channel','telegram'); contact=str(x.get('username','')).strip()
                    if channel!='telegram' or not re.fullmatch(r'@[A-Za-z0-9_]{5,32}',contact): raise ValueError('Укажите деловой @username Telegram')
                    evidence=str(x.get('evidence','')).strip()
                    if not x.get('verified') or len(evidence)<8: raise ValueError('Укажите источник, подтверждающий связь контакта с компанией')
                    c.execute('UPDATE companies SET channel=?,telegram_username=?,chat_verified=1,contact_evidence=? WHERE id=?',(channel,contact,evidence[:500],cid))
                    if row['consent'] and row['demo_path'] and settings(c)['auto']: queue(c,'send',cid)
                    event(c,'chat_verified',str(cid));out={'ok':True}
                elif path=='/api/consent':
                    cid=int(x['id']); row=c.execute('SELECT * FROM companies WHERE id=?',(cid,)).fetchone()
                    if not row or not row['chat_verified']: raise ValueError('Сначала подтвердите чат')
                    if not x.get('evidence'): raise ValueError('Укажите основание разрешения')
                    c.execute('UPDATE companies SET consent=1,consent_at=? WHERE id=?',(now(),cid));event(c,'consent',f'{cid}: {str(x["evidence"])[:200]}');out={'ok':True}
                    if row['demo_path'] and settings(c)['auto']: queue(c,'send',cid)
                elif path=='/api/stop':
                    cid=int(x['id']); c.execute("UPDATE companies SET do_not_contact=1,consent=0,status='отказ' WHERE id=?",(cid,));c.execute("UPDATE jobs SET state='cancelled' WHERE company_id=? AND kind='send' AND state='queued'",(cid,));event(c,'opt_out',str(cid));out={'ok':True}
                else: self.send_error(404);return
            self.respond(out)
        except Exception as e:
            message=str(e) if isinstance(e,(ValueError,TelegramUnavailable,TelegramPaused)) else 'Операция не выполнена. Проверьте настройки и повторите попытку.'
            self.respond({'error':message[:400]},400)

def run_server():
    init();TG.start()
    threading.Thread(target=worker,daemon=True).start()
    threading.Thread(target=polling,daemon=True).start()
    threading.Thread(target=discovery,daemon=True).start()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    return server

if __name__=='__main__':
    server=run_server()
    print(f'Юрий CRM: http://{HOST}:{PORT}',flush=True)
    try: server.serve_forever()
    finally: STOP.set();server.server_close()
