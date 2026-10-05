import os
import datetime
import calendar
import csv
import io
import secrets
import unicodedata
import re
import time
import base64
import binascii
import zipfile
import datetime as _dt
from collections import Counter
from typing import NamedTuple
from functools import wraps

import openpyxl
import PyPDF2
import psycopg2  # Reemplaza a sqlite3 para conectar con Neon.tech
from flask import Flask, render_template, request, session, redirect, url_for, flash, g, Response, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import RequestEntityTooLarge

# =================================================================
# INICIALIZACIÓN Y CIBERSEGURIDAD DEL BACKEND
# =================================================================
app = Flask(__name__)

# Gestión de Secretos: Llaves protegidas por variables de entorno para evitar vulnerabilidades[cite: 4]
app.secret_key = os.environ.get('SECRET_KEY', 'clave_desarrollo_local_segura')
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """Establece la conexión a la base de datos PostgreSQL en Neon.tech"""
    if not DATABASE_URL:
        raise ValueError("Error crítico: DATABASE_URL no está configurada.")
    return psycopg2.connect(DATABASE_URL)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# (Eliminada la variable DATABASE local porque el almacenamiento ahora está en la nube)

# =================================================================
# CONFIGURACIÓN DE LÍMITES (Ingesta de Datos y Archivos)
# =================================================================
MAX_BYTES = 10 * 1024 * 1024
MAX_B64 = int(MAX_BYTES * 1.40)
MAX_TEXTO = 1500000
MAX_LINEAS = 20000
MAX_MOVIMIENTOS = 5000
MAX_PAGINAS_PDF = 80
app.config['MAX_CONTENT_LENGTH'] = MAX_B64 + MAX_TEXTO + 64 * 1024

class ImportacionError(Exception):
    pass
