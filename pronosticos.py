"""Pronosticos listos para los ruteos, bajados por GitHub de forma autonoma para toda la flota.

Corre en el repo publico (Action pronosticos.yml; compartir.py publica este archivo, la Action, veleros.json y
destinos.json). Para cada velero que navega (o cuya bitacora sigue en curso) arma el camino de sus PROXIMOS DESTINOS:
su posicion, el WP activo y los siguientes WP de la ruta cargada en Sailaway (o, si no tiene ruta, los proximos
puertos de su bitacora), hasta ~6-7 dias de navegacion. Alrededor de esos caminos (la misma franja que usan las
paginas: +-25% de la distancia) baja de Open-Meteo el viento de ECMWF, GFS e ICON, las corrientes y las olas.

Toda la flota va en UN archivo por modelo: una grilla regular comun (alineada a multiplos del paso) donde solo se
piden los puntos cerca de algun camino; los veleros que navegan juntos (Glory y Enterprise) no cuestan el doble.
Las paginas, al armar un ruteo, usan estos datos si el camino del ruteo queda dentro de la franja de algun velero
(arranca enseguida, sin bajar nada); si no, recien ahi los bajan ellas mismas.

Cuando bajar: cada modelo cuando Open-Meteo publica una corrida nueva (se mira su meta.json), pero no antes de
~11 h de la bajada anterior (unas 2 por dia), salvo que aparezca un camino nuevo que lo bajado no cubre.
Cupo de Open-Meteo gratis: 600 puntos por minuto y 10000 por dia: se espera entre pedidos y los puntos se reparten
para no pasar ~9000 por dia.

Deja todo en pronos/ (index.json y un archivo por modelo) y la Action lo sube a la rama pronos, en un solo commit
sin historial (los archivos son grandes y se reemplazan).

Uso:  python pronosticos.py [--previo carpeta del index.json anterior] [--forzar] [--probar]   (--probar: no baja nada)
"""
import array, base64, calendar, json, math, os, sys, time, urllib.error, urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
SALIDA = os.path.join(DIR, 'pronos')
UA = {'User-Agent': 'Mozilla/5.0 (bitacoras SA Viaje)'}
RAD = math.pi / 180
DIAS = 14   # mas de 14 dias Open-Meteo los cuenta como mas puntos
WP_ACTIVO = 'https://raw.githubusercontent.com/adrianfonq/bitacoras/datos/wp_activo.json'
SAILAWAY_RUTA = 'https://sailaway.world/cgi-bin/sailaway3/boatmapdata.pl?showboatlog=0&showracecourse=0&showwaypoints=1&ubtnr=0&botnr=%d'
# modelo de las paginas -> (api, meta.json de Open-Meteo, resolucion minima en grados)
MODELOS = {
    'ecmwf_ifs025': ('forecast', 'ecmwf_ifs025', 0.25),
    'gfs_seamless': ('forecast', 'ncep_gfs025', 0.25),
    'icon_seamless': ('forecast', 'dwd_icon', 0.25),   # los tres de viento en la MISMA grilla: las paginas los promedian
    'corrientes': ('marine', 'meteofrance_currents', 1 / 12),
    'olas': ('marine', 'meteofrance_wave', 0.25),
}
VIENTO = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless']
MAR = ['corrientes', 'olas']
HORAS_ENTRE = {'ecmwf_ifs025': 11, 'gfs_seamless': 11, 'icon_seamless': 11, 'corrientes': 20, 'olas': 11}   # ~2 por dia (corrientes 1)
CUPO_DIA = 9000
HORIZONTE_H = 96   # horas de navegacion hacia adelante que cubre el camino de cada velero (4 dias; mas lejos, la pagina baja lo que falte)
MIN_BITACORA = 1000   # MN minimos hacia adelante de los veleros con bitacora en curso (que su proximo puerto quede adentro)
ANCHO_MAX = 120   # MN a cada lado del camino como maximo: mas angosta = mas fina (alrededor, las paginas completan ~3°)
PRIORIDAD = 0.75   # parte del cupo para los veleros con bitacora en curso (grilla fina); el resto, para los demas que navegan

def leer_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))

