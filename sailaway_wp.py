"""WP activo de tus veleros, leido de sailaway.world con tu sesion (no hace falta el juego prendido).

Con la sesion iniciada, la pagina de cada velero lista los waypoints de su ruta y marca con un tilde
los que ya paso: el primero sin tilde es el activo. Sin sesion esa lista no aparece, por eso se entra
con tu usuario y clave:
  - en la PC: credentials.json (no se sube a GitHub)   {"email": "...", "password": "..."}
  - en GitHub: secretos SAILAWAY_EMAIL y SAILAWAY_PASSWORD

Ademas, de cada velero tuyo:
  - la polar que publica Sailaway (polarexport.pl, solo con sesion), renovada cada 6 h;
  - lo que navega: viento real, angulo y velocidad en el agua, anotados en cada lectura y acumulados por
    casillas de viento y angulo (la polar "medida", para comparar con la de Sailaway);
  - con --olas: la medicion de olas (para ajustar el ruteo de prueba con olas). En cada lectura de un velero que
    navega a vela se guarda viento, angulo, velocidad y la ola en su posicion (Open-Meteo Marine); cada 6 h, las
    entradas del libro de bitacora de Sailaway (logbookexport.pl), que traen la altura de ola del juego.
    analisis_olas.py las analiza.

Uso:
  python sailaway_wp.py                 -> WP activo de los veleros de las bitacoras
  python sailaway_wp.py --guardar 47502 -> guarda la pagina (con sesion) en sailaway_pagina.local.html, para revisarla
  python sailaway_wp.py --json salida.json [--previo URL o archivo]   (lo anterior: polares y lo medido)
         [--olas medicion_olas.json --olas-previo URL o archivo]       (la medicion de olas, que se acumula)
"""
import calendar, html, http.cookiejar, json, os, re, sys, time, urllib.parse, urllib.request

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

class ErrorClave(Exception):
    """Sailaway no acepto el usuario o la clave (p. ej. se cambio la clave y no se actualizo aca)."""

class Sesion:
    """Sesion de sailaway.world: entra una vez y vuelve a entrar si la sesion vence (dura ~2 h).
    Sin email y clave usa los de credentials.json (o los secretos de GitHub)."""
    def __init__(self, email=None, clave=None):
        self.cuenta = (email, clave) if email and clave else None
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.desde = 0

    def _abrir(self, url, datos=None):
        req = urllib.request.Request(url, data=datos, headers=UA)
        with self.op.open(req, timeout=30) as r:
            return r.read().decode('utf-8', 'replace')

    def entrar(self):
        email, clave = self.cuenta or credenciales()
        if not email or not clave: raise ErrorClave('faltan el usuario y la clave de Sailaway')
        datos = urllib.parse.urlencode({'p': 'login', 'r': '/home', 'a': '1', 'e': email, 'w': clave,
                                        'stayloggedin': '1', 'login': 'Log in'}).encode()
        h = self._abrir(BASE, datos)
        # si no entra, Sailaway vuelve al formulario con el motivo marcado con un asterisco ("email address unknown"...)
        m = re.search(r'<span class="title clr2">\*</span>\s*([^<]+)', h)
        if m: raise ErrorClave(html.unescape(m.group(1)).strip())
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

# ─── Polar de Sailaway ───
def polar(nr, s):
    """La polar que publica Sailaway para ese velero: {tws: [...], twa: [...], v: [[por twa] por tws]} o None."""
    t = s._abrir('https://sailaway.world/cgi-bin/sailaway3/polarexport.pl?nr=%d' % int(nr))
    filas = [l.strip().split(';') for l in t.replace('\r', '').split('\n') if ';' in l]
    if len(filas) < 5 or not filas[0][0].upper().startswith('TWA'): return None
    tws = [float(x) for x in filas[0][1:]]
    twa, v = [], []
    for f in filas[1:]:
        try: a = float(f[0]); vals = [max(0.0, float(x)) for x in f[1:]]   # Sailaway pone negativos donde no avanza
        except ValueError: continue
        vals = (vals + [0.0] * len(tws))[:len(tws)]
        twa.append(a); v.append([round(x, 2) for x in vals])
    if not twa or max(max(f) for f in v) <= 0: return None
    return {'tws': tws, 'twa': twa, 'v': v, 't': int(time.time())}

