#!/usr/bin/env python3
"""Servidor de la clase: sirve la web y recoge los votos de los estudiantes.

Uso:  python servidor.py [puerto]
El docente abre http://localhost:8777/ ; los estudiantes entran con la dirección
de red que muestra el servidor (misma red Wi-Fi) a /votar.html
Solo biblioteca estándar de Python 3. Los votos se guardan en datos/votos.json.
"""
import json, os, socket, sys, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get('PORT') or (sys.argv[1] if len(sys.argv) > 1 else 8777))
TEACHER_KEY = os.environ.get('TEACHER_KEY', '')   # en la nube: clave del docente (obligatoria)
PUBLIC_URL = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
ALLOWED_TOP = {'index.html', 'clase.html', 'votar.html'}
ALLOWED_DIRS = ('fonts/', 'vendor/')
MAX_CLIENTS_PER_IP = 300      # tope de dispositivos distintos por IP y votación (anti-spam)
IP_CLIENTS = {}
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'datos')
FILE = os.path.join(DATA, 'votos.json')
LOCK = threading.Lock()
os.makedirs(DATA, exist_ok=True)

STATE = {'votes': {}, 'active': None, 'open': False, 'seen': {}, 'labels': {}}  # votes[key][client] = option index
if os.path.exists(FILE):
    try:
        STATE.update(json.load(open(FILE, encoding='utf-8')))
        STATE['seen'] = {}
    except Exception:
        pass


def save():
    tmp = FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'votes': STATE['votes'], 'active': STATE['active'], 'open': STATE['open'], 'labels': STATE['labels']}, f, ensure_ascii=False)
    os.replace(tmp, FILE)


def counts(key, n=None):
    v = STATE['votes'].get(key, {})
    size = n or (max(v.values()) + 1 if v else 0)
    out = [0] * max(size, 0)
    for o in v.values():
        if 0 <= o < len(out):
            out[o] += 1
    return out


def snapshot():
    now = time.time()
    online = sum(1 for t in STATE['seen'].values() if now - t < 8)
    res = {k: counts(k, 4 if not k.startswith('r') else 3) for k in STATE['votes']}
    return {'active': STATE['active'], 'open': STATE['open'], 'online': online, 'results': res}