def dist(a, b):
    h = math.sin((b[0] - a[0]) * RAD / 2) ** 2 + math.cos(a[0] * RAD) * math.cos(b[0] * RAD) * math.sin((b[1] - a[1]) * RAD / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(min(1, h)))

def d_camino(C, la, lo):
    """Distancia (MN) de un punto al camino, en el plano local del punto (igual que las paginas)."""
    k = math.cos(la * RAD) * 60
    d = float('inf')
    for i, (cla, clo) in enumerate(C):
        x = clo - lo
        if x > 180: x -= 360
        elif x < -180: x += 360
        ax, ay = x * k, (cla - la) * 60
        if i == 0: d = min(d, math.hypot(ax, ay)); continue
        x0 = C[i - 1][1] - lo
        if x0 > 180: x0 -= 360
        elif x0 < -180: x0 += 360
        bx, by = x0 * k, (C[i - 1][0] - la) * 60
        ex, ey = ax - bx, ay - by
        L2 = ex * ex + ey * ey
        f = max(0, min(1, -(bx * ex + by * ey) / L2)) if L2 else 0
        d = min(d, math.hypot(bx + f * ex, by + f * ey))
    return d

# ─── los proximos destinos de cada velero ───
def caminos(veleros, destinos, wpact):
    """{velero: {'camino': [[lat, lon]...], 'ancho': MN, 'origen': 'ruta' | 'bitacora'}} de los veleros que navegan o
    tienen la bitacora en curso: posicion + WP activo y siguientes (o proximos puertos), hasta HORIZONTE_H de navegacion."""
    res = {}
    for nr in veleros:
        try: d = leer_json(SAILAWAY_RUTA % nr)
        except Exception as e: print('  %s: sin datos de Sailaway (%s)' % (nr, e)); continue
        b = (d.get('boats') or [{}])[0]
        if b.get('lat') is None: continue
        pos, spd = [float(b['lat']), float(b['lon'])], abs(float(b.get('spd') or 0))
        en_bitacora = str(nr) in destinos
        if spd < 0.3 and not en_bitacora: continue   # fondeado y sin bitacora en curso: no hace falta
        wps = [[float(w['lat']), float(w['lon'])] for w in (d.get('waypoints') or []) if w.get('lat') is not None]
        act = (wpact.get('veleros') or {}).get(str(nr)) or {}
        destinos_v, origen = [], None
        if len(wps) >= 2 and not act.get('sinRuta'):
            i = act.get('i')
            if i is None:   # sin dato del WP activo: el que sigue al tramo mas cercano
                i = 1 + min(range(len(wps) - 1), key=lambda k: d_camino([wps[k], wps[k + 1]], *pos))
            destinos_v, origen = wps[max(1, min(i, len(wps) - 1)):], 'ruta'
        if not destinos_v and en_bitacora: destinos_v, origen = destinos[str(nr)], 'bitacora'
        if not destinos_v: continue
        # hasta ~HORIZONTE_H horas de navegacion (a la velocidad de ahora, entre 6 y 12 kn); el ultimo tramo se recorta
        alcance = HORIZONTE_H * max(6, min(12, spd or 7))
        if en_bitacora: alcance = max(alcance, MIN_BITACORA)   # con bitacora en curso: que llegue al menos a su proximo destino
        camino, total = [pos], 0
        for p in destinos_v:
            d_t = dist(camino[-1], p)
            if total + d_t >= alcance:
                f, q = (alcance - total) / d_t, camino[-1]
                dl = p[1] - q[1]
                if dl > 180: dl -= 360
                elif dl < -180: dl += 360
                camino.append([q[0] + (p[0] - q[0]) * f, ((q[1] + dl * f + 540) % 360) - 180]); total = alcance; break
            total += d_t; camino.append(p)
        if total < 20: continue   # ya llego al final de su ruta
        ancho = max(80, min(ANCHO_MAX, 0.25 * total))   # (las paginas completan ~3° alrededor con el dato mas cercano)
        res['nr:%d' % nr] = {'camino': [[round(a, 3), round(o, 3)] for a, o in camino], 'ancho': round(ancho, 1), 'origen': origen,
                             'millas': round(total), 'prioridad': en_bitacora}
    return res