# ─── Lo que navega: instrumentos de la pagina del velero ───
def instrumentos(pagina):
    t = html.unescape(re.sub(r'<[^>]+>', ' ', pagina)).replace('\xa0', ' ')
    t = re.sub(r'\s+', ' ', t)
    m = lambda rx: re.search(rx, t)
    d = {}
    x = m(r'Heading \(magnetic\)/speed\s*(\d+)°,\s*([\d.]+)\s*kn'); d['stw'] = float(x.group(2)) if x else None
    x = m(r'Heading \(true\)\s*(\d+)°'); d['hdg'] = float(x.group(1)) if x else None
    x = m(r'True wind direction/speed\s*(\d+)°,\s*([\d.]+)\s*kn'); d['twd'], d['tws'] = (float(x.group(1)), float(x.group(2))) if x else (None, None)
    x = m(r'Sailmode:\s*([^(]+?)\s*(?:\(|Last)'); d['modo'] = x.group(1).strip().lower() if x else ''
    x = m(r'Auto navigation\s*(.+?)\s*Heading \(magnetic'); d['auto'] = x.group(1).strip() if x else ''
    x = m(r'Last position update:\s*(\d{4}-\d\d-\d\d \d\d:\d\d)'); d['upd'] = x.group(1) if x else None
    x = m(r"Position\s*([NS])\s*(\d+)°\s*([\d.]+)'\s*([EW])\s*(\d+)°\s*([\d.]+)'")
    d['lat'], d['lon'] = (_grados(x.group(1), x.group(2), x.group(3)), _grados(x.group(4), x.group(5), x.group(6))) if x else (None, None)
    x = m(r'GPS course/speed over ground\s*(\d+)°,\s*([\d.]+)\s*kn'); d['cog'], d['sog'] = (float(x.group(1)), float(x.group(2))) if x else (None, None)
    return d