def lan_urls():
    urls = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        urls.append('http://%s:%d/votar.html' % (s.getsockname()[0], PORT))
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            u = 'http://%s:%d/votar.html' % (ip, PORT)
            if not ip.startswith('127.') and u not in urls:
                urls.append(u)
    except Exception:
        pass
    return urls


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def log_message(self, fmt, *args):
        if '/api/' not in (args[0] if args else ''):
            sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def real_ip(self):
        xf = self.headers.get('X-Forwarded-For', '')
        return (xf.split(',')[0].strip() if xf else self.client_address[0])

    def is_teacher(self):
        if TEACHER_KEY:
            return self.headers.get('X-Teacher-Key', '') == TEACHER_KEY
        return self.client_address[0] in ('127.0.0.1', '::1')

    def allowed_path(self, p):
        p = p.lstrip('/')
        return p == '' or p in ALLOWED_TOP or p.startswith(ALLOWED_DIRS) and '..' not in p

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = min(int(self.headers.get('Content-Length') or 0), 20000)
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return {}

    def do_GET(self):
        p = urlparse(self.path).path
        if p == '/api/estado':
            cid = self.headers.get('X-Client') or ''
            with LOCK:
                if cid:
                    STATE['seen'][cid] = time.time()
                    if len(STATE['seen']) > 5000:
                        cut = time.time() - 60
                        STATE['seen'] = {k: t for k, t in STATE['seen'].items() if t > cut}
                s = snapshot()
                a = s['active']
                mine = None
                if a and cid:
                    mine = STATE['votes'].get(a['key'], {}).get(cid)
                return self.send_json({'active': a, 'open': s['open'], 'mine': mine,
                                       'online': s['online'], 'results': s['results'] if self.is_teacher() else None})
        if p == '/api/admin/exportar':
            if not self.is_teacher():
                return self.send_json({'ok': False, 'error': 'solo el docente'}, 403)
            import csv, io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['clave', 'pregunta', 'opcion', 'votos'])
            with LOCK:
                for k in sorted(STATE['votes']):
                    lab = STATE['labels'].get(k, {})
                    opts = lab.get('options') or (['Copia', 'No copia', 'Se parece, pero no sé'] if k.startswith('r') else [])
                    c = counts(k, max(len(opts), 1))
                    for i, n in enumerate(c):
                        w.writerow([k, lab.get('title', ''), opts[i] if i < len(opts) else 'opcion %d' % (i + 1), n])
            b = ('\ufeff' + buf.getvalue()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="votos-sala-de-remezclas.csv"')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if p == '/api/info':
            urls = [PUBLIC_URL + '/votar.html'] if PUBLIC_URL else lan_urls()
            return self.send_json({'urls': urls, 'teacher': self.is_teacher(), 'port': PORT})
        if p == '/' or p == '':
            self.path = '/index.html'
        elif not self.allowed_path(p):
            return self.send_json({'ok': False, 'error': 'no encontrado'}, 404)
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        b = self.body()
        if p == '/api/votar':
            cid = str(b.get('client') or '')[:64]
            key = str(b.get('key') or '')[:40]
            opt = b.get('option')
            with LOCK:
                a = STATE['active']
                if not (cid and a and a['key'] == key and STATE['open']):
                    return self.send_json({'ok': False, 'error': 'cerrada'}, 409)
                if not isinstance(opt, int) or not 0 <= opt < len(a['options']):
                    return self.send_json({'ok': False, 'error': 'opcion'}, 400)
                ip = self.real_ip()
                known = IP_CLIENTS.setdefault((ip, key), set())
                if cid not in known and len(known) >= MAX_CLIENTS_PER_IP:
                    return self.send_json({'ok': False, 'error': 'limite'}, 429)
                known.add(cid)
                STATE['votes'].setdefault(key, {})[cid] = opt
                save()
            return self.send_json({'ok': True})
        if not self.is_teacher():
            return self.send_json({'ok': False, 'error': 'solo el docente'}, 403)
        with LOCK:
            if p == '/api/admin/activar':
                key = str(b.get('key') or '')[:40]
                opts = [str(o)[:80] for o in (b.get('options') or [])][:8]
                if not key or len(opts) < 2:
                    return self.send_json({'ok': False}, 400)
                STATE['active'] = {'key': key, 'title': str(b.get('title') or '')[:200],
                                   'subtitle': str(b.get('subtitle') or '')[:300], 'options': opts}
                STATE['open'] = True
                STATE['labels'][key] = {'title': STATE['active']['title'], 'options': opts}
            elif p == '/api/admin/cerrar':
                STATE['open'] = False
            elif p == '/api/admin/abrir':
                STATE['open'] = bool(STATE['active'])
            elif p == '/api/admin/borrar':
                key = b.get('key')
                if key == '*':
                    STATE['votes'] = {}
                    IP_CLIENTS.clear()
                elif key:
                    STATE['votes'].pop(str(key), None)
                    for k in [k for k in IP_CLIENTS if k[1] == str(key)]:
                        IP_CLIENTS.pop(k, None)
            save()
            return self.send_json({'ok': True, **snapshot()})
        return self.send_json({'ok': False}, 404)


def main():
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    print('=' * 60)
    print(' LA SALA DE REMEZCLAS')
    print(' Docente   : %s' % (PUBLIC_URL or 'http://localhost:%d/' % PORT))
    for u in ([PUBLIC_URL + '/votar.html'] if PUBLIC_URL else lan_urls()):
        print(' Estudiantes: %s' % u)
    print(' (si no entran: permita Python en el Firewall de Windows,')
    print('  "Redes privadas", y use la misma red Wi-Fi)')
    print(' Votos guardados en: %s' % FILE)
    print(' Cierre esta ventana al terminar la clase.')
    print('=' * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
