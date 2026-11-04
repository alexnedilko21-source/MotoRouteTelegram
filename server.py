#!/usr/bin/env python3
"""Local prototype for courier photo route. No OpenAI dependency."""
import base64, json, os, re, subprocess, tempfile, urllib.request, urllib.error, hmac, hashlib, time, threading, ipaddress, socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT=Path(__file__).parent
TOKEN=os.environ.get('TELEGRAM_BOT_TOKEN','').strip()
PUBLIC=os.environ.get('PUBLIC_MODE','0')=='1'
ALLOWED_USERS={x.strip() for x in os.environ.get('ALLOWED_TELEGRAM_USER_IDS','').split(',') if x.strip()}
RATE_LOCK=threading.Lock()
REQUESTS={}

def telegram_identity(init_data):
    if not TOKEN: return None
    from urllib.parse import parse_qsl
    try:
        params=dict(parse_qsl(init_data,keep_blank_values=True))
        signature=params.pop('hash','')
        params.pop('signature',None)
        auth_date=int(params.get('auth_date','0'))
        if abs(time.time()-auth_date)>86400: return None
        message='\n'.join(f'{k}={v}' for k,v in sorted(params.items()))
        secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
        digest=hmac.new(secret,message.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest,signature): return None
        user=json.loads(params.get('user','{}'))
        return str(user['id'])
    except (ValueError,KeyError,TypeError): return None

def rate_limit(user_id):
    now=time.monotonic()
    with RATE_LOCK:
        recent=[v for v in REQUESTS.get(user_id,[]) if now-v<3600]
        if len(recent)>=15: return False
        recent.append(now)
        REQUESTS[user_id]=recent
        return True

MODEL=os.environ.get('GEMINI_MODEL','gemini-2.5-flash')

def extract_lines(text):
    lines=[]
    for raw in text.splitlines():
        s=raw.strip()
        s=re.sub(r'^\s*(?:\d{1,3}\s*[.)-]|[•●▪])\s*','',s)
        if s and not re.fullmatch(r'[-—= ]+',s): lines.append(s)
    return lines

def gemini_ocr(data,mime):
    key=os.environ.get('GEMINI_API_KEY','').strip()
    if not key: raise ValueError('Не задан GEMINI_API_KEY. Выберите локальный OCR или настройте ключ на сервере.')
    prompt=('Прочитай адреса и номера почтоматов на фото. Верни ТОЛЬКО адреса, по одному на строке, '
            'строго сверху вниз и слева направо в порядке нумерации листа. '
            'Ничего не оптимизируй и не переставляй. Если место неразборчиво, сохрани его как [НЕЯСНО]. '
            'Не додумывай отсутствующие адреса. Для каждой строки сохрани номер почтомата, если он есть.')
    body={'contents':[{'parts':[{'text':prompt},{'inline_data':{'mime_type':mime,'data':base64.b64encode(data).decode()}}]}],
          'generationConfig':{'temperature':0}}
    url=f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent'
    req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','x-goog-api-key':key},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=50) as res: answer=json.load(res)
    except urllib.error.HTTPError as e:
        raise ValueError(f'Ошибка Gemini API ({e.code}): проверьте ключ, модель и квоту')
    try:
        return '\n'.join(p.get('text','') for p in answer['candidates'][0]['content']['parts'])
    except (KeyError, IndexError, TypeError) as exc: raise ValueError('Gemini не вернул распознанный текст') from exc

def local_ocr(data):
    # Installed by user; e.g. brew install tesseract tesseract-lang
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'photo.jpg'; path.write_bytes(data)
        langs=os.environ.get('OCR_LANGS','ukr+rus+eng')
        cmd=['tesseract',str(path),'stdout','-l',langs,'--psm','6']
        try: proc=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=35)
        except FileNotFoundError: raise ValueError('Tesseract не установлен. На Mac: brew install tesseract tesseract-lang')
        if proc.returncode: raise ValueError('Ошибка OCR: '+proc.stderr.decode(errors='replace')[-260:])
        return proc.stdout.decode(errors='replace')

class Handler(BaseHTTPRequestHandler):
    def _out(self,code,data,kind='application/json; charset=utf-8'):
        payload=json.dumps(data,ensure_ascii=False).encode() if isinstance(data,(dict,list)) else data
        self.send_response(code); self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(payload)
    def do_GET(self):
        if self.path.split('?',1)[0] in ('/','/index.html'): self._out(200,(ROOT/'static/index.html').read_bytes(),'text/html; charset=utf-8')
        elif self.path=='/health': self._out(200,{'ok':True})
        else:self._out(404,{'error':'Not found'})
    def do_POST(self):
        if self.path!='/api/recognize':return self._out(404,{'error':'Not found'})
        try:
            if PUBLIC:
                ident=telegram_identity(self.headers.get('X-Telegram-Init-Data',''))
                if not ident: return self._out(401,{'error':'Откройте приложение внутри Telegram'})
                if ALLOWED_USERS and ident not in ALLOWED_USERS: return self._out(403,{'error':'Нет доступа'})
                if not rate_limit(ident): return self._out(429,{'error':'Лимит: 15 фотографий в час'})
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>9*1024*1024: return self._out(413,{'error':'Фото должно быть меньше 7 МБ'})
            req=json.loads(self.rfile.read(n))
            mime=req.get('mime','image/jpeg');mode=req.get('mode','local')
            if mime not in ('image/jpeg','image/png','image/webp'): raise ValueError('Поддерживаются JPG, PNG, WEBP')
            b64=req.get('image','');data=base64.b64decode(b64,validate=True)
            if len(data)>7*1024*1024: raise ValueError('Фото больше 7 МБ')
            text=gemini_ocr(data,mime) if mode=='gemini' else local_ocr(data) if mode=='local' else None
            if text is None:raise ValueError('Неизвестный режим')
            self._out(200,{'lines':extract_lines(text),'raw_text':text,'mode':mode})
        except Exception as e:self._out(400,{'error':str(e)})

if __name__=='__main__':
    # Local-only by default. For Telegram need deployment with HTTPS and auth/rate limiting.
    if PUBLIC and not TOKEN: raise SystemExit('Set TELEGRAM_BOT_TOKEN for public deployment')
    host='0.0.0.0' if PUBLIC else '127.0.0.1'
    port=int(os.environ.get('PORT','8767'))
    print(f'МотоМаршрут: http://{host}:{port}',flush=True)
    ThreadingHTTPServer((host,port),Handler).serve_forever()
