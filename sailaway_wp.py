"""WP activo de tus veleros, leido de sailaway.world con tu sesion (no hace falta el juego prendido).

Con la sesion iniciada, la pagina de cada velero lista los waypoints de su ruta y marca con un tilde
los que ya paso: el primero sin tilde es el activo. Sin sesion esa lista no aparece, por eso se entra
con tu usuario y clave:
  - en la PC: credentials.json (no se sube a GitHub)   {"email": "...", "password": "..."}
  - en GitHub: secretos SAILAWAY_EMAIL y SAILAWAY_PASSWORD

Uso:
  python sailaway_wp.py                 -> WP activo de los veleros de las bitacoras
  python sailaway_wp.py --guardar 47502 -> guarda la pagina (con sesion) en sailaway_pagina.local.html, para revisarla
  python sailaway_wp.py --json salida.json
"""
import html, http.cookiejar, json, os, re, sys, time, urllib.parse, urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
BASE = 'https://sailaway.world/cgi-bin/sailaway3/page.pl'
UA = {'User-Agent': 'Mozilla/5.0 (bitacoras SA Viaje)'}

def credenciales():
    e, w = os.environ.get('SAILAWAY_EMAIL'), os.environ.get('SAILAWAY_PASSWORD')
    if e and w: return e, w
    try:
        with open(os.path.join(DIR, 'credentials.json'), encoding='utf-8') as f: c = json.load(f)
        return c.get('email'), c.get('password')
    except (OSError, ValueError):
        return None, None

class Sesion:
    """Sesion de sailaway.world: entra una vez y vuelve a entrar si la sesion vence (dura ~2 h)."""
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.desde = 0

    def _abrir(self, url, datos=None):
        req = urllib.request.Request(url, data=datos, headers=UA)
        with self.op.open(req, timeout=30) as r:
            return r.read().decode('utf-8', 'replace')

    def entrar(self):
        email, clave = credenciales()
        if not email or not clave: raise RuntimeError('faltan el usuario y la clave de Sailaway (credentials.json o secretos)')
        datos = urllib.parse.urlencode({'p': 'login', 'r': '/home', 'a': '1', 'e': email, 'w': clave,
                                        'stayloggedin': '1', 'login': 'Log in'}).encode()
        self._abrir(BASE, datos)
        self.desde = time.time()

    def pagina(self, nr):
        if not self.desde or time.time() - self.desde > 90 * 60: self.entrar()
        h = self._abrir(BASE + '?p=boat&nr=%d&nr2=0' % int(nr))
        if not lista_waypoints(h) and time.time() - self.desde > 60:   # sesion vencida: se entra de nuevo y se reintenta
            self.entrar(); h = self._abrir(BASE + '?p=boat&nr=%d&nr2=0' % int(nr))
        return h

# ─── Lectura de la lista de waypoints ───
COORD = re.compile(r"([NS])\s*(\d+)\s*(?:&deg;|°)\s*([\d.]+)'?\s*([EW])\s*(\d+)\s*(?:&deg;|°)\s*([\d.]+)'?")
# lo que marca un WP ya pasado: un tilde (caracter o entidad) o una imagen de check
# (sailaway.world usa la raiz cuadrada: &radic; = √)
TILDE = re.compile(r'&radic;|√|✓|✔|☑|&#10003;|&#10004;|&#x2713;|&#x2714;|&check;|<img[^>]*(?:check|tick|done|ok)[^>]*>', re.I)

def _grados(h, g, m):
    v = int(g) + float(m) / 60
    return -v if h in ('S', 'W') else v

def lista_waypoints(pagina):
    """[{lat, lon, pasado}] en el orden de la ruta, o [] si la pagina no trae la lista (sin sesion)."""
    i = pagina.find('Waypoints')
    if i < 0: i = pagina.lower().find('waypoints')
    if i < 0: return []
    zona = pagina[i:]
    fin = re.search(r'ShowWindy|</table>\s*</td>\s*</tr>\s*</table>|<!-- home button', zona)
    if fin: zona = zona[:fin.start()]
    wps = []
    # cada WP es una fila: el tilde (si lo tiene) esta a la izquierda de la coordenada, en la misma fila
    filas = re.split(r'<tr\b|<br\s*/?>|<li\b|<div\b', zona)
    for f in filas:
        m = COORD.search(html.unescape(re.sub(r'<(?!img)[^>]+>', ' ', f)).replace('\xa0', ' ')) or COORD.search(f)
        if not m: continue
        wps.append({'lat': round(_grados(m.group(1), m.group(2), m.group(3)), 5),
                    'lon': round(_grados(m.group(4), m.group(5), m.group(6)), 5),
                    'pasado': bool(TILDE.search(f[:f.find(m.group(0))] if m.group(0) in f else f))})
    return wps

def activo(wps):
    """Indice (desde 0) del WP activo: el primero sin tilde. None si no hay lista o ya paso todos."""
    for i, w in enumerate(wps):
        if not w['pasado']: return i
    return None

def leer(nrs, sesion=None):
    """{nr: {i, total, wp: {lat, lon}, wps, t}} de los veleros que tienen lista (los tuyos)."""
    s = sesion or Sesion()
    res = {}
    for nr in nrs:
        try:
            wps = lista_waypoints(s.pagina(nr))
        except Exception as e:
            print('  Sailaway %s: %s' % (nr, e), file=sys.stderr); continue
        if not wps: continue
        i = activo(wps)
        res[str(nr)] = {'i': i, 'total': len(wps), 'wp': wps[i] if i is not None else None, 'wps': wps, 't': int(time.time())}
        time.sleep(0.5)
    return res

def veleros():
    """Numeros de los veleros de las bitacoras (los MMSI de los .md) y de la flota.
    En el repo publico no estan los .md: ahi compartir.py deja la lista en veleros.json."""
    try:
        import generar_html as g
    except ImportError:
        with open(os.path.join(DIR, 'veleros.json'), encoding='utf-8') as f: return json.load(f)
    nrs = set()
    for base, _ in g.bitacoras().values():
        try: txt = open(g.ruta(base), encoding='utf-8').read()
        except OSError: continue
        nrs.update(int(x) for x in re.findall(r'MMSI:?\*{0,2}\s*(\d{4,6})', txt))
    return sorted(nrs)

if __name__ == '__main__':
    sys.path.insert(0, DIR)
    a = sys.argv[1:]
    if a[:1] == ['--guardar']:
        h = Sesion().pagina(int(a[1]))
        destino = os.path.join(DIR, 'sailaway_pagina.local.html')
        open(destino, 'w', encoding='utf-8').write(h)
        print('guardada en', destino, '·', len(lista_waypoints(h)), 'waypoints encontrados')
        sys.exit()
    if not credenciales()[0]:
        print('Faltan el usuario y la clave de Sailaway (credentials.json o secretos SAILAWAY_EMAIL / SAILAWAY_PASSWORD)'); sys.exit()
    datos = leer(veleros())
    if a[:1] == ['--json'] and datos:   # sin datos (p. ej. Sailaway caido) no se pisa lo ultimo bueno
        with open(a[1], 'w', encoding='utf-8') as f: json.dump({'t': int(time.time()), 'veleros': datos}, f, ensure_ascii=False)
    for nr, d in datos.items():
        print('%s: WP activo %s de %d' % (nr, (d['i'] if d['i'] is not None else '—'), d['total'] - 1))
    if not datos: print('Ningun velero con lista de waypoints (¿usuario y clave bien cargados?)')