def celdas_camino(C, paso, lat_max=85):
    """Las celdas (i, j) de la grilla global de ese paso (lat = i*paso, lon = j*paso) que quedan cerca de algun camino."""
    celdas, n180, n360 = set(), round(180 / paso), round(360 / paso)
    for c in C.values():
        cam, ancho = c['camino'], c['ancho']
        m_lat = ancho / 60 + 0.3
        la_min, la_max = max(-lat_max, min(p[0] for p in cam) - m_lat), min(lat_max, max(p[0] for p in cam) + m_lat)
        m_lon = m_lat / max(0.2, math.cos(min(80, max(abs(la_min), abs(la_max))) * RAD))
        lons = [cam[0][1]]
        for p in cam[1:]:   # longitudes sin saltos (cruzando el antimeridiano)
            o = p[1]
            while o - lons[-1] > 180: o -= 360
            while o - lons[-1] < -180: o += 360
            lons.append(o)
        for i in range(math.floor(la_min / paso), math.ceil(la_max / paso) + 1):
            for j in range(math.floor((min(lons) - m_lon) / paso), math.ceil((max(lons) + m_lon) / paso) + 1):
                if d_camino(cam, i * paso, j * paso) <= ancho + paso * 45:
                    celdas.add((i, ((j + n180) % n360) - n180))   # j entre -180 y 180
    return celdas

def grilla_celdas(celdas, paso):
    """La grilla regular que contiene esas celdas, en el formato de las paginas (la0, lo0, nLa, nLo, idx) y sus puntos."""
    if not celdas: return None
    I0, I1 = min(i for i, _ in celdas), max(i for i, _ in celdas)
    J0, J1 = min(j for _, j in celdas), max(j for _, j in celdas)
    nLo, pts = J1 - J0 + 1, sorted(celdas)
    return {'la0': round(I0 * paso, 4), 'lo0': round(J0 * paso, 4), 'paso': paso, 'nLa': I1 - I0 + 1, 'nLo': nLo, 'celdas': pts,
            'idx': [(i - I0) * nLo + (j - J0) for i, j in pts], 'pts': [(round(i * paso, 4), round(j * paso, 4)) for i, j in pts]}

def grilla_flota(C, max_pts, paso_min, lat_max=85):
    """Grilla comun de un grupo de veleros con a lo sumo max_pts puntos, lo mas fina posible desde paso_min (para el mar)."""
    paso = paso_min
    while True:
        celdas = celdas_camino(C, paso, lat_max)
        if len(celdas) <= max_pts or paso > 5: return grilla_celdas(celdas, paso)
        paso = round(max(paso + 0.01, paso * math.sqrt(len(celdas) / max_pts) * 1.02), 4)

def cerca(C, f=0.5):
    """La parte cercana de cada camino: la primera mitad (al menos 300 MN), que es la que mas usan los ruteos."""
    res = {}
    for v, c in C.items():
        cam = c['camino']; tot = sum(dist(cam[k - 1], cam[k]) for k in range(1, len(cam))); lim = max(300, f * tot); out, acc = [cam[0]], 0
        for k in range(1, len(cam)):
            d_t = dist(cam[k - 1], cam[k])
            if acc + d_t >= lim:
                a, b, t = cam[k - 1], cam[k], (lim - acc) / d_t
                dl = b[1] - a[1]
                if dl > 180: dl -= 360
                elif dl < -180: dl += 360
                out.append([round(a[0] + (b[0] - a[0]) * t, 3), round(((a[1] + dl * t + 540) % 360) - 180, 3)]); break
            acc += d_t; out.append(cam[k])
        res[v] = dict(c, camino=out)
    return res

# ─── cupo de Open-Meteo ───
PEDIDOS = []
def cupo(puntos):
    while True:
        ahora = time.time()
        while PEDIDOS and ahora - PEDIDOS[0][0] > 62: PEDIDOS.pop(0)
        if not PEDIDOS or sum(p for _, p in PEDIDOS) + puntos <= 540:
            PEDIDOS.append((ahora, puntos)); return
        time.sleep(min(62 - (ahora - PEDIDOS[0][0]), 5))

def pedir(url, n):
    for intento in range(4):
        cupo(n)
        try:
            return leer_json(url)
        except urllib.error.HTTPError as e:
            if e.code != 429 or intento == 3: raise
            print('  Open-Meteo pidio esperar (429): sigo en un minuto'); PEDIDOS.append((time.time(), 540)); time.sleep(60)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:   # cortes de red pasajeros: se reintenta
            if intento == 3: raise
            print('  falla de red (%s): reintento en %d s' % (e, 20 * (intento + 1))); time.sleep(20 * (intento + 1))