TWS_BINS = [(0, 4, 3), (4, 7.5, 5), (7.5, 12.5, 10), (12.5, 17.5, 15), (17.5, 25, 20), (25, 35, 30), (35, 45, 40), (45, 999, 50)]
def anotar(obs, ins):
    """Suma una lectura a lo medido: casillas de viento real (las columnas de la polar de Sailaway) y de 10° de angulo.
    obs: {"tws": [...], "n": {"10|45": [cantidad, suma de velocidad, maxima]}, "upd": ultima lectura}"""
    if not ins or None in (ins.get('stw'), ins.get('hdg'), ins.get('twd'), ins.get('tws')): return obs
    if ins['stw'] < 0.5 or ins['tws'] < 1: return obs
    if any(p in ins.get('modo', '') for p in ('motor', 'anchor', 'moor', 'harbo', 'dock')): return obs
    obs = obs or {'tws': [b[2] for b in TWS_BINS], 'n': {}, 'upd': None}
    if ins.get('upd') and ins['upd'] == obs.get('upd'): return obs   # la misma posicion de Sailaway: ya anotada
    twa = abs((ins['twd'] - ins['hdg'] + 540) % 360 - 180)
    col = next(b[2] for b in TWS_BINS if b[0] <= ins['tws'] < b[1])
    clave = '%d|%d' % (col, min(175, int(twa // 10) * 10 + 5))
    c = obs['n'].get(clave, [0, 0.0, 0.0])
    obs['n'][clave] = [c[0] + 1, round(c[1] + ins['stw'], 2), max(c[2], ins['stw'])]
    obs['upd'] = ins.get('upd')
    return obs

# ─── Medicion de olas: como frena la ola a cada velero en el juego ───
MAX_MUESTRAS = 8000
def navegando(ins):
    if not ins or None in (ins.get('stw'), ins.get('hdg'), ins.get('twd'), ins.get('tws')): return False
    if ins['stw'] < 0.5 or ins['tws'] < 1: return False
    return not any(p in ins.get('modo', '') for p in ('motor', 'anchor', 'moor', 'harbo', 'dock'))

def ola_en(lat, lon):
    """(altura m, de donde viene °, periodo s) de la ola ahora en ese lugar, de Open-Meteo Marine."""
    u = 'https://marine-api.open-meteo.com/v1/marine?latitude=%.4f&longitude=%.4f&current=wave_height,wave_direction,wave_period' % (lat, lon)
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
        c = json.loads(r.read().decode('utf-8')).get('current') or {}
    return c.get('wave_height'), c.get('wave_direction'), c.get('wave_period')

def bitacora_juego(nr, s):
    """Entradas del libro de bitacora de Sailaway navegando: [t, lat, lon, ola del juego, stw, sog, hdg, twd, tws]."""
    t = s._abrir('https://sailaway.world/cgi-bin/sailaway3/logbookexport.pl?nr=%d' % int(nr))
    filas = []
    for l in t.replace('\r', '').split('\n'):
        if not l.startswith('B,'): continue
        c = [x.strip().strip('"') for x in re.findall(r'"[^"]*"|[^,]+', l[2:])]
        # datetime, status, coordenadas, lat, lon, HDG, SPD, COG, SOG, TWD, TWS, wp, wplat, wplon, temp, presion, nubes, niebla, lluvia, nieve, ola, nota
        if len(c) < 21 or c[1].lower() != 'sailing': continue
        try:
            tt = calendar.timegm(time.strptime(c[0], '%Y-%m-%d %H:%M'))   # Sailaway da la hora en UTC
            filas.append([tt, round(float(c[3]), 4), round(float(c[4]), 4), round(float(c[20]), 2), round(float(c[6]), 2), round(float(c[8]), 2),
                          round(float(c[5]), 1), round(float(c[9]), 1), round(float(c[10]), 2)])
        except (ValueError, IndexError): continue
    return filas

def medir_olas(M, nr, ins, s):
    """Suma a la medicion M: la lectura de ahora (si navega a vela) con la ola en su posicion, y cada 6 h la bitacora del juego.
    M: {"muestras": {nr: [[t, tws, twa, stw, sog, ola m, angulo de la ola a la proa °, periodo s], ...]},
        "bitacora": {nr: [[t, lat, lon, ola del juego, stw, sog, hdg, twd, tws], ...]}, "upd": {nr: ...}, "bit_t": {nr: t}}"""
    k = str(nr)
    for c in ('muestras', 'bitacora', 'upd', 'bit_t'): M.setdefault(c, {})
    if navegando(ins) and ins.get('lat') is not None and ins.get('upd') != M['upd'].get(k):
        try:
            hs, wdir, tp = ola_en(ins['lat'], ins['lon'])
            if hs is not None and wdir is not None:
                twa = abs((ins['twd'] - ins['hdg'] + 540) % 360 - 180)
                ang = abs((wdir - ins['hdg'] + 540) % 360 - 180)   # 0: la ola llega de proa, 180: de popa
                L = M['muestras'].setdefault(k, [])
                L.append([int(time.time()), ins['tws'], round(twa), ins['stw'], ins.get('sog'), hs, round(ang), tp])
                del L[:-MAX_MUESTRAS]
                M['upd'][k] = ins['upd']
        except Exception as e: print('  ola %s: %s' % (nr, e), file=sys.stderr)
    if time.time() - M['bit_t'].get(k, 0) > 6 * 3600:
        try:
            nuevas, L = bitacora_juego(nr, s), M['bitacora'].setdefault(k, [])
            vistas = {f[0] for f in L}
            L.extend(f for f in nuevas if f[0] not in vistas)
            L.sort(); del L[:-MAX_MUESTRAS]
            M['bit_t'][k] = int(time.time())
        except Exception as e: print('  bitacora %s: %s' % (nr, e), file=sys.stderr)

def leer(nrs, sesion=None, previo=None, medir=True, olas=None):
    """{nr: {i, total, wp: {lat, lon}, wps, t, polar, obs}} de los veleros que tienen lista (los tuyos).
    previo: lo de la lectura anterior (polar de Sailaway, que se renueva cada 6 h, y lo medido, que se acumula).
    olas: la medicion de olas (se completa aca, ver medir_olas)."""
    s = sesion or Sesion()
    previo = previo or {}
    res = {}
    for nr in nrs:
        try:
            pagina = s.pagina(nr)
            wps = lista_waypoints(pagina)
        except ErrorClave:
            raise
        except Exception as e:
            print('  Sailaway %s: %s' % (nr, e), file=sys.stderr); continue
        ant = previo.get(str(nr)) or {}
        if wps:
            i = activo(wps)
            d = {'i': i, 'total': len(wps), 'wp': wps[i] if i is not None else None, 't': int(time.time())}   # sin la lista entera: las paginas usan el WP activo
        else:   # sin ruta cargada en el juego: igual se leen su polar y lo que navega
            d = {'sinRuta': True, 't': int(time.time())}
        pol = ant.get('polar')
        if not pol or time.time() - pol.get('t', 0) > 6 * 3600:
            try: pol = polar(nr, s) or pol
            except Exception as e: print('  polar %s: %s' % (nr, e), file=sys.stderr)
        if pol: d['polar'] = pol
        obs, ins = ant.get('obs'), instrumentos(pagina)
        if medir: obs = anotar(obs, ins)
        if olas is not None: medir_olas(olas, nr, ins, s)
        if obs: d['obs'] = obs
        res[str(nr)] = d
        time.sleep(0.5)
    return res

def json_de(origen):
    """Un json anterior (archivo o URL), para seguir acumulando; {} si no hay."""
    if not origen: return {}
    try:
        if origen.startswith('http'):
            with urllib.request.urlopen(urllib.request.Request(origen + ('&' if '?' in origen else '?') + 't=%d' % time.time(), headers=UA), timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        with open(origen, encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        print('  sin datos anteriores de %s (%s)' % (origen, e), file=sys.stderr); return {}

def previo_de(origen):
    """veleros{} de un wp_activo.json anterior (archivo o URL), para seguir acumulando."""
    return json_de(origen).get('veleros') or {}

def veleros():
    """Numeros de los veleros de las bitacoras (los MMSI de los .md) y de todos los de la flota.
    En el repo publico no estan los .md: ahi compartir.py deja la lista en veleros.json."""
    try:
        import generar_html as g
    except ImportError:
        with open(os.path.join(DIR, 'veleros.json'), encoding='utf-8') as f: return json.load(f)
    nrs = set()
    try: nrs.update(int(v['nr']) for v in g.flota_md())   # todos los veleros de la flota (MMSI de su tabla)
    except Exception as e: print('  flota:', e, file=sys.stderr)
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
    previo = previo_de(a[a.index('--previo') + 1]) if '--previo' in a else {}
    olas = (json_de(a[a.index('--olas-previo') + 1]) if '--olas-previo' in a else {}) if '--olas' in a else None
    try:
        datos = leer(veleros(), previo=previo, olas=olas)
    except ErrorClave as e:
        # las paginas lo muestran y ofrecen cargar el usuario y la clave nuevos
        print('Sailaway no acepto el usuario o la clave:', e)
        if a[:1] == ['--json']:
            with open(a[1], 'w', encoding='utf-8') as f:
                json.dump({'t': int(time.time()), 'error': 'clave', 'mensaje': str(e), 'veleros': {}}, f, ensure_ascii=False)
        sys.exit()
    if a[:1] == ['--json'] and datos:   # sin datos (p. ej. Sailaway caido) no se pisa lo ultimo bueno
        with open(a[1], 'w', encoding='utf-8') as f: json.dump({'t': int(time.time()), 'veleros': datos}, f, ensure_ascii=False)
    if olas is not None and datos:
        olas['t'] = int(time.time())
        with open(a[a.index('--olas') + 1], 'w', encoding='utf-8') as f: json.dump(olas, f, ensure_ascii=False, separators=(',', ':'))
        print('olas: %d lecturas con ola · %d entradas de bitacora del juego' % (sum(map(len, olas.get('muestras', {}).values())),
              sum(map(len, olas.get('bitacora', {}).values()))))
    for nr, d in datos.items():
        n_obs = sum(c[0] for c in (d.get('obs') or {}).get('n', {}).values())
        if d.get('sinRuta'): print('%s: sin ruta en el juego · polar de Sailaway %s · %d mediciones' % (nr, 'sí' if d.get('polar') else 'no', n_obs)); continue
        print('%s: WP activo %s de %d · polar de Sailaway %s · %d mediciones' % (nr, (d['i'] if d['i'] is not None else '—'), d['total'] - 1,
              'sí' if d.get('polar') else 'no', n_obs))
    if not datos: print('Ningun velero con lista de waypoints (¿usuario y clave bien cargados?)')