# =================================================================
# INICIALIZACIÓN DE TABLAS SQL
# =================================================================
def init_db():
    """Inicializa la estructura de la base de datos PostgreSQL si no existe."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Tabla de Usuarios
        cur.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                es_nuevo_usuario BOOLEAN DEFAULT TRUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # Tabla de Movimientos (Optimizada para las consultas de agrupación y edición inline)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS movimientos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                fecha DATE NOT NULL,
                concepto VARCHAR(255) NOT NULL,
                importe NUMERIC(10, 2) NOT NULL,
                categoria VARCHAR(100) NOT NULL,
                es_ingreso BOOLEAN DEFAULT FALSE,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        conn.commit()
        cur.close()
        print("INFO: Tablas de PostgreSQL verificadas/inicializadas correctamente.")
    except Exception as e:
        print(f"ERROR CRÍTICO al inicializar la base de datos: {e}")
    finally:
        if conn is not None:
            conn.close()

# Disparador de inicialización al arrancar el servidor
if DATABASE_URL:
    init_db()
# =================================================================
# UTILIDADES DE BASE DE DATOS Y SEGURIDAD
# =================================================================
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def execute_db(query, args=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(query, args)
    db.commit()
    return cur.lastrowid

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT, email TEXT UNIQUE, password TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS movimientos (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, fecha TEXT, concepto TEXT, categoria TEXT, importe REAL, tipo TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS presupuestos (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, categoria TEXT, importe_mensual REAL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS metas (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, nombre TEXT, cantidad_objetivo REAL, cantidad_actual REAL DEFAULT 0)''')
        db.execute('''CREATE TABLE IF NOT EXISTS recurrentes (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, concepto TEXT, categoria TEXT, importe REAL, tipo TEXT, dia_mes INTEGER, ultimo_mes_procesado TEXT)''')
        db.commit()

init_db()

def format_currency(value):
    try: return f"{float(value):,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.')
    except (ValueError, TypeError): return "0,00 €"

app.jinja_env.filters['format_currency'] = format_currency

def generate_csrf_token():
    if 'csrf_token' not in session: session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

def validate_csrf(token):
    return token and token == session.get('csrf_token')

app.jinja_env.globals['csrf_token'] = generate_csrf_token

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'usuario_id' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# =================================================================
# ESCUDO ANTIMISILES V3: FRANCOTIRADOR DE FORMATOS
# =================================================================
def _parseador_francotirador(texto):
    """
    Caza el formato nativo: [CONCEPTO] [DD/MM/YYYY] [+/-IMPORTE€] [SALDO€]
    Ignora la paginación (Ej: 2/18) y entiende números sin separador de miles.
    """
    movs = []
    # Ignoramos paginación
    lineas = [l for l in texto.split('\n') if l.strip() and not re.fullmatch(r'\d+/\d+', l.strip())]

    # Regex blindada para el formato de tu banco
    patron = re.compile(r'^(.*?)\s+(\d{2}[/-]\d{2}[/-]\d{2,4})\s+([-+]?\d+(?:[.,]\d{3})*[.,]\d{2})\s*€?(?:\s+[-+]?\d+(?:[.,]\d{3})*[.,]\d{2}\s*€?)?\s*$', re.IGNORECASE)

    for linea in lineas:
        m = patron.search(linea.strip())
        if m:
            concepto = m.group(1).strip()
            fecha_str = m.group(2)
            importe_str = m.group(3)

            if '.' in importe_str and ',' in importe_str:
                importe_str = importe_str.replace('.', '').replace(',', '.')
            else:
                importe_str = importe_str.replace(',', '.')

            try:
                imp_val = float(importe_str)
                d, month, y = re.split(r'[/-]', fecha_str)
                if len(y) == 2: y = "20" + y
                fecha_obj = _dt.date(int(y), int(month), int(d))

                movs.append({
                    'fecha': fecha_obj,
                    'concepto': concepto[:120],
                    'importe': imp_val,
                    'categoria': ''
                })
            except Exception:
                continue
    return movs

# =================================================================
# CLAUDE'S ENGINE: RECONSTRUCTOR UNIVERSAL DE DATOS
# =================================================================
_GUIONES = '\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d'

def _normalizar(texto):
    t = unicodedata.normalize('NFKC', texto)
    if re.fullmatch(r'[\x20-\x7e\t\n]*', t): return t
    salida = []
    for ch in t:
        if ch == '\t' or ch == '\n': salida.append(ch)
        elif ch == '\r' or ch in '\x0b\x0c\x85': salida.append('\n')
        elif ch in _GUIONES: salida.append('-')
        else:
            cat = unicodedata.category(ch)
            if cat == 'Zs': salida.append(' ')
            elif cat in ('Zl', 'Zp'): salida.append('\n')
            elif cat[0] == 'C': continue
            else: salida.append(ch)
    return ''.join(salida)

def _sin_acentos(t): return unicodedata.normalize('NFKD', str(t)).encode('ascii', 'ignore').decode('ascii').lower().strip()

def _mkdate(a, m, d):
    try: return _dt.date(a, m, d)
    except ValueError: return None

def _resolver(d, m, a, hoy):
    if a is not None: return _mkdate(a, m, d)
    f = _mkdate(hoy.year, m, d)
    if f is not None and f > hoy + _dt.timedelta(days=3): f = _mkdate(hoy.year - 1, m, d)
    return f

_MESES = {'ene': 1, 'jan': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'ago': 8, 'aug': 8, 'sep': 9, 'set': 9, 'oct': 10, 'nov': 11, 'dic': 12, 'dec': 12}
_MES = (r'enero|ene|january|jan|febrero|february|feb|marzo|march|mar|abril|april|abr|apr|'
        r'mayo|may|junio|june|jun|julio|july|jul|agosto|august|ago|aug|'
        r'septiembre|setiembre|september|sept|sep|set|octubre|october|oct|'
        r'noviembre|november|nov|diciembre|december|dic|dec')
_FIN_ANIO = r'(?![\d:]|[.,]\d|%|\s*(?:€|\$|eur))'

_RE_HORA = re.compile(r'(?<![\d.,])\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?\s?m\.?)?(?:\s?h(?:rs?|s)?\b)?(?![\d])', re.I)
_RE_ISO = re.compile(r'(?<![\d.,/])(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?![\d])')
_RE_NUM = re.compile(r'(?<![\d.,/])(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?![\d])')
_RE_TXT = re.compile(r'(?<!\w)(\d{1,2})(?:\s*(?:de|del)\s+|[\s\-./]*)(' + _MES + r')\.?(?![a-záéíóúñ])'
                     r'(?:(?:\s*(?:de|del)\s*|[\s,\-./\']+)(\d{4}|\d{2}))?' + _FIN_ANIO, re.I)
_RE_TXT2 = re.compile(r'(?<!\w)(' + _MES + r')\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?' + _FIN_ANIO + r'(?![\w])', re.I)
_RE_DM = re.compile(r'(?<![\d.,/])(\d{2})/(\d{2})(?![\d/])')
_RE_REL = re.compile(r'^\W*(hoy|ayer|anteayer|antes\s+de\s+ayer|lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\W*$', re.I)
_DIAS = {'lunes': 0, 'martes': 1, 'miercoles': 2, 'jueves': 3, 'viernes': 4, 'sabado': 5, 'domingo': 6}

def _h_iso(m, hoy):
    a, mes, d = (int(x) for x in m.groups())
    f = _mkdate(a, mes, d) if 1990 <= a <= 2100 else None
    return (f, m.end()) if f else None

def _h_num(m, hoy):
    d, mes, a_txt = int(m.group(1)), int(m.group(2)), m.group(3)
    if mes > 12 >= d: d, mes = mes, d
    a = int(a_txt) + (2000 if len(a_txt) == 2 else 0)
    f = _mkdate(a, mes, d) if 1990 <= a <= 2100 else None
    return (f, m.end()) if f else None

def _h_dm(m, hoy):
    d, mes = int(m.group(1)), int(m.group(2))
    if mes > 12 >= d: d, mes = mes, d
    f = _resolver(d, mes, None, hoy)
    return (f, m.end()) if f else None

def _anio_texto(a_txt, hoy):
    if not a_txt: return None
    a = int(a_txt)
    if len(a_txt) == 2:
        a += 2000
        return a if hoy.year - 8 <= a <= hoy.year + 1 else None
    return a if 1990 <= a <= 2100 else None

def _h_txt(m, hoy):
    d, mes = int(m.group(1)), _MESES[m.group(2)[:3].lower()]
    a = _anio_texto(m.group(3), hoy)
    fin = m.end() if (a is not None or not m.group(3)) else m.end(2)
    f = _resolver(d, mes, a, hoy)
    return (f, fin) if f else None

def _h_txt2(m, hoy):
    mes, d = _MESES[m.group(1)[:3].lower()], int(m.group(2))
    a = _anio_texto(m.group(3), hoy)
    f = _resolver(d, mes, a, hoy)
    return (f, m.end()) if f else None

_PATRONES_FECHA = ((_RE_ISO, _h_iso), (_RE_NUM, _h_num), (_RE_TXT, _h_txt), (_RE_TXT2, _h_txt2), (_RE_DM, _h_dm))

def _buscar_fechas(texto, hoy):
    hallazgos, trabajo = [], texto
    for rx, handler in _PATRONES_FECHA:
        partes, ultimo = [], 0
        for m in rx.finditer(trabajo):
            r = handler(m, hoy)
            if r is None: continue
            f, fin = r
            hallazgos.append((m.start(), fin, f))
            partes.append(trabajo[ultimo:m.start()])
            partes.append(' ' * (fin - m.start()))
            ultimo = fin
        partes.append(trabajo[ultimo:])
        trabajo = ''.join(partes)
    hallazgos.sort(key=lambda x: x[0])
    return hallazgos, trabajo

def _extraer_fechas(texto, hoy):
    h, t = _buscar_fechas(texto, hoy)
    return [f for _, _, f in h], t

def _fecha_relativa(linea, hoy):
    m = _RE_REL.match(linea)
    if not m: return None
    w = _sin_acentos(m.group(1)).replace(' ', '')
    if w == 'hoy': return hoy
    if w == 'ayer': return hoy - _dt.timedelta(days=1)
    if w in ('anteayer', 'antesdeayer'): return hoy - _dt.timedelta(days=2)
    return hoy - _dt.timedelta(days=(hoy.weekday() - _DIAS[w]) % 7 or 7)

_NUM_PAT = (r'(?:\d{1,3}(?:\.\d{3})+,\d{1,2}|\d{1,3}(?:,\d{3})+\.\d{1,2}|\d+[.,]\d{1,2}|\d{1,3}(?:[.,]\d{3})+|\d+)')
_CUR = r'(?:€|\$|eur(?:os?)?\b|usd\b)'
_RE_IMPORTE = re.compile(r'(?<![\w.,])(?P<s1>[-+(])?\s*(?:(?P<c1>' + _CUR + r')\s*)?(?P<s2>[-+])?\s*(?P<num>' + _NUM_PAT + r')(?:\s*(?P<c2>' + _CUR + r'))?(?P<s3>-(?=\s|$)|\))?(?![\w%])', re.I)

def _numero(num, decimal=None):
    if '.' in num and ',' in num:
        dec = '.' if num.rfind('.') > num.rfind(',') else ','
        mil = ',' if dec == '.' else '.'
        num = num.replace(mil, '').replace(dec, '.')
    elif '.' in num or ',' in num:
        sep = '.' if '.' in num else ','
        partes = num.split(sep)
        if len(partes) > 2: es_decimal = False
        elif decimal in ('.', ','): es_decimal = (sep == decimal)
        else: es_decimal = len(partes[-1]) != 3
        num = partes[0] + '.' + partes[1] if es_decimal else ''.join(partes)
    return float(num)

def _interpretar_importe(m):
    num = m.group('num')
    con_divisa = bool(m.group('c1') or m.group('c2'))
    con_decimales = bool(re.search(r'[.,]\d{1,2}$', num))
    if not con_decimales and not con_divisa: return None
    try: valor = abs(_numero(num))
    except ValueError: return None
    s1, s2, s3 = m.group('s1'), m.group('s2'), m.group('s3')
    neg = '-' in (s1, s2) or s3 == '-' or (s1 == '(' and s3 == ')')
    pos = '+' in (s1, s2)
    return (round(valor, 2), neg, pos)

def _buscar_importes(texto):
    hallazgos = []
    def sub(m):
        r = _interpretar_importe(m)
        if r is None: return m.group(0)
        hallazgos.append((m.start(), m.end(), r))
        return ' ' * (m.end() - m.start())
    return hallazgos, _RE_IMPORTE.sub(sub, texto)

_RE_IBAN = re.compile(r'\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b')
_RE_PAGINA = re.compile(r'^\W*(p[aá]gina|p[aá]g\.?|page)\b', re.I)
_RE_DIA_INICIAL = re.compile(r'^\W*(?:(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|hoy|ayer)\b\W*)+', re.I)
_PALABRAS_CABECERA = {'fecha', 'valor', 'operacion', 'oper', 'concepto', 'importe', 'saldo', 'descripcion', 'detalle', 'detalles', 'movimiento', 'movimientos', 'categoria', 'divisa', 'moneda', 'cargo', 'abono', 'cargos', 'abonos', 'debe', 'haber', 'de', 'la', 'el', 'del', 'eur', 'euros', 'referencia', 'ref', 'n', 'no', 'tipo', 'estado', 'mas', 'recientes'}
_RE_PREFIJO_BASURA = re.compile(r'^(?:saldo|total|pagina|extracto|titular|iban|numero de cuenta|ver mas|ver todo|ver detalle|mostrar mas|cargar mas|ultimos movimientos|sin movimientos|no hay movimientos|descargar|exportar|filtrar|buscar|copyright|todos los derechos|actividad reciente|tu actividad)\b')
_RE_SALDO = re.compile(r'^(?:saldo|total|disponible)\b')
_RE_ETIQUETA = re.compile(r'^\W*(?:fecha(?:\s+(?:valor|de\s+operaci[oó]n|operaci[oó]n|contable))?|concepto|importe|cantidad|descripci[oó]n|detalle|movimiento|monto)\s*:\s*', re.I)

def _limpiar_concepto(t):
    t = _RE_IBAN.sub(' ', t)
    t = _RE_DIA_INICIAL.sub(' ', t)
    t = re.sub(r'[|·•►▪●*_=~<>]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' -:;,.·|/\\()[]')
    if len(re.findall(r'[^\W\d_]', t)) < 2: return ''
    return t

def _es_basura(texto):
    n = _sin_acentos(texto)
    if not n or '@' in n or 'http' in n or 'www.' in n: return True
    if _RE_PREFIJO_BASURA.match(n): return True
    palabras = re.findall(r'[a-z]+', n)
    return bool(palabras) and all(p in _PALABRAS_CABECERA for p in palabras)

def _unir_concepto(partes):
    vistos = []
    for p in partes:
        p = p.strip()
        if p and p.lower() not in [v.lower() for v in vistos]: vistos.append(p)
    return ' '.join(vistos)[:120]

_RE_KW_ING = re.compile(r'\b(?:nomin|salari|sueld|haber|pension|abon|ingres|devoluc|reembols|cashback|interes|dividend|premi|prestacion|recibid|paro\b)')
_RE_KW_GAS = re.compile(r'\b(?:compra|pago|recibo|cargo|retirad|reintegr|comision|adeudo|domicili|envi|suscrip|cuota|factura|tarjeta)')

def _resolver_tipos(registros, categorizador):
    hay_neg = any(r['neg'] for r in registros)
    hay_pos = any(r['pos'] and not r['neg'] for r in registros)
    salida = []
    for r in registros:
        n = _sin_acentos(r['concepto'])
        kw = bool(_RE_KW_ING.search(n)) and not _RE_KW_GAS.search(n)
        if r.get('kw'): ingreso = kw
        elif r['neg']: ingreso = False
        elif r['pos']: ingreso = True
        elif hay_neg: ingreso = True
        elif hay_pos: ingreso = False
        else: ingreso = kw

        importe_final = r['valor'] if ingreso else -r['valor']
        cat = r.get('categoria', '')
        if not cat and categorizador:
            try: cat = str(categorizador(r['concepto'], importe_final) or '')
            except Exception: cat = ''

        salida.append({'fecha': r['fecha'], 'concepto': r['concepto'], 'importe': importe_final, 'categoria': cat})
    return salida

_VOCAB_CAB = ('fecha', 'date', 'f.', 'concepto', 'descrip', 'detalle', 'movimiento', 'importe', 'cantidad', 'monto', 'amount', 'saldo', 'balance', 'cargo', 'abono', 'debe', 'haber', 'valor', 'categor', 'referencia', 'observ', 'operaci', 'divisa', 'euros', 'mas datos')
_ROL_SALDO = ('saldo', 'balance')
_ROL_IMPORTE = ('importe', 'cantidad', 'monto', 'amount', 'euros')
_ROL_DEBITO = ('cargo', 'debe', 'adeudo', 'debit', 'salida', 'gasto', 'pago', 'retirad')
_ROL_CREDITO = ('abono', 'haber', 'ingreso', 'credit', 'entrada', 'deposit', 'cobro')
_ROL_CONCEPTO = ('concepto', 'descrip', 'detalle', 'movimiento', 'mensaje')
_ROL_CONCEPTO2 = ('observ', 'mas datos', 'comentario', 'nota')
_ROL_REF = ('referencia', 'ref', 'numero', 'n.', 'num', 'id', 'codigo')
_PAL_DEB = {'cargo', 'cargos', 'debe', 'debito', 'adeudo', 'gasto', 'salida', 'debit', 'retirada', 'pago'}
_PAL_CRE = {'abono', 'abonos', 'haber', 'credito', 'ingreso', 'entrada', 'credit', 'deposito', 'cobro'}

def _tiene(nombre, palabras): return any(p in nombre for p in palabras)

def _celda_fecha(c, hoy):
    c = c.strip()
    if not c or len(c) > 40: return None
    rel = _fecha_relativa(c, hoy)
    if rel: return rel
    fechas, resto = _extraer_fechas(_RE_HORA.sub(' ', c), hoy)
    if not fechas: return None
    resto = _RE_DIA_INICIAL.sub(' ', resto)
    return fechas[0] if len(re.findall(r'[^\W_]', resto)) <= 2 else None

_RE_NUM_AGRUPADO = re.compile(r'\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?')
_RE_NUM_PLANO = re.compile(r'\d+(?:[.,]\d{1,2})?')

def _celda_importe(c, decimal=None):
    t = c.strip()
    if not t or len(t) > 26: return None
    t = re.sub(r'€|\$|\beur(?:os?)?\b|\busd\b', '', t, flags=re.I).replace(' ', '')
    if not t: return None
    neg = pos = False
    if t.startswith('(') and t.endswith(')'): neg, t = True, t[1:-1]
    if t.endswith('-'): neg, t = True, t[:-1]
    if t.startswith('-'): neg, t = True, t[1:]
    elif t.startswith('+'): pos, t = True, t[1:]
    if not (_RE_NUM_AGRUPADO.fullmatch(t) or _RE_NUM_PLANO.fullmatch(t)): return None
    try: valor = _numero(t, decimal)
    except ValueError: return None
    return (round(abs(valor), 2), neg, pos, bool(re.search(r'[.,]\d{1,2}$', t)))

def _es_cabecera(fila, hoy):
    n = 0
    for c in fila:
        c = c.strip()
        if c and len(c) <= 40 and _tiene(_sin_acentos(c), _VOCAB_CAB) and _celda_fecha(c, hoy) is None and _celda_importe(c) is None: n += 1
    return n >= 2

def _desquotar(c):
    c = c.strip()
    if len(c) >= 2 and c[0] == '"' and c[-1] == '"': c = c[1:-1].replace('""', '"').strip()
    return c

def _dividir(linea, delim):
    if delim == '\t': celdas = linea.split('\t')
    else:
        try: celdas = next(csv.reader([linea], delimiter=delim))
        except (csv.Error, StopIteration): celdas = linea.split(delim)
    return [_desquotar(c) for c in celdas]

def _pista_decimal(celdas):
    coma = punto = 0
    for c in celdas:
        c = c.strip()
        if re.fullmatch(r'[\s€$+\-(]*\d[\d.,]*[\s€)\-]*(?:eur)?', c, re.I):
            if re.search(r'\d,\d{1,2}\D*$', c): coma += 1
            elif re.search(r'\d\.\d{1,2}\D*$', c): punto += 1
    if coma == 0 and punto == 0: return None
    return ',' if coma >= punto else '.'

def _consistencia(a, b):
    asc = desc = pares = 0
    for i in range(len(a) - 1):
        if b[i] is None or b[i + 1] is None: continue
        d = abs((-b[i + 1][0] if b[i + 1][1] else b[i + 1][0]) - (-b[i][0] if b[i][1] else b[i][0]))
        pares += 1
        if a[i + 1] is not None and abs(d - a[i + 1][0]) < 0.011: asc += 1
        if a[i] is not None and abs(d - a[i][0]) < 0.011: desc += 1
    if pares < 2: return 0.0, 'asc'
    return max(asc, desc) / pares, ('asc' if asc >= desc else 'desc')

def _perfilar(datos, cab, hoy):
    n = len(datos)
    perfiles = []
    for j in range(len(datos[0])):
        celdas = [f[j].strip() for f in datos]
        dec = _pista_decimal(celdas)
        fechas, vals = [None] * n, [None] * n
        nf = nn = nt = ndec = 0
        long_t, textos = 0, []
        for r, c in enumerate(celdas):
            if not c: continue
            f = _celda_fecha(c, hoy)
            if f:
                fechas[r], nf = f, nf + 1
                continue
            im = _celda_importe(c, dec)
            if im:
                vals[r], nn, ndec = im, nn + 1, ndec + (1 if im[3] else 0)
                continue
            if len(re.findall(r'[^\W\d_]', c)) >= 2:
                nt, long_t = nt + 1, long_t + len(c)
                textos.append(c.lower())
        nne = sum(1 for c in celdas if c)
        perfiles.append({
            'j': j, 'nombre': _sin_acentos(cab[j]) if j < len(cab) else '', 'nne': nne, 'lleno': nne / n if n else 0,
            'f_fecha': nf / nne if nne else 0, 'f_num': nn / nne if nne else 0, 'f_dec': ndec / nn if nn else 0, 'f_texto': nt / nne if nne else 0,
            'long': long_t / nt if nt else 0, 'fechas': fechas, 'vals': vals, 'constante': n >= 4 and len(set(textos)) <= 1})
    return perfiles

def _inferir_tabla(filas, hoy, info):
    idx_cab = None
    for i, f in enumerate(filas[:40]):
        if _es_cabecera(f, hoy):
            idx_cab = i; break
    cab = filas[idx_cab] if idx_cab is not None else []
    datos = filas[idx_cab + 1:] if idx_cab is not None else filas
    datos = [f for f in datos if sum(1 for c in f if c.strip()) >= 2][:MAX_LINEAS]
    datos = [f for f in datos if not _es_cabecera(f, hoy) and not _RE_PREFIJO_BASURA.match(_sin_acentos(next(c for c in f if c.strip())))]
    if not datos: return None
    ancho = min(max(len(f) for f in datos), 40)
    datos = [(f + [''] * ancho)[:ancho] for f in datos]
    n = len(datos)
    perf = _perfilar(datos, cab, hoy)

    cand_f = [p for p in perf if p['nne'] and p['f_fecha'] >= 0.6 and p['lleno'] >= 0.5]
    if not cand_f: return None
    cand_f.sort(key=lambda p: (_tiene(p['nombre'], ('valor',)) and not _tiene(p['nombre'], ('operaci',)), p['j']))
    col_f, col_f2 = cand_f[0], (cand_f[1] if len(cand_f) > 1 else None)

    def rol_num(p): return 'ref' if _tiene(p['nombre'], _ROL_REF) and not _tiene(p['nombre'], _ROL_IMPORTE) else ''
    cand_n = [p for p in perf if p not in cand_f and p['nne'] and p['f_num'] >= 0.8 and p['lleno'] >= 0.25 and rol_num(p) != 'ref']
    if any(p['f_dec'] >= 0.5 for p in cand_n):
        cand_n = [p for p in cand_n if p['f_dec'] >= 0.5 or _tiene(p['nombre'], _ROL_IMPORTE + _ROL_DEBITO + _ROL_CREDITO)]
    if not cand_n: return None

    par_dc, imp, saldo, orient, ratio = None, None, None, 'asc', 0.0
    for a in cand_n:
        for b in cand_n:
            if a['j'] >= b['j']: continue
            ambas = sum(1 for r in range(n) if a['vals'][r] and b['vals'][r])
            union = sum(1 for r in range(n) if a['vals'][r] or b['vals'][r])
            if union >= 0.8 * n and ambas <= 0.05 * union and a['lleno'] <= 0.9 and b['lleno'] <= 0.9:
                if not par_dc or union > par_dc[2]: par_dc = (a, b, union)
    if par_dc:
        a, b = par_dc[0], par_dc[1]
        if _tiene(a['nombre'], _ROL_CREDITO) and _tiene(b['nombre'], _ROL_DEBITO): a, b = b, a
        par_dc = (a, b)
        otros = [p for p in cand_n if p is not a and p is not b]
        saldo = otros[0] if otros else None
    elif len(cand_n) == 1: imp = cand_n[0]
    else:
        mejor = None
        for a in cand_n:
            for b in cand_n:
                if a is not b:
                    r, o = _consistencia(a['vals'], b['vals'])
                    if mejor is None or r > mejor[0]: mejor = (r, o, a, b)
        if mejor and mejor[0] >= 0.6: ratio, orient, imp, saldo = mejor
        else:
            por_nombre = [p for p in cand_n if _tiene(p['nombre'], _ROL_SALDO)]
            saldo = por_nombre[0] if por_nombre else None
            resto = [p for p in cand_n if p is not saldo]
            imp = next((p for p in resto if _tiene(p['nombre'], _ROL_IMPORTE)), None) or next((p for p in resto if any(v and v[1] for v in p['vals'])), None) or resto[0]
            if saldo is None and len(resto) > 1: saldo = next((p for p in resto if p is not imp), None)

    usadas = {p['j'] for p in cand_f} | {p['j'] for p in cand_n}
    cand_t = [p for p in perf if p['j'] not in usadas and p['nne'] and p['f_texto'] >= 0.5]
    col_cat = next((p for p in cand_t if _tiene(p['nombre'], ('categor',))), None)

    def es_tipo(p):
        v = [_sin_acentos(f[p['j']]) for f in datos if f[p['j']].strip()]
        return len(v) >= 2 and sum(1 for x in v if x in _PAL_DEB or x in _PAL_CRE) >= 0.8 * len(v)
    col_tipo = next((p for p in cand_t if p is not col_cat and es_tipo(p)), None)
    cand_t = [p for p in cand_t if p is not col_cat and p is not col_tipo and (not p['constante'] or len(cand_t) == 1)]

    def rango(p):
        if _tiene(p['nombre'], _ROL_CONCEPTO): return (0, -p['long'])
        if _tiene(p['nombre'], _ROL_CONCEPTO2): return (1, -p['long'])
        return (2, -p['long'])
    cand_t.sort(key=rango)
    if cand_t and rango(cand_t[0])[0] <= 1: conceptos = [p for p in cand_t if rango(p)[0] <= 1][:2]
    else: conceptos = [p for i, p in enumerate(cand_t) if i == 0 or p['long'] >= 5][:2]
    conceptos.sort(key=lambda p: p['j'])

    signos = [None] * n
    if imp and saldo and ratio >= 0.8 and not any(v and (v[1] or v[2]) for v in imp['vals']):
        sb = [(-v[0] if v[1] else v[0]) if v else None for v in saldo['vals']]
        for r in range(n):
            vecino = r - 1 if orient == 'asc' else r + 1
            if imp['vals'][r] and 0 <= vecino < n and sb[r] is not None and sb[vecino] is not None:
                delta = sb[r] - sb[vecino]
                if abs(abs(delta) - imp['vals'][r][0]) < 0.011 and delta != 0: signos[r] = 'neg' if delta < 0 else 'pos'

    regs, descartadas = [], 0
    for r in range(n):
        f = col_f['fechas'][r] or (col_f2['fechas'][r] if col_f2 else None)
        if f is None:
            descartadas += 1; continue
        kw = False
        if par_dc:
            d, c = par_dc[0]['vals'][r], par_dc[1]['vals'][r]
            if d and d[0]: valor, neg, pos = d[0], True, False
            elif c and c[0]: valor, neg, pos = c[0], False, True
            else: descartadas += 1; continue
        else:
            t = imp['vals'][r]
            if t is None or t[0] == 0: descartadas += 1; continue
            valor, neg, pos = t[0], t[1], t[2]
            if signos[r]: neg, pos = signos[r] == 'neg', signos[r] == 'pos'
            elif imp and saldo and ratio >= 0.8 and not neg and not pos and not any(v and (v[1] or v[2]) for v in imp['vals']): kw = True
        if col_tipo and not par_dc:
            w = _sin_acentos(datos[r][col_tipo['j']])
            if w in _PAL_DEB: neg, pos, kw = True, False, False
            elif w in _PAL_CRE: neg, pos, kw = False, True, False
        partes = [datos[r][p['j']].strip() for p in conceptos]
        concepto = _unir_concepto([re.sub(r'\s+', ' ', x) for x in partes if x]) or 'Movimiento bancario'
        if _RE_PREFIJO_BASURA.match(_sin_acentos(concepto)):
            descartadas += 1; continue
        regs.append({'fecha': f, 'concepto': concepto, 'valor': valor, 'neg': neg, 'pos': pos, 'kw': kw, 'categoria': datos[r][col_cat['j']].strip() if col_cat else ''})

    if not regs or len(regs) < 0.5 * n: return None
    info['descartadas'] += descartadas
    return regs

def _intentar_tabla(lineas, hoy, info):
    muestra = lineas[:300]
    for delim, min_cols, nombre in (('\t', 2, 'tab'), (';', 2, ';'), ('|', 2, '|'), (',', 3, ',')):
        con = [l for l in muestra if l.count(delim) >= min_cols - 1]
        if len(con) < 2 or len(con) < 0.4 * len(muestra): continue
        regs = _inferir_tabla([_dividir(l, delim) for l in lineas], hoy, info)
        if regs:
            info['delimitador'] = nombre
            return regs
    return None

class _L(NamedTuple):
    fecha: object; importes: list; texto: str; basura: bool; saldo: bool

def _clasificar(cruda, hoy):
    linea = cruda.replace('\t', ' ').strip()[:400]
    if not linea or re.fullmatch(r'[\W_]+', linea) or _RE_PAGINA.match(linea): return None
    rel = _fecha_relativa(linea, hoy)
    if rel: return _L(rel, [], '', False, False)
    linea = _RE_ETIQUETA.sub('', linea)
    trabajo = _RE_HORA.sub(lambda m: ' ' * len(m.group(0)), linea)
    fechas, trabajo = _extraer_fechas(trabajo, hoy)
    importes, trabajo = _buscar_importes(trabajo)
    importes = [i[2] for i in importes]
    texto = _limpiar_concepto(trabajo)
    if texto and _es_basura(texto):
        if _RE_PREFIJO_BASURA.match(_sin_acentos(texto)): return _L(None, [], '', True, not importes and bool(_RE_SALDO.match(_sin_acentos(texto))))
        texto = ''
    if not fechas and not importes and not texto: return None
    return _L(fechas[0] if fechas else None, importes, texto, False, False)

def _inferir_modo(lineas):
    votos_c, votos_a, espera, peso, tras_saldo = 0.0, 0.0, True, 0.5, False
    for ln in lineas:
        if ln.basura: tras_saldo = ln.saldo; continue
        if tras_saldo and ln.importes and not ln.texto: tras_saldo = False; continue
        tras_saldo = False
        if ln.fecha and not ln.importes and not ln.texto: espera, peso = True, 1.0; continue
        if espera:
            if ln.importes and not ln.texto: votos_a += peso
            elif ln.texto and not ln.importes: votos_c += peso
            espera = False
    return 'A' if votos_a > votos_c else 'C'

def _recorrer(lineas, modo):
    registros, stats = [], {'descartadas': 0}
    estado = {'fecha': None, 'textos': [], 'imp': None, 'saldo': False}
    def emitir(partes, imp):
        concepto = _unir_concepto(partes)
        if not concepto:
            stats['descartadas'] += 1; return
        registros.append({'fecha': estado['fecha'], 'concepto': concepto, 'valor': imp[0], 'neg': imp[1], 'pos': imp[2]})
    def cerrar():
        if modo == 'A' and estado['imp'] is not None:
            if estado['textos']: emitir(estado['textos'], estado['imp'])
            else: stats['descartadas'] += 1
        estado['textos'], estado['imp'] = [], None

    for ln in lineas:
        if ln.basura:
            if ln.saldo: estado['saldo'] = True
            continue
        if ln.fecha:
            cerrar()
            estado['fecha'] = ln.fecha
            estado['saldo'] = False
        if ln.importes:
            imp = ln.importes[0]
            if estado['saldo'] and not ln.texto:
                estado['saldo'] = False; continue
            estado['saldo'] = False
            if ln.texto:
                if modo == 'A': cerrar(); emitir([ln.texto], imp)
                else: emitir(estado['textos'] + [ln.texto], imp); estado['textos'] = []
            elif modo == 'C':
                if estado['textos']: emitir(estado['textos'], imp); estado['textos'] = []
                else: stats['descartadas'] += 1
            else:
                if estado['imp'] is None: estado['imp'], estado['textos'] = imp, []
                elif estado['textos']: emitir(estado['textos'], estado['imp']); estado['imp'], estado['textos'] = imp, []
        elif ln.texto:
            estado['saldo'] = False
            if modo == 'C': estado['textos'] = (estado['textos'] + [ln.texto])[-3:]
            elif estado['imp'] is not None: estado['textos'].append(ln.texto)
    cerrar()
    return registros, stats

def _estrategia_lineas(lineas, hoy):
    lins = [l for l in (_clasificar(c, hoy) for c in lineas) if l]
    n_fechas = sum(1 for l in lins if l.fecha and not l.basura)
    registros, stats = _recorrer(lins, _inferir_modo(lins))
    return registros, n_fechas, stats

def _estrategia_difusa(lineas, hoy):
    utiles = []
    for l in lineas:
        s = l.strip()
        c = _clasificar(l, hoy)
        if c is not None and c.basura: continue
        if _RE_PAGINA.match(s) or (re.fullmatch(r'[\W_]+', s) and s not in ('-', '+', '(', ')', '€')): continue
        rel = _fecha_relativa(s, hoy)
        utiles.append(rel.isoformat() if rel else _RE_ETIQUETA.sub('', l.replace('\t', ' ')))
    plano = re.sub(r'\s+', ' ', ' '.join(utiles))
    plano = re.sub(r'(?<=\d)([/.,\-:])\s+(?=\d)', r'\1', plano)
    plano = _RE_HORA.sub(lambda m: ' ' * len(m.group(0)), plano)
    fechas, tras_f = _buscar_fechas(plano, hoy)
    imps, mask = _buscar_importes(tras_f)

    segmentos = [(0, fechas[0][0] if fechas else len(plano), None)]
    for k, (s, e, f) in enumerate(fechas): segmentos.append((e, fechas[k + 1][0] if k + 1 < len(fechas) else len(plano), f))
    def con_contenido(ini, fin): return any(ini <= s < fin for s, _, _ in imps) or bool(_limpiar_concepto(mask[ini:fin]))

    efectivos, pendiente = [], None
    for ini, fin, f in segmentos:
        if f is not None and not con_contenido(ini, fin):
            pendiente = pendiente or f; continue
        if f is not None and pendiente is not None and abs((f - pendiente).days) <= 7: f = pendiente
        pendiente = None
        efectivos.append((ini, fin, f))

    trabajos = []
    for ini, fin, f in efectivos:
        grupos = []
        for s, e, imp in imps:
            if not (ini <= s < fin): continue
            if grupos and not _limpiar_concepto(mask[grupos[-1][1]:s]): grupos[-1][1] = e
            else: grupos.append([s, e, imp])
        piezas = []
        for g, (s, e, imp) in enumerate(grupos):
            ant = grupos[g - 1][1] if g else ini
            sig = grupos[g + 1][0] if g + 1 < len(grupos) else fin
            piezas.append((_limpiar_concepto(mask[ant:s]), _limpiar_concepto(mask[e:sig]), imp))
        if piezas: trabajos.append((f, piezas))

    votos_c = sum(1 for _, p in trabajos if p[0][0])
    votos_a = sum(1 for _, p in trabajos if not p[0][0] and p[0][1])
    modo = 'A' if votos_a > votos_c else 'C'

    registros, descartadas = [], 0
    for f, piezas in trabajos:
        for antes, despues, imp in piezas:
            concepto = antes if modo == 'C' else despues
            if not concepto and len(piezas) == 1: concepto = despues or antes
            if not concepto or _es_basura(concepto):
                descartadas += 1; continue
            registros.append({'fecha': f, 'concepto': concepto[:120], 'valor': imp[0], 'neg': imp[1], 'pos': imp[2]})
    return registros, descartadas

def reconstruir_movimientos_detallado(texto, categorizador=None, hoy=None):
    info = {'modo': 'ninguno', 'descartadas': 0, 'sin_fecha': 0, 'delimitador': None, 'cabecera': None, 'columnas': None, 'advertencias': []}
    try:
        if not isinstance(texto, str) or not texto.strip(): return [], info
        hoy = hoy or _dt.date.today()
        texto = _normalizar(texto[:MAX_CHARS])
        lineas = [l for l in texto.split('\n') if l.strip()][:MAX_LINEAS]
        if not lineas: return [], info

        registros = None
        try:
            registros = _intentar_tabla(lineas, hoy, info)
            if registros: info['modo'] = 'tabla'
        except Exception: registros = None

        if not registros:
            reg_l, n_fechas, st = [], 0, {'descartadas': 0}
            try: reg_l, n_fechas, st = _estrategia_lineas(lineas, hoy)
            except Exception: pass
            registros, info['descartadas'] = reg_l, st['descartadas']
            if reg_l: info['modo'] = 'lineas'
            con_fecha_l = sum(1 for r in reg_l if r['fecha'] is not None)
            dudoso = (not reg_l or con_fecha_l < len(reg_l) or (n_fechas >= 4 and len(reg_l) < 0.6 * n_fechas))
            if dudoso:
                try:
                    reg_f, desc_f = _estrategia_difusa(lineas, hoy)
                    con_fecha_f = sum(1 for r in reg_f if r['fecha'] is not None)
                    if (con_fecha_f, len(reg_f)) > (con_fecha_l, len(reg_l)):
                        registros, info['descartadas'], info['modo'] = reg_f, desc_f, 'difuso'
                except Exception: pass

        if not registros: return [], info
        salida = []
        for m in _resolver_tipos(registros[:MAX_MOVIMIENTOS], categorizador):
            f = m['fecha']
            if f is None: f, info['sin_fecha'] = hoy, info['sin_fecha'] + 1
            salida.append({'fecha': f, 'concepto': m['concepto'], 'importe': round(m['importe'], 2), 'categoria': m.get('categoria', '')})
        return salida, info
    except Exception as e:
        app.logger.exception('Fallo en Reconstructor')
        info['modo'] = 'error'
        return [], info

def normalizar_texto(texto):
    if not texto: return ""
    return unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8').lower().strip()

def categorizar_movimiento(concepto, importe_val):
    concepto_low = normalizar_texto(concepto)
    if importe_val > 0:
        if any(kw in concepto_low for kw in ['nomina', 'salario', 'sueldo', 'haberes', 'pension', 'paro']): return 'Nómina y Salario'
        if any(kw in concepto_low for kw in ['bizum', 'transferencia', 'traspaso']): return 'Transferencias'
        return 'Otros Ingresos'
    mapa_categorias = {
        'Alimentación': ['mercadona', 'carrefour', 'lidl', 'dia', 'aldi', 'consum', 'eroski', 'supermercado'],
        'Restaurantes y Comida': ['restaurante', 'mcdonalds', 'burger', 'glovo', 'just eat', 'uber eats', 'telepizza', 'sushi'],
        'Ocio y Tiempo Libre': ['cine', 'bar', 'discoteca', 'concierto', 'teatro', 'pub', 'cerveza'],
        'Suscripciones Digitales': ['netflix', 'spotify', 'hbo', 'disney', 'amazon prime', 'youtube', 'dazn', 'apple'],
        'Servicios del Hogar': ['luz', 'agua', 'gas', 'internet', 'iberdrola', 'endesa', 'naturgy', 'vodafone', 'movistar', 'factura'],
        'Vivienda': ['alquiler', 'hipoteca', 'comunidad', 'ikea', 'inmobiliaria', 'fianza'],
        'Transporte Público': ['metro', 'autobus', 'renfe', 'tren', 'taxi', 'uber', 'cabify', 'ave'],
        'Vehículo y Gasolina': ['gasolinera', 'repsol', 'cepsa', 'bp', 'galp', 'taller', 'seguro coche', 'parking', 'gasolina'],
        'Compras y Ropa': ['zara', 'amazon', 'el corte ingles', 'shein', 'aliexpress', 'zalando', 'mango', 'primark', 'ropa'],
        'Salud y Bienestar': ['farmacia', 'dentista', 'medico', 'fisio', 'hospital', 'gimnasio'],
        'Viajes y Vacaciones': ['vuelo', 'hotel', 'ryanair', 'booking', 'airbnb', 'vueling', 'viaje']
    }
    for categoria, keywords in mapa_categorias.items():
        if any(normalizar_texto(kw) in concepto_low for kw in keywords): return categoria
    return 'Gastos Generales'

# =================================================================
# LECTORES DE ARCHIVOS -> CLAUDE'S ENGINE
# =================================================================
def _a_texto(v):
    if v is None: return ''
    if isinstance(v, _dt.datetime) or isinstance(v, _dt.date): return v.strftime('%d/%m/%Y')
    return str(v).strip()

def _tablas_xlsx(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(i.file_size for i in z.infolist()) > 150 * 1024 * 1024: raise ImportacionError('Excel demasiado grande.')
    except zipfile.BadZipFile: raise ImportacionError('Archivo dañado.')
    import openpyxl
    try: wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception: raise ImportacionError('El archivo no es un Excel válido.')
    tablas = []
    try:
        for hoja in wb.worksheets[:5]:
            filas = []
            for fila in hoja.iter_rows(values_only=True):
                filas.append(list(fila))
                if len(filas) >= MAX_LINEAS: break
            tablas.append(filas)
    finally: wb.close()
    return tablas

def _texto_pdf(raw):
    try: from pypdf import PdfReader
    except ImportError: from PyPDF2 import PdfReader
    lector = PdfReader(io.BytesIO(raw))
    if getattr(lector, 'is_encrypted', False):
        try: ok = lector.decrypt('')
        except Exception: ok = 0
        if not ok: raise ImportacionError('PDF protegido con contraseña.')
    partes = []
    for i in range(min(len(lector.pages), MAX_PAGINAS_PDF)):
        try: partes.append(lector.pages[i].extract_text() or '')
        except Exception: continue
    return '\n'.join(partes)

def _detectar_tipo(raw):
    if raw[:4] == b'%PDF': return 'pdf'
    if raw[:4] == b'PK\x03\x04': return 'xlsx'
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'): return 'texto16'
    if b'\x00' in raw[:2048]: return None
    return 'texto'

def procesar_importacion(usuario_id, raw=None, texto=''):
    hoy = _dt.date.today()
    movs, info = [], {}

    # 1. Si es archivo físico
    if raw:
        tipo = _detectar_tipo(raw)
        if tipo is None: raise ImportacionError('Formato no reconocido. Sube un CSV, Excel (.xlsx) o PDF.')
        if tipo == 'pdf':
            texto_extraido = _texto_pdf(raw)
            if not texto_extraido.strip(): raise ImportacionError('El PDF parece una imagen escaneada.')
            movs, info = reconstruir_movimientos_detallado(texto_extraido, categorizador=categorizar_movimiento, hoy=hoy)
        elif tipo == 'xlsx':
            tablas = _tablas_xlsx(raw)
            lineas = []
            for tabla in tablas:
                for fila in tabla: lineas.append('\t'.join(_a_texto(c) for c in fila))
            texto_extraido = '\n'.join(lineas)
            movs, info = reconstruir_movimientos_detallado(texto_extraido, categorizador=categorizar_movimiento, hoy=hoy)
        else:
            if tipo == 'texto16': texto_extraido = raw.decode('utf-16', errors='replace')
            else:
                try: texto_extraido = raw.decode('utf-8-sig')
                except UnicodeDecodeError: texto_extraido = raw.decode('cp1252', errors='replace')
            movs, info = reconstruir_movimientos_detallado(texto_extraido, categorizador=categorizar_movimiento, hoy=hoy)

    # 2. Si es texto pegado (caja de la web)
    if not movs and texto.strip():
        # ESCUDO V3: Francotirador que ataca directamente al formato exacto de tu banco
        movs = _parseador_francotirador(texto)
        if not movs:
            # Si pegan otra cosa que no sea ese formato, entra el motor de Claude
            movs, info = reconstruir_movimientos_detallado(texto[:MAX_TEXTO], categorizador=categorizar_movimiento, hoy=hoy)

    if not movs: raise ImportacionError('No pudimos detectar operaciones. Intenta copiar la tabla completa o usa otro formato.')

    # 3. Guardar en Base de Datos
    f_min, f_max = min(m['fecha'].strftime('%Y-%m-%d') for m in movs), max(m['fecha'].strftime('%Y-%m-%d') for m in movs)
    existentes = query_db("SELECT fecha, concepto, importe, tipo FROM movimientos WHERE usuario_id = ? AND fecha BETWEEN ? AND ?", (usuario_id, f_min, f_max))
    cuenta = Counter((r['fecha'], (r['concepto'] or '').strip().lower(), round(r['importe'], 2), r['tipo']) for r in existentes)

    nuevos, duplicados = [], 0
    for m in movs:
        tipo_mov = 'ingreso' if m['importe'] > 0 else 'gasto'
        clave = (m['fecha'].strftime('%Y-%m-%d'), m['concepto'].strip().lower(), round(abs(m['importe']), 2), tipo_mov)
        if cuenta.get(clave, 0) > 0:
            cuenta[clave] -= 1
            duplicados += 1
        else: nuevos.append((m, tipo_mov))

    categorizados, filas = 0, []
    for m, tipo_mov in nuevos:
        cat = m['categoria'] or 'Gastos Generales'
        if cat not in ('Gastos Generales', 'Otros Ingresos'): categorizados += 1
        filas.append((usuario_id, m['fecha'].strftime('%Y-%m-%d'), m['concepto'], cat, round(abs(m['importe']), 2), tipo_mov))

    if filas:
        db = get_db()
        try:
            db.executemany("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)", filas)
            db.commit()
        except Exception:
            db.rollback(); raise

    return {'importados': len(filas), 'duplicados': duplicados, 'categorizados': categorizados, 'sin_fecha': info.get('sin_fecha', 0), 'descartadas': info.get('descartadas', 0)}

def _mensaje_resumen(r):
    if r['importados'] == 0 and r['duplicados']: return 'Estos movimientos ya estaban importados (%d repetidos). No se ha añadido nada.' % r['duplicados']
    msg = '¡Éxito! %d %s' % (r['importados'], 'movimiento importado' if r['importados'] == 1 else 'movimientos importados')
    msg += ' (%d auto-categorizados).' % r['categorizados']
    if r['duplicados']: msg += ' %d ya existían y se omitieron.' % r['duplicados']
    if r['sin_fecha']: msg += ' %d no tenían fecha: se usó la de hoy, revísalos.' % r['sin_fecha']
    return msg

def _json_error(mensaje, status, **extra):
    resp = jsonify(ok=False, error=mensaje, **extra)
    resp.status_code = status
    return resp

def _decodificar_b64(s):
    s = s.strip()
    if s.startswith('data:') and ',' in s[:200]: s = s.split(',', 1)[1]
    s = re.sub(r'\s+', '', s)
    s += '=' * (-len(s) % 4)
    return base64.b64decode(s, validate=True)

@app.errorhandler(413)
def _archivo_demasiado_grande(e):
    if request.path.startswith('/panel/importar'): return _json_error('El archivo es demasiado grande (máximo 10 MB).', 413)
    return e

@app.route('/panel/importar', methods=['POST'])
def importar_universal():
    try:
        if 'usuario_id' not in session: return _json_error('Tu sesión ha caducado. Vuelve a iniciar sesión.', 401, login=url_for('login'))
        if not validate_csrf(request.headers.get('X-CSRFToken', '')): return _json_error('La sesión de seguridad ha caducado.', 403)
        datos = request.get_json(silent=True)
        if not isinstance(datos, dict): return _json_error('Petición inválida.', 400)

        clave = datos.get('clave')
        clave = clave if isinstance(clave, str) and re.fullmatch(r'[A-Za-z0-9-]{8,64}', clave) else None
        ahora = time.time()
        claves = [k for k in session.get('imp_claves', []) if ahora - k[1] < 600]
        if clave and any(k[0] == clave for k in claves):
            return jsonify(ok=True, repetido=True, importados=0, duplicados=0, categorizados=0, sin_fecha=0, mensaje='Esta importación ya se había procesado.')

        texto = datos.get('texto', '')
        texto = texto if isinstance(texto, str) else ''
        raw = None
        archivo = datos.get('archivo')

        if archivo is not None:
            b64 = archivo.get('b64') if isinstance(archivo, dict) else None
            if not isinstance(b64, str) or not b64: return _json_error('El archivo llegó vacío.', 400)
            if len(b64) > MAX_B64: return _json_error('El archivo es demasiado grande.', 413)
            try: raw = _decodificar_b64(b64)
            except (binascii.Error, ValueError): return _json_error('El archivo llegó dañado.', 400)
            if not raw: return _json_error('El archivo está vacío.', 400)
            if len(raw) > MAX_BYTES: return _json_error('El archivo es demasiado grande.', 413)

        if raw is None and not texto.strip(): return _json_error('Selecciona un archivo o pega el texto de tu banco.', 400)

        resultado = procesar_importacion(session['usuario_id'], raw=raw, texto=texto)
        if clave:
            claves.append([clave, ahora])
            session['imp_claves'] = claves[-5:]
        return jsonify(ok=True, mensaje=_mensaje_resumen(resultado), **resultado)
    except ImportacionError as e: return _json_error(str(e), 422)
    except RequestEntityTooLarge: return _json_error('El archivo es demasiado grande.', 413)
    except Exception: return _json_error('Error procesando los datos. Intenta copiarlos de otra forma.', 500)

def _respuesta_legacy(raw=None, texto=''):
    try:
        r = procesar_importacion(session['usuario_id'], raw=raw, texto=texto)
        flash(_mensaje_resumen(r), 'success' if (r['importados'] or r['duplicados']) else 'warning')
    except ImportacionError as e: flash(str(e), 'warning')
    except Exception: flash('Error al procesar los datos.', 'danger')
    return redirect(url_for('transactions'))

@app.route('/panel/importar_csv', methods=['POST'])
@login_required
def importar_csv():
    if not validate_csrf(request.form.get('csrf_token')): return redirect(url_for('transactions'))
    f = request.files.get('archivo_csv')
    raw = f.read(MAX_BYTES + 1) if f and f.filename else b''
    if not raw: return redirect(url_for('transactions'))
    if len(raw) > MAX_BYTES: return redirect(url_for('transactions'))
    return _respuesta_legacy(raw=raw)

@app.route('/panel/importar_texto', methods=['POST'])
@login_required
def importar_texto_plano():
    if not validate_csrf(request.form.get('csrf_token')): return redirect(url_for('transactions'))
    texto = request.form.get('texto_banco', '')
    if not texto.strip(): return redirect(url_for('transactions'))
    return _respuesta_legacy(texto=texto)

# =================================================================
# RUTAS DE LA APLICACIÓN (DASHBOARD, CRUD, SETTINGS)
# =================================================================
@app.route('/')
def index():
    if 'usuario_id' in session: return redirect(url_for('dashboard'))
    return render_template('landing_publica.html')

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nombre = request.form['nombre'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']
        if query_db("SELECT id FROM usuarios WHERE email = ?", (email,), one=True):
            flash('Correo ya registrado.', 'danger')
            return redirect(url_for('registro'))
        execute_db("INSERT INTO usuarios (nombre, email, password) VALUES (?, ?, ?)", (nombre, email, generate_password_hash(password)))
        flash('Cuenta creada.', 'success')
        return redirect(url_for('login'))
    return render_template('registro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        usuario = query_db("SELECT * FROM usuarios WHERE email = ?", (email,), one=True)
        if usuario and check_password_hash(usuario['password'], password):
            session['usuario_id'], session['nombre'], session.permanent = usuario['id'], usuario['nombre'], True
            return redirect(url_for('dashboard'))
        flash('Credenciales inválidas.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/panel', methods=['GET'])
@login_required
def dashboard():
    usuario_id = session['usuario_id']
    hoy_date = datetime.date.today()
    mes_actual_str = hoy_date.strftime('%Y-%m')
    dia_actual = hoy_date.day
    dias_mes = calendar.monthrange(hoy_date.year, hoy_date.month)[1]
    dias_restantes = dias_mes - dia_actual

    recurrentes_pendientes = query_db("SELECT * FROM recurrentes WHERE usuario_id = ? AND (ultimo_mes_procesado IS NULL OR ultimo_mes_procesado != ?)", (usuario_id, mes_actual_str))
    recurrentes_futuros = []
    if recurrentes_pendientes:
        for rec in recurrentes_pendientes:
            if dia_actual >= rec['dia_mes']:
                fecha_cobro = f"{mes_actual_str}-{rec['dia_mes']:02d}"
                execute_db("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)", (usuario_id, fecha_cobro, f"🔄 {rec['concepto']}", rec['categoria'], rec['importe'], rec['tipo']))
                execute_db("UPDATE recurrentes SET ultimo_mes_procesado = ? WHERE id = ?", (mes_actual_str, rec['id']))
            else: recurrentes_futuros.append(rec)

    periodo_req = request.args.get('periodo')
    if periodo_req:
        session['periodo'] = periodo_req
        if periodo_req == 'custom':
            session['start_date'] = request.args.get('start', hoy_date.replace(day=1).strftime('%Y-%m-%d'))
            session['end_date'] = request.args.get('end', hoy_date.strftime('%Y-%m-%d'))

    periodo = session.get('periodo', 'mes')

    if periodo == 'trimestre':
        fecha_inicio = (hoy_date - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
        fecha_fin = hoy_date.strftime('%Y-%m-%d')
    elif periodo == 'anio':
        fecha_inicio = hoy_date.replace(month=1, day=1).strftime('%Y-%m-%d')
        fecha_fin = hoy_date.strftime('%Y-%m-%d')
    elif periodo == 'custom':
        fecha_inicio = session.get('start_date', hoy_date.replace(day=1).strftime('%Y-%m-%d'))
        fecha_fin = session.get('end_date', hoy_date.strftime('%Y-%m-%d'))
    else:
        fecha_inicio = hoy_date.replace(day=1).strftime('%Y-%m-%d')
        fecha_fin = hoy_date.replace(day=dias_mes).strftime('%Y-%m-%d')

    resultado = query_db("SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE 0 END), 0) as ingresos, COALESCE(SUM(CASE WHEN tipo='gasto' THEN importe ELSE 0 END), 0) as gastos FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ?", (usuario_id, fecha_inicio, fecha_fin), one=True)
    ingresos = resultado['ingresos'] if resultado else 0
    gastos = resultado['gastos'] if resultado else 0
    ahorro = ingresos - gastos
    ratio_ahorro = f"{(ahorro / ingresos * 100):.1f}%" if ingresos > 0 else "0.0%"

    movimientos = query_db("SELECT fecha, concepto, categoria, importe, tipo, id FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? ORDER BY fecha DESC, id DESC LIMIT 10", (usuario_id, fecha_inicio, fecha_fin))

    saldo_ant = query_db("SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE -importe END), 0) as saldo_previo FROM movimientos WHERE usuario_id = ? AND fecha < ?", (usuario_id, fecha_inicio), one=True)
    saldo_actual = saldo_ant['saldo_previo'] if saldo_ant else 0

    movimientos_serie = query_db("SELECT fecha, tipo, importe FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? ORDER BY fecha, id", (usuario_id, fecha_inicio, fecha_fin))
    saldo_fechas, saldo_valores = [], []
    saldo_iterativo = saldo_actual
    for mov in movimientos_serie:
        saldo_iterativo += mov['importe'] if mov['tipo'] == 'ingreso' else -mov['importe']
        saldo_fechas.append(mov['fecha'])
        saldo_valores.append(round(saldo_iterativo, 2))

    proyeccion_fechas, proyeccion_valores = [], []
    if periodo == 'mes' and dia_actual < dias_mes and saldo_fechas:
        proyeccion_fechas.append(saldo_fechas[-1])
        proyeccion_valores.append(saldo_valores[-1])
        gasto_medio_diario = gastos / dia_actual if dia_actual > 0 else 0
        saldo_futuro = saldo_valores[-1] - (gasto_medio_diario * dias_restantes)
        for rec in recurrentes_futuros: saldo_futuro += rec['importe'] if rec['tipo'] == 'ingreso' else -rec['importe']
        proyeccion_fechas.append(hoy_date.replace(day=dias_mes).strftime('%Y-%m-%d'))
        proyeccion_valores.append(round(saldo_futuro, 2))

    presupuestos_raw = query_db("SELECT categoria, importe_mensual FROM presupuestos WHERE usuario_id = ?", (usuario_id,))
    presupuestos = {row['categoria']: row['importe_mensual'] for row in presupuestos_raw}
    alertas_presupuesto = []
    gastos_mes_agrupados = query_db("SELECT categoria, SUM(importe) as total FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' AND strftime('%Y-%m', fecha) = ? GROUP BY categoria", (usuario_id, mes_actual_str))
    for gasto in gastos_mes_agrupados:
        presu = presupuestos.get(gasto['categoria'])
        if presu and presu > 0:
            ratio = gasto['total'] / presu
            if ratio >= 1.0: alertas_presupuesto.append({'nivel': 'danger', 'texto': f"Límite superado en {gasto['categoria']}"})
            elif ratio >= 0.8: alertas_presupuesto.append({'nivel': 'warning', 'texto': f"Atención: {ratio*100:.0f}% consumido en {gasto['categoria']}"})

    runway = None
    media = query_db("SELECT AVG(total) as media FROM (SELECT strftime('%Y-%m', fecha) AS mes, SUM(importe) as total FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' GROUP BY mes ORDER BY mes DESC LIMIT 3)", (usuario_id,), one=True)
    if media and media['media'] and media['media'] > 0 and ahorro > 0: runway = round(ahorro / media['media'], 1)

    meta = query_db("SELECT nombre, cantidad_objetivo FROM metas WHERE usuario_id = ? ORDER BY id DESC LIMIT 1", (usuario_id,), one=True)
    ahorro_historico = query_db("SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE -importe END), 0) as total FROM movimientos WHERE usuario_id = ?", (usuario_id,), one=True)
    ahorro_acumulado = ahorro_historico['total'] if ahorro_historico else 0

    es_nuevo_usuario = (query_db("SELECT COUNT(*) as c FROM movimientos WHERE usuario_id=?",(usuario_id,),one=True)['c'] == 0)

    return render_template(
        'dashboard.html',
        periodo=periodo,
        nombre=session.get('nombre'),
        ingresos=format_currency(ingresos),
        gastos=format_currency(gastos),
        ahorro=format_currency(ahorro),
        ahorro_acumulado=ahorro_acumulado,
        meta_nombre=meta['nombre'] if meta else None,
        meta_objetivo=meta['cantidad_objetivo'] if meta else 0,
        ratio_ahorro=ratio_ahorro,
        movimientos=movimientos,
        runway=runway,
        alertas_presupuesto=alertas_presupuesto,
        chart_saldo_tiempo={"labels": saldo_fechas, "data": saldo_valores},
        chart_proyeccion={"labels": proyeccion_fechas, "data": proyeccion_valores},
        es_nuevo_usuario=es_nuevo_usuario,
        mostrar_wrapped=False,
        ahorro_mes_pasado=0
    )

@app.route('/panel/analytics', methods=['GET'])
@login_required
def analytics():
    usuario_id = session['usuario_id']
    hoy = datetime.date.today()

    periodo_req = request.args.get('periodo')
    if periodo_req:
        session['periodo'] = periodo_req
        if periodo_req == 'custom':
            session['start_date'] = request.args.get('start', hoy.replace(day=1).strftime('%Y-%m-%d'))
            session['end_date'] = request.args.get('end', hoy.strftime('%Y-%m-%d'))

    periodo = session.get('periodo', 'mes')

    if periodo == 'trimestre':
        fecha_inicio = (hoy - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
        fecha_fin = hoy.strftime('%Y-%m-%d')
    elif periodo == 'anio':
        fecha_inicio = hoy.replace(month=1, day=1).strftime('%Y-%m-%d')
        fecha_fin = hoy.strftime('%Y-%m-%d')
    elif periodo == 'custom':
        fecha_inicio = session.get('start_date', hoy.replace(day=1).strftime('%Y-%m-%d'))
        fecha_fin = session.get('end_date', hoy.strftime('%Y-%m-%d'))
    else:
        fecha_inicio = hoy.replace(day=1).strftime('%Y-%m-%d')
        fecha_fin = hoy.replace(day=calendar.monthrange(hoy.year, hoy.month)[1]).strftime('%Y-%m-%d')

    resultado = query_db("SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE 0 END), 0) as ingresos, COALESCE(SUM(CASE WHEN tipo='gasto' THEN importe ELSE 0 END), 0) as gastos FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ?", (usuario_id, fecha_inicio, fecha_fin), one=True)
    ingresos = resultado['ingresos'] if resultado else 0
    gastos = resultado['gastos'] if resultado else 0
    ahorro = ingresos - gastos

    try: dias_diferencia = (datetime.datetime.strptime(fecha_fin, '%Y-%m-%d') - datetime.datetime.strptime(fecha_inicio, '%Y-%m-%d')).days
    except: dias_diferencia = 30

    agrupacion = "strftime('%Y-%m', fecha)" if dias_diferencia > 60 else "fecha"

    datos_agrupados = query_db(f"SELECT {agrupacion} as label, SUM(CASE WHEN tipo='ingreso' THEN importe ELSE 0 END) as ingresos, SUM(CASE WHEN tipo='gasto' THEN importe ELSE 0 END) as gastos FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? GROUP BY label ORDER BY label", (usuario_id, fecha_inicio, fecha_fin))
    labels = [d['label'] for d in datos_agrupados] if datos_agrupados else []
    datos_ahorro = [(d['ingresos'] - d['gastos']) for d in datos_agrupados] if datos_agrupados else []
    datos_categorias = query_db("SELECT UPPER(TRIM(categoria)) as cat_normalizada, categoria, SUM(importe) as total FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' AND fecha >= ? AND fecha <= ? GROUP BY cat_normalizada ORDER BY total DESC", (usuario_id, fecha_inicio, fecha_fin))

    return render_template('analytics.html', rango=periodo, ingresos=format_currency(ingresos), gastos=format_currency(gastos), ahorro=format_currency(ahorro), chart_ahorro_mes={"labels": labels, "data": datos_ahorro}, chart_gastos_categoria={"labels": [d['categoria'].strip().title() for d in datos_categorias] if datos_categorias else [], "data": [d['total'] for d in datos_categorias] if datos_categorias else []})

@app.route('/panel/anadir', methods=['GET', 'POST'])
@login_required
def anadir():
    if request.method == 'POST':
        execute_db("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)", (session['usuario_id'], request.form['fecha'], request.form['concepto'], request.form.get('categoria', 'Otros'), float(request.form['importe']), request.form['tipo']))
        flash('Registrado con éxito.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('anadir.html', hoy=datetime.date.today().strftime('%Y-%m-%d'))

@app.route('/panel/transactions')
@login_required
def transactions():
    page = request.args.get('page', 1, type=int)
    per_page = 25
    offset = (page - 1) * per_page
    total = query_db("SELECT COUNT(*) as cnt FROM movimientos WHERE usuario_id = ?", (session['usuario_id'],), one=True)['cnt']
    total_pages = max(1, (total + per_page - 1) // per_page)
    movimientos = query_db("SELECT * FROM movimientos WHERE usuario_id = ? ORDER BY fecha DESC, id DESC LIMIT ? OFFSET ?", (session['usuario_id'], per_page, offset))
    return render_template('transactions.html', movimientos=movimientos, page=page, total_pages=total_pages)

@app.route('/panel/borrar/<int:movimiento_id>', methods=['POST'])
@login_required
def borrar(movimiento_id):
    execute_db("DELETE FROM movimientos WHERE id = ? AND usuario_id = ?", (movimiento_id, session['usuario_id']))
    flash('Transacción eliminada.', 'success')
    return redirect(request.referrer or url_for('transactions'))

@app.route('/panel/duplicar/<int:movimiento_id>', methods=['POST'])
@login_required
def duplicar(movimiento_id):
    mov = query_db("SELECT * FROM movimientos WHERE id = ? AND usuario_id = ?", (movimiento_id, session['usuario_id']), one=True)
    if mov:
        execute_db("INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)",
                   (session['usuario_id'], datetime.date.today().strftime('%Y-%m-%d'), f"{mov['concepto']} (Copia)", mov['categoria'], mov['importe'], mov['tipo']))
        flash('Duplicado con éxito.', 'success')
    return redirect(url_for('transactions'))

@app.route('/panel/editar_movimiento', methods=['POST'])
@login_required
def editar_movimiento():
    execute_db("UPDATE movimientos SET fecha = ?, concepto = ?, categoria = ?, importe = ? WHERE id = ? AND usuario_id = ?", (request.form.get('fecha'), request.form.get('concepto'), request.form.get('categoria'), float(request.form.get('importe')), request.form.get('id'), session['usuario_id']))
    flash('Transacción actualizada.', 'success')
    return redirect(request.referrer or url_for('transactions'))

@app.route('/panel/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Error de seguridad. Recarga la página.', 'danger')
            return redirect(url_for('settings'))

        accion = request.form.get('accion')
        if accion == 'guardar_perfil':
            nombre = request.form.get('nombrevisible', '').strip()
            tema = request.form.get('tema', 'dark')
            if nombre:
                execute_db("UPDATE usuarios SET nombre = ? WHERE id = ?", (nombre, session['usuario_id']))
                session['nombre'] = nombre
                session['tema'] = tema
                flash('Perfil y apariencia actualizados.', 'success')

        elif accion == 'guardar_presupuestos':
            execute_db("DELETE FROM presupuestos WHERE usuario_id = ?", (session['usuario_id'],))
            for k, v in request.form.items():
                if k.startswith('presu_') and v.strip():
                    try:
                        val = float(v)
                        if val > 0:
                            execute_db("INSERT INTO presupuestos (usuario_id, categoria, importe_mensual) VALUES (?, ?, ?)",
                                       (session['usuario_id'], k.replace('presu_', ''), val))
                    except ValueError: pass
            flash('Presupuestos actualizados.', 'success')

        elif accion == 'nueva_meta':
            try:
                meta_objetivo = float(request.form.get('meta_objetivo', 0))
                if request.form.get('meta_nombre', '').strip() and meta_objetivo > 0:
                    execute_db("INSERT INTO metas (usuario_id, nombre, cantidad_objetivo, cantidad_actual) VALUES (?, ?, ?, 0)",
                               (session['usuario_id'], request.form.get('meta_nombre').strip(), meta_objetivo))
                    flash('Objetivo creado.', 'success')
            except ValueError: flash('Importe inválido.', 'danger')

        elif accion == 'borrar_meta':
            execute_db("DELETE FROM metas WHERE id = ? AND usuario_id = ?", (request.form.get('meta_id'), session['usuario_id']))
            flash('Meta eliminada.', 'success')

        return redirect(url_for('settings'))

    categorias_estandar = ['Alimentación', 'Restaurantes y Comida', 'Ocio y Tiempo Libre', 'Suscripciones Digitales', 'Servicios del Hogar', 'Vivienda', 'Transporte Público', 'Vehículo y Gasolina', 'Compras y Ropa', 'Salud y Bienestar', 'Educación', 'Tecnología', 'Viajes y Vacaciones']
    cat_db = query_db("SELECT DISTINCT categoria FROM movimientos WHERE usuario_id = ?", (session['usuario_id'],))
    categorias_usadas = sorted(list(set([row['categoria'] for row in cat_db] + categorias_estandar)))
    presupuestos_raw = query_db("SELECT categoria, importe_mensual FROM presupuestos WHERE usuario_id = ?", (session['usuario_id'],))

    return render_template('settings.html',
        nombre=session.get('nombre'),
        email_real=query_db("SELECT email FROM usuarios WHERE id = ?", (session['usuario_id'],), one=True)['email'],
        tema_actual=session.get('tema', 'dark'),
        presupuestos={row['categoria']: row['importe_mensual'] for row in presupuestos_raw} if presupuestos_raw else {},
        metas=query_db("SELECT id, nombre, cantidad_objetivo FROM metas WHERE usuario_id = ? ORDER BY id DESC", (session['usuario_id'],)),
        categorias_usadas=categorias_usadas)

@app.route('/panel/settings/preferencias', methods=['POST'])
@login_required
def guardar_preferencias():
    session['tema'] = request.json.get('tema', 'dark')
    return jsonify({"status": "success"})

@app.route('/exportar_csv')
@login_required
def exportar_csv():
    movimientos = query_db("SELECT fecha, tipo, concepto, categoria, importe FROM movimientos WHERE usuario_id = ? ORDER BY fecha DESC", (session['usuario_id'],))
    if not movimientos:
        flash('No hay movimientos para exportar.', 'warning')
        return redirect(request.referrer or url_for('transactions'))
    si = io.StringIO()
    cw = csv.writer(si, delimiter=';', lineterminator='\n')
    cw.writerow(['Fecha', 'Tipo', 'Concepto', 'Categoria', 'Importe (EUR)'])
    for row in movimientos:
        cw.writerow([row['fecha'], row['tipo'].upper(), row['concepto'], row['categoria'], f"{row['importe']:.2f}".replace('.', ',')])
    return Response(si.getvalue().encode('utf-8-sig'), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename=findash_{datetime.date.today().strftime('%Y%m%d')}.csv"})

if __name__ == '__main__':
    app.run(debug=True)