def bajar(modelo, pts, fecha):
    """{t0, nh, escala, U, V}: por punto, nh valores int16 (hora por hora, desde `fecha` y DIAS dias) del vector hacia donde
    va (viento en decimas de nudo, corriente en centesimas de nudo, ola en centesimas de metro)."""
    api = MODELOS[modelo][0]
    fin = time.strftime('%Y-%m-%d', time.gmtime(calendar.timegm(time.strptime(fecha, '%Y-%m-%d')) + (DIAS - 1) * 86400))
    fechas = '&start_date=%s&end_date=%s' % (fecha, fin)
    if api == 'forecast':
        base = 'https://api.open-meteo.com/v1/forecast?hourly=wind_speed_10m,wind_direction_10m&wind_speed_unit=kn&timezone=GMT&models=%s' % modelo
        vs, vd, valor, hacia = 'wind_speed_10m', 'wind_direction_10m', (lambda x: x), (lambda d: d + 180)   # el viento viene de esa direccion
    elif modelo == 'corrientes':
        base = 'https://marine-api.open-meteo.com/v1/marine?hourly=ocean_current_velocity,ocean_current_direction&models=meteofrance_currents&timezone=GMT'
        vs, vd, valor, hacia = 'ocean_current_velocity', 'ocean_current_direction', (lambda x: x / 1.852), (lambda d: d)
    else:
        base = 'https://marine-api.open-meteo.com/v1/marine?hourly=wave_height,wave_direction&timezone=GMT'
        vs, vd, valor, hacia = 'wave_height', 'wave_direction', (lambda x: x), (lambda d: d + 180)
    res, T = [], 250
    for k in range(0, len(pts), T):
        t = pts[k:k + T]
        d = pedir(base + fechas + '&latitude=' + ','.join('%.4f' % q[0] for q in t) + '&longitude=' + ','.join('%.4f' % q[1] for q in t), len(t))
        res += d if isinstance(d, list) else [d]
    tt = res[0]['hourly']['time']
    t0 = calendar.timegm(time.strptime(tt[0], '%Y-%m-%dT%H:%M'))   # Open-Meteo con timezone=GMT
    escala = 10 if api == 'forecast' else 100
    U, V = [], []
    for q in res:
        h = q['hourly']; S, D = h.get(vs) or [], h.get(vd) or []
        u, v = array.array('h'), array.array('h')
        for k in range(len(tt)):
            x = S[k] if k < len(S) else None; dd = D[k] if k < len(D) else None
            if x is None or dd is None: u.append(-32768); v.append(-32768); continue
            x, a = valor(x), hacia(dd) * RAD
            u.append(max(-32767, min(32767, round(x * math.sin(a) * escala)))); v.append(max(-32767, min(32767, round(x * math.cos(a) * escala))))
        U.append(u); V.append(v)
    return {'t0': t0 * 1000, 'nh': len(tt), 'escala': escala, 'U': U, 'V': V}

# ─── archivos de la rama pronos (formato de las paginas: la grilla y, por punto, la serie hora por hora en int16) ───
b64 = lambda A: base64.b64encode(A.tobytes()).decode()   # int16 little-endian
def escribir(arch, modelo, grilla, datos, extra):
    """datos: {celda (i, j): (u, v)}; se ordenan como las celdas de la grilla."""
    U, V = array.array('h'), array.array('h')
    for c in grilla['celdas']: u, v = datos[c]; U.extend(u); V.extend(v)
    with open(os.path.join(SALIDA, arch), 'w', encoding='utf-8') as f:
        json.dump(dict(extra, modelo=modelo, t0=extra['t0'], dtMs=3600000, la0=grilla['la0'], lo0=grilla['lo0'], paso=grilla['paso'],
                       nLa=grilla['nLa'], nLo=grilla['nLo'], idx=grilla['idx'], u=b64(U), v=b64(V)), f, separators=(',', ':'))

def leer(arch):
    """{celda: (u, v)} y los datos generales de un archivo ya bajado (o None)."""
    try: d = json.load(open(os.path.join(SALIDA, arch), encoding='utf-8'))
    except Exception: return None
    U, V = array.array('h'), array.array('h'); U.frombytes(base64.b64decode(d['u'])); V.frombytes(base64.b64decode(d['v']))
    nh, paso, I0, J0 = d['nh'], d['paso'], round(d['la0'] / d['paso']), round(d['lo0'] / d['paso'])
    datos = {}
    for k, n in enumerate(d['idx']):
        datos[(I0 + n // d['nLo'], J0 + n % d['nLo'])] = (U[k * nh:(k + 1) * nh], V[k * nh:(k + 1) * nh])
    return d, datos

# ─── viento por capas ───
# Cada ciclo (una corrida nueva cada ~12 h) arranca con una base gruesa de toda la flota, y despues, mientras alcanza el
# cupo del dia, cada vez que corre la Action suma capas mas finas: primero la mitad cercana de los caminos de los
# veleros con bitacora en curso, despues el resto, despues los demas veleros, y asi hasta la resolucion del modelo.
# Las paginas usan todas a la vez: la mas fina donde hay y la mas gruesa donde no llega.
# Un ciclo por dia: con el cupo de Open-Meteo (9000 puntos), dos bases por dia se comian lo que hace falta para llegar a
# 0,25°. Lo mas fino va primero solo con ECMWF (el modelo principal); el promedio usa la capa mas fina con los tres.
CICLO_H = 24
SOLO_ECMWF = ['ecmwf_ifs025']
BASE = [('', 1.0), ('_flota', 2.0)]
REFINAR = [('', 0.5, 'cerca'), ('', 0.5, 'todo'), ('_flota', 1.0, 'cerca'), ('_flota', 1.0, 'todo'),
           ('', 0.25, 'cerca', SOLO_ECMWF), ('', 0.25, 'todo', SOLO_ECMWF), ('_flota', 0.5, 'cerca'), ('_flota', 0.5, 'todo'),
           ('', 0.25, 'cerca'), ('', 0.25, 'todo'), ('_flota', 0.25, 'cerca', SOLO_ECMWF), ('_flota', 0.25, 'todo', SOLO_ECMWF)]
RESERVA = 1300   # puntos del dia que se guardan para la base del proximo ciclo
MAR_PTS = {'': 300, '_flota': 150}   # el mar (corrientes, olas): una grilla por grupo, sin capas
nombre_capa = lambda m, gr, paso, suf='': 'v_%s%s_%s%s.json' % (m, gr, str(paso).replace('.', 'p'), suf)

def cubre(caminos_viejos, C):
    """Si la parte cercana (60%) de cada camino de ahora queda dentro de alguna franja de antes."""
    viejos = [(c['camino'], c['ancho']) for c in (caminos_viejos or {}).values()]
    if not viejos: return False
    for c in cerca(C, 0.6).values():
        cam, P = c['camino'], [c['camino'][0]]
        for k in range(1, len(cam)):
            a, b = cam[k - 1], cam[k]; n = max(1, int(dist(a, b) // 20))
            dl = b[1] - a[1]
            if dl > 180: dl -= 360
            elif dl < -180: dl += 360
            P += [[a[0] + (b[0] - a[0]) * t / n, a[1] + dl * t / n] for t in range(1, n + 1)]
        if not all(any(d_camino(cv, p[0], p[1]) <= ancho - 20 for cv, ancho in viejos) for p in P): return False
    return True

class Cupo:
    def __init__(self, uso):
        hoy = time.strftime('%Y-%m-%d', time.gmtime())
        self.uso = uso if (uso or {}).get('dia') == hoy else {'dia': hoy, 'puntos': 0}
    def queda(self, reserva=0): return CUPO_DIA - self.uso['puntos'] - reserva
    def gastar(self, n): self.uso['puntos'] += n

def capa(viento, C, gr, paso, tramo, meta, ciclo, cupo, reserva, probar, mods=None):
    """Baja (o completa) la capa de viento de ese grupo y paso hasta `tramo`; devuelve True si quedo hecha.
    mods: los modelos de la capa (por defecto los tres; una capa solo de ECMWF va aparte, con su propio nombre)."""
    mods = mods or VIENTO
    suf = '' if mods == VIENTO else '_' + '_'.join(m.split('_')[0] for m in mods)
    clave = '%s|%s%s' % (gr, paso, suf)
    Cg = {v: c for v, c in C.items() if c['prioridad'] == (gr == '')}
    if not Cg: return True
    Ct = cerca(Cg) if tramo == 'cerca' else Cg
    objetivo = celdas_camino(Ct, paso)
    e = viento['capas'].get(clave)
    del_ciclo = e and e.get('ciclo') == ciclo['inicio']
    if del_ciclo and (e['hecho'] == 'todo' or e['hecho'] == tramo): return True
    previos = {m: (leer(e['archivos'][m]) if del_ciclo else None) for m in mods}
    falta = {m: objetivo - (set(previos[m][1]) if previos[m] else set()) for m in mods}
    # lo que coincide con otra capa (mas gruesa o del mismo paso) del mismo ciclo y la misma corrida se copia (es el mismo punto)
    copia = {m: {} for m in mods}
    for clave_g, eg in viento['capas'].items():
        if clave_g == clave or eg.get('ciclo') != ciclo['inicio'] or eg['grupo'] != gr or eg['paso'] < paso: continue   # (las del mismo paso tambien: la solo ECMWF)
        k = round(eg['paso'] / paso)
        if abs(eg['paso'] / paso - k) > 1e-6: continue
        for m in mods:
            if m not in eg['archivos'] or eg['corridas'].get(m) != (meta.get(m) or {}).get('last_run_initialisation_time'): continue
            g = leer(eg['archivos'][m])
            if not g: continue
            for (i, j) in list(falta[m]):
                if i % k == 0 and j % k == 0 and (i // k, j // k) in g[1]: copia[m][(i, j)] = g[1][(i // k, j // k)]; falta[m].discard((i, j))
    costo = sum(len(f) for f in falta.values())
    print('  capa %s %.2f° (%s): %d puntos por modelo, %d se reusan de una capa mas gruesa, faltan bajar %d' % (gr or 'bitácora', paso, tramo, len(objetivo),
          len(copia[mods[0]]), costo))
    if costo > cupo.queda(reserva):
        print('    no alcanza el cupo del dia (quedan %d): sigue en la proxima corrida' % cupo.queda(reserva)); return False
    if probar: return False
    nuevos, fallo = {}, False
    for m in mods:
        pts = [(round(i * paso, 4), round(j * paso, 4)) for i, j in sorted(falta[m])]
        try: D = bajar(m, pts, ciclo['fecha']) if pts else None
        except Exception as err: print('    %s: no se pudo bajar (%s)' % (m, err)); fallo = True; continue
        if D: cupo.gastar(len(pts))
        datos = dict(previos[m][1]) if previos[m] else {}
        datos.update(copia[m])
        if D:
            for k, c in enumerate(sorted(falta[m])): datos[c] = (D['U'][k], D['V'][k])
        nuevos[m] = (datos, D)
    if fallo:
        # lo que si se bajo queda guardado (cada modelo con sus puntos): la proxima corrida baja solo lo que falta. La capa
        # sigue anunciando lo que cubria antes (si era del ciclo); el archivo de cada modelo ya tiene los puntos nuevos.
        if not del_ciclo:   # capa nueva: queda «en curso» (sin caminos: las paginas no la usan hasta completarla)
            e = viento['capas'][clave] = {'grupo': gr, 'paso': paso, 'hecho': None, 'caminos': {}, 'archivos': {m: nombre_capa(m, gr, paso, suf) for m in mods},
                                          'corridas': {m: (meta.get(m) or {}).get('last_run_initialisation_time') for m in mods}, 'pts': 0, 't': int(time.time()), 'ciclo': ciclo['inicio']}
        for m, (datos, _) in nuevos.items():
            if datos: escribir(e['archivos'][m], m, grilla_celdas(set(datos), paso), datos, {'t0': ciclo['t0'], 'nh': ciclo['nh'], 'escala': 10})
        return False
    # todas las del grupo quedan con la misma grilla (las paginas promedian los tres modelos punto a punto)
    todas = set.intersection(*[set(d) for d, _ in nuevos.values()])
    G = grilla_celdas(todas, paso)
    t0 = ciclo['t0']
    archivos, corridas = {}, {}
    for m in mods:
        arch = nombre_capa(m, gr, paso, suf)
        escribir(arch, m, G, nuevos[m][0], {'t0': t0, 'nh': ciclo['nh'], 'escala': 10})
        archivos[m] = arch; corridas[m] = (meta.get(m) or {}).get('last_run_initialisation_time')
    viento['capas'][clave] = {'grupo': gr, 'paso': paso, 'hecho': tramo, 'caminos': Ct, 'archivos': archivos, 'corridas': corridas,
                              'pts': len(G['pts']), 't': int(time.time()), 'ciclo': ciclo['inicio']}
    return True

def main():
    a = sys.argv[1:]
    forzar, probar = '--forzar' in a, '--probar' in a
    previo = {}
    if '--previo' in a:
        p = a[a.index('--previo') + 1]
        try: previo = json.load(open(os.path.join(p, 'index.json'), encoding='utf-8'))
        except Exception as e: print('  sin index anterior:', e)
    if previo.get('version', 0) < 3: previo = {}   # formato anterior: se arranca de cero
    veleros = json.load(open(os.path.join(DIR, 'veleros.json'), encoding='utf-8')) if os.path.exists(os.path.join(DIR, 'veleros.json')) else []
    destinos = json.load(open(os.path.join(DIR, 'destinos.json'), encoding='utf-8')) if os.path.exists(os.path.join(DIR, 'destinos.json')) else {}
    try: wpact = leer_json(WP_ACTIVO + '?t=%d' % time.time())
    except Exception as e: print('  sin WP activo:', e); wpact = {}
    os.makedirs(SALIDA, exist_ok=True)
    C = caminos(veleros, destinos, wpact)
    for v, c in C.items(): print('%s: %s, %d MN hacia adelante (franja de %d MN)%s' % (v, c['origen'], c['millas'], c['ancho'], ' · con bitácora' if c['prioridad'] else ''))
    meta = {}
    for m, (api, nombre, _) in MODELOS.items():
        host = 'api' if api == 'forecast' else 'marine-api'
        try: meta[m] = leer_json('https://%s.open-meteo.com/data/%s/static/meta.json' % (host, nombre))
        except Exception as e: print('  meta %s: %s' % (m, e))
    ahora = int(time.time())
    cupo = Cupo(previo.get('uso'))
    print('cupo de hoy: usados %d de %d' % (cupo.uso['puntos'], CUPO_DIA))
    viento = previo.get('viento') or {'ciclo': None, 'capas': {}}
    # ── ciclo de viento: arranca uno nuevo con la corrida nueva (cada ~12 h) o si los caminos cambiaron ──
    ciclo = viento.get('ciclo')
    base_caminos = {}
    for (gr, paso) in BASE:
        e = viento['capas'].get('%s|%s' % (gr, paso))
        if e and ciclo and e.get('ciclo') == ciclo['inicio']: base_caminos.update(e['caminos'])
    nueva = not ciclo or ciclo['corridas'].get('ecmwf_ifs025') != (meta.get('ecmwf_ifs025') or {}).get('last_run_initialisation_time')
    if forzar or not ciclo or (nueva and ahora - ciclo['inicio'] >= CICLO_H * 3600) or not cubre(base_caminos, C):
        hoy = time.strftime('%Y-%m-%d', time.gmtime())
        ciclo = {'inicio': ahora, 'fecha': hoy, 't0': calendar.timegm(time.strptime(hoy, '%Y-%m-%d')) * 1000, 'nh': DIAS * 24,
                 'corridas': {m: (meta.get(m) or {}).get('last_run_initialisation_time') for m in VIENTO}}
        print('ciclo nuevo de viento (corrida ECMWF %s)' % time.strftime('%d/%m %H UTC', time.gmtime(ciclo['corridas']['ecmwf_ifs025'] or 0)))
    viento['ciclo'] = ciclo
    bajo = False
    for (gr, paso) in BASE:   # la base primero, con todo el cupo que quede
        antes = cupo.uso['puntos']
        capa(viento, C, gr, paso, 'todo', meta, ciclo, cupo, 0, probar)
        bajo |= cupo.uso['puntos'] > antes
    # ── el mar: una grilla por grupo, cuando hay corrida nueva (corrientes ~1 por dia, olas ~2) ──
    mar = previo.get('mar') or {}
    for gr in ('', '_flota'):
        Cg = {v: c for v, c in C.items() if c['prioridad'] == (gr == '')}
        if not Cg: continue
        Gm = grilla_flota(Cg, MAR_PTS[gr], 1 / 12, 75)
        for m in MAR:
            e = mar.get(m + gr)
            disp = (meta.get(m) or {}).get('last_run_availability_time')
            if e and os.path.exists(os.path.join(SALIDA, e['archivo'])) and not forzar and cubre(e.get('caminos'), Cg) and \
               (e.get('disp') == disp or ahora - e.get('t', 0) < HORAS_ENTRE[m] * 3600): continue
            if len(Gm['pts']) > cupo.queda(RESERVA): print('  %s%s: no alcanza el cupo del dia' % (m, gr)); continue
            print('  %s%s: %d puntos cada %.3f°' % (m, gr, len(Gm['pts']), Gm['paso']))
            if probar: continue
            hoy = time.strftime('%Y-%m-%d', time.gmtime())
            try: D = bajar(m, Gm['pts'], hoy)
            except Exception as err: print('    no se pudo bajar:', err); continue
            cupo.gastar(len(Gm['pts'])); bajo = True
            arch = '%s%s.json' % (m, gr)
            escribir(arch, m, Gm, {c: (D['U'][k], D['V'][k]) for k, c in enumerate(Gm['celdas'])}, {'t0': D['t0'], 'nh': D['nh'], 'escala': D['escala']})
            mar[m + gr] = {'modelo': m, 'grupo': gr, 'archivo': arch, 'corrida': (meta.get(m) or {}).get('last_run_initialisation_time'), 'disp': disp,
                           'paso': Gm['paso'], 'pts': len(Gm['pts']), 't': ahora, 'caminos': Cg}
    # ── con lo que sobra del cupo, capas mas finas de viento, en orden ──
    for paso_def in REFINAR:
        gr, paso, tramo = paso_def[:3]
        antes = cupo.uso['puntos']
        hecha = capa(viento, C, gr, paso, tramo, meta, ciclo, cupo, RESERVA, probar, paso_def[3] if len(paso_def) > 3 else None)
        bajo |= cupo.uso['puntos'] > antes
        if not hecha: break
    if probar: print('(prueba: no se bajo nada)'); return 0
    # los archivos que ya no figuran se borran
    vivos = {x for e in viento['capas'].values() for x in e['archivos'].values()} | {e['archivo'] for e in mar.values()}
    borrados = 0
    for f in os.listdir(SALIDA):
        if f.endswith('.json') and f != 'index.json' and f not in vivos: os.remove(os.path.join(SALIDA, f)); borrados += 1
    # para las paginas: capas de viento (con los caminos que cubre cada una) y, para el mar, por modelo y velero
    capas = sorted([dict({k: x for k, x in e.items()}, t0=ciclo['t0'] if e.get('ciclo') == ciclo['inicio'] else None) for e in viento['capas'].values()],
                   key=lambda e: e['paso'])
    archivos = sorted([dict({k: x for k, x in e.items() if k != 'caminos'}, velero=v, camino=c['camino'], ancho=c['ancho'])
                       for e in mar.values() for v, c in (e.get('caminos') or {}).items()], key=lambda x: x['paso'])
    if os.environ.get('GITHUB_OUTPUT'):   # la Action sube la rama solo si cambio algo
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f: f.write('cambios=%d' % (int(bajo) + borrados) + chr(10))
    with open(os.path.join(SALIDA, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'t': ahora, 'version': 3, 'uso': cupo.uso, 'viento': viento, 'mar': mar, 'capas': capas, 'archivos': archivos, 'caminos': C},
                  f, ensure_ascii=False, separators=(',', ':'))
    print('listo: cupo de hoy %d de %d · capas de viento: %s' % (cupo.uso['puntos'], CUPO_DIA,
          ', '.join('%s %.2f° %s%s' % (e['grupo'] or 'bitácora', e['paso'], e['hecho'], '' if len(e['archivos']) == 3 else ' (solo ECMWF)') for e in capas)))
    return 1

if __name__ == '__main__':
    sys.exit(0 if main() >= 0 else 1)
