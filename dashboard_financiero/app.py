import os
import re
import io
import csv
import secrets
import string
import datetime
import calendar
import unicodedata
from decimal import Decimal
from urllib.parse import urlparse
from functools import wraps

from flask import (
    Flask, render_template, render_template_string, request, 
    redirect, url_for, flash, session, jsonify, Response, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

# Control opcional de librerías de lectura masiva
try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

import psycopg2
from psycopg2.extras import RealDictCursor
import sqlite3

# =================================================================------------
# CONFIGURACIÓN DEL SERVIDOR & PROTECCIÓN PROXY HTTPS
# =================================================================------------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'findash_secret_production_key_2026_ssl')
app.permanent_session_lifetime = datetime.timedelta(days=7)

# Soporte para ProxyReverse en Render / Cloudflare (Asegura cookies de sesión en HTTPS)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

DATABASE_URL = os.environ.get('DATABASE_URL')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =================================================================------------
# CONEXIÓN INTELIGENTE A BASE DE DATOS (Neon PostgreSQL / SQLite Fallback)
# =================================================================------------
def get_db():
    """
    Obtiene la conexión activa según el entorno:
    - PostgreSQL en Neon.tech si DATABASE_URL está configurada.
    - SQLite local ('findash.db') como fallback para desarrollo.
    """
    if DATABASE_URL:
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
        return conn, 'postgres'
    else:
        db_path = os.path.join(BASE_DIR, 'findash.db')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn, 'sqlite'

def query_db(query, args=(), one=False, commit=False):
    """
    Ejecuta consultas de forma agnóstica entre PostgreSQL (%s) y SQLite (?).
    """
    conn, db_type = get_db()
    cur = conn.cursor()
    
    if db_type == 'sqlite':
        query = query.replace('%s', '?')
        
    try:
        cur.execute(query, args)
        if commit:
            conn.commit()
            last_id = None
            if db_type == 'sqlite':
                last_id = getattr(cur, 'lastrowid', None)
            elif db_type == 'postgres' and cur.description:
                try:
                    res = cur.fetchone()
                    if res:
                        last_id = list(res.values())[0] if isinstance(res, dict) else res[0]
                except Exception:
                    pass
            cur.close()
            conn.close()
            return last_id
        
        rv = cur.fetchall()
        cur.close()
        conn.close()
        
        results = [dict(row) for row in rv] if rv else []
        return (results[0] if results else None) if one else results
    except Exception as e:
        conn.rollback()
        conn.close()
        raise e

def execute_db(query, args=()):
    return query_db(query, args, commit=True)

# =================================================================------------
# INICIALIZACIÓN AUTOMÁTICA DEL ESQUEMA SQL
# =================================================================------------
def init_db():
    conn, db_type = get_db()
    cur = conn.cursor()
    
    if db_type == 'postgres':
        cur.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                nombre VARCHAR(255) DEFAULT 'Usuario',
                email VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                es_nuevo_usuario BOOLEAN DEFAULT TRUE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS movimientos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                fecha DATE NOT NULL,
                concepto VARCHAR(255) NOT NULL,
                importe NUMERIC(10, 2) NOT NULL,
                categoria VARCHAR(100) NOT NULL,
                tipo VARCHAR(20) DEFAULT 'gasto',
                es_ingreso BOOLEAN DEFAULT FALSE,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS presupuestos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                categoria VARCHAR(100) NOT NULL,
                limite NUMERIC(10, 2) NOT NULL,
                importe_mensual NUMERIC(10, 2) DEFAULT 0.00,
                UNIQUE(usuario_id, categoria)
            );
            CREATE TABLE IF NOT EXISTS metas (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                nombre VARCHAR(255) NOT NULL,
                cantidad_objetivo NUMERIC(10, 2) NOT NULL,
                cantidad_actual NUMERIC(10, 2) DEFAULT 0.00
            );
            CREATE TABLE IF NOT EXISTS recurrentes (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                concepto VARCHAR(255) NOT NULL,
                categoria VARCHAR(100) NOT NULL,
                importe NUMERIC(10, 2) NOT NULL,
                tipo VARCHAR(20) DEFAULT 'gasto',
                dia_mes INTEGER DEFAULT 1,
                ultimo_mes_procesado VARCHAR(20)
            );
            CREATE TABLE IF NOT EXISTS reglas_categorizacion (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                patron VARCHAR(255) NOT NULL,
                categoria VARCHAR(100) NOT NULL
            );
        ''')
    else:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT DEFAULT 'Usuario',
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                es_nuevo_usuario BOOLEAN DEFAULT 1,
                fecha_registro DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS movimientos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER REFERENCES usuarios(id),
                fecha DATE NOT NULL,
                concepto TEXT NOT NULL,
                importe REAL NOT NULL,
                categoria TEXT NOT NULL,
                tipo TEXT DEFAULT 'gasto',
                es_ingreso BOOLEAN DEFAULT 0,
                creado_en DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS presupuestos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER REFERENCES usuarios(id),
                categoria TEXT NOT NULL,
                limite REAL NOT NULL,
                importe_mensual REAL DEFAULT 0.00,
                UNIQUE(usuario_id, categoria)
            );
            CREATE TABLE IF NOT EXISTS metas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER REFERENCES usuarios(id),
                nombre TEXT NOT NULL,
                cantidad_objetivo REAL NOT NULL,
                cantidad_actual REAL DEFAULT 0.00
            );
            CREATE TABLE IF NOT EXISTS recurrentes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER REFERENCES usuarios(id),
                concepto TEXT NOT NULL,
                categoria TEXT NOT NULL,
                importe REAL NOT NULL,
                tipo TEXT DEFAULT 'gasto',
                dia_mes INTEGER DEFAULT 1,
                ultimo_mes_procesado TEXT
            );
            CREATE TABLE IF NOT EXISTS reglas_categorizacion (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER REFERENCES usuarios(id),
                patron TEXT NOT NULL,
                categoria TEXT NOT NULL
            );
        ''')
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"[FinDash Init DB Warning] {e}")

# =================================================================------------
# FILTROS Y UTILIDADES FORMATO DE MONEDA Y SEGURIDAD CSRF
# =================================================================------------
def format_currency(value):
    try:
        val = float(value)
        return f"{val:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.')
    except (ValueError, TypeError):
        return "0,00 €"

app.jinja_env.filters['format_currency'] = format_currency

def generate_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

def validate_csrf(token):
    return token and token == session.get('csrf_token')

app.jinja_env.globals['csrf_token'] = generate_csrf_token

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'usuario_id' not in session:
            flash('Por favor, inicia sesión para acceder.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# =================================================================------------
# MOTOR DE CATEGORIZACIÓN INTELIGENTE (NLP + NORMALIZACIÓN UNICODE)
# =================================================================------------
def normalizar_texto(texto):
    if not texto:
        return ""
    texto_norm = unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8')
    return texto_norm.lower().strip()

def categorizar_movimiento(concepto, importe_val, usuario_id=None):
    concepto_low = normalizar_texto(concepto)
    
    # 1. Reglas personalizadas del usuario en BD
    if usuario_id:
        reglas = query_db("SELECT patron, categoria FROM reglas_categorizacion WHERE usuario_id = %s", (usuario_id,))
        for r in reglas:
            if normalizar_texto(r['patron']) in concepto_low:
                return r['categoria']

    # 2. Reglas de Ingresos
    if importe_val > 0:
        if any(kw in concepto_low for kw in ['nomina', 'salario', 'sueldo', 'haberes', 'pension', 'paro', 'empresa']):
            return 'Nómina y Salario'
        if any(kw in concepto_low for kw in ['bizum', 'transferencia', 'traspaso', 'ingreso']):
            return 'Transferencias'
        if any(kw in concepto_low for kw in ['dividendo', 'interes', 'inversion', 'cripto']):
            return 'Rendimientos e Inversiones'
        return 'Otros Ingresos'

    # 3. Diccionario base de Gastos
    mapa_categorias = {
        'Alimentación': ['mercadona', 'carrefour', 'lidl', 'dia', 'aldi', 'consum', 'eroski', 'supermercado', 'fruteria', 'panaderia'],
        'Restaurantes y Comida': ['restaurante', 'mcdonalds', 'burger', 'glovo', 'just eat', 'uber eats', 'telepizza', 'kfc', 'pizzeria', 'bar', 'cafeteria', 'starbucks'],
        'Ocio y Tiempo Libre': ['cine', 'discoteca', 'concierto', 'entrada', 'teatro', 'pub', 'cerveza', 'copas', 'gimnasio', 'bowling'],
        'Suscripciones Digitales': ['netflix', 'spotify', 'hbo', 'disney', 'amazon prime', 'patreon', 'icloud', 'youtube', 'dazn', 'apple'],
        'Servicios del Hogar': ['luz', 'agua', 'gas', 'internet', 'iberdrola', 'endesa', 'naturgy', 'vodafone', 'movistar', 'orange', 'pepephone', 'o2', 'factura'],
        'Vivienda': ['alquiler', 'hipoteca', 'comunidad', 'ikea', 'leroy merlin', 'bricomart', 'inmobiliaria', 'fianza'],
        'Transporte Público': ['metro', 'autobus', 'renfe', 'tren', 'bonobus', 'taxi', 'uber', 'cabify', 'bolt', 'ave'],
        'Vehículo y Gasolina': ['gasolinera', 'repsol', 'cepsa', 'bp', 'galp', 'taller', 'seguro coche', 'parking', 'peaje', 'itv', 'gasolina'],
        'Compras y Ropa': ['zara', 'amazon', 'el corte ingles', 'shein', 'aliexpress', 'zalando', 'mango', 'primark', 'h&m', 'pull'],
        'Salud y Bienestar': ['farmacia', 'dentista', 'medico', 'fisio', 'psicologo', 'optica', 'clinica', 'hospital'],
        'Educación': ['universidad', 'curso', 'academia', 'libros', 'papeleria', 'master', 'clases'],
        'Tecnología': ['pc componentes', 'mediamarkt', 'movil', 'apple store', 'software', 'hardware', 'game'],
        'Viajes y Vacaciones': ['vuelo', 'hotel', 'ryanair', 'booking', 'airbnb', 'vueling', 'iberia', 'viaje']
    }

    for cat, keywords in mapa_categorias.items():
        if any(normalizar_texto(kw) in concepto_low for kw in keywords):
            return cat

    return 'Gastos Generales'

def limpiar_fecha(val_fecha):
    if isinstance(val_fecha, (datetime.date, datetime.datetime)):
        return val_fecha.strftime('%Y-%m-%d')
    fecha_str = str(val_fecha).split(' ')[0].strip()
    if '/' in fecha_str:
        partes = fecha_str.split('/')
        if len(partes) == 3:
            if len(partes[0]) == 4:
                return f"{partes[0]}-{partes[1].zfill(2)}-{partes[2].zfill(2)}"
            else:
                dia, mes, anio = partes[0].zfill(2), partes[1].zfill(2), partes[2]
                if len(anio) == 2: anio = '20' + anio
                return f"{anio}-{mes}-{dia}"
    elif '-' in fecha_str and len(fecha_str.split('-')[0]) == 2:
        partes = fecha_str.split('-')
        dia, mes, anio = partes[0].zfill(2), partes[1].zfill(2), partes[2]
        if len(anio) == 2: anio = '20' + anio
        return f"{anio}-{mes}-{dia}"
    return fecha_str

def limpiar_importe(val_importe):
    if isinstance(val_importe, (int, float)):
        return float(val_importe)
    imp_str = str(val_importe).replace('€', '').replace('$', '').replace('EUR', '').strip()
    if '.' in imp_str and ',' in imp_str:
        imp_str = imp_str.replace('.', '').replace(',', '.')
    elif ',' in imp_str:
        imp_str = imp_str.replace(',', '.')
    return float(imp_str)

# =================================================================------------
# RUTAS AUTENTICACIÓN Y REGISTRO
# =================================================================------------
@app.route('/')
def index():
    if 'usuario_id' in session:
        return redirect(url_for('panel'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        user = query_db("SELECT * FROM usuarios WHERE LOWER(email) = %s", (email,), one=True)
        if user:
            # Compatibilidad con contraseñas en hash o texto plano antiguo
            pwd_ok = False
            if user['password'].startswith('scrypt:') or user['password'].startswith('pbkdf2:'):
                pwd_ok = check_password_hash(user['password'], password)
            else:
                pwd_ok = (user['password'] == password)

            if pwd_ok:
                session.permanent = True
                session['usuario_id'] = user['id']
                session['user_email'] = user['email']
                session['nombre'] = user.get('nombre', 'Usuario')
                session['periodo'] = 'mes'
                flash('¡Bienvenido de nuevo a FinDash!', 'success')
                return redirect(url_for('panel'))
            
        flash('Credenciales incorrectas. Verifique el correo o contraseña.', 'danger')

    return render_template('login.html') if os.path.exists(os.path.join(BASE_DIR, 'templates', 'login.html')) else render_template_string(HTML_LOGIN_FALLBACK)

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nombre = request.form.get('nombre', 'Usuario').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            flash('Por favor complete todos los campos.', 'warning')
            return redirect(url_for('registro'))

        existente = query_db("SELECT id FROM usuarios WHERE LOWER(email) = %s", (email,), one=True)
        if existente:
            flash('Este correo electrónico ya está registrado. Inicie sesión.', 'warning')
            return redirect(url_for('login'))

        hashed_pwd = generate_password_hash(password)
        execute_db(
            "INSERT INTO usuarios (nombre, email, password, es_nuevo_usuario) VALUES (%s, %s, %s, TRUE)",
            (nombre, email, hashed_pwd)
        )
        flash('Cuenta creada con éxito. Ya puede iniciar sesión.', 'success')
        return redirect(url_for('login'))

    return render_template('registro.html') if os.path.exists(os.path.join(BASE_DIR, 'templates', 'registro.html')) else render_template_string(HTML_REGISTRO_FALLBACK)

@app.route('/logout')
def logout():
    session.clear()
    flash('Sesión cerrada correctamente.', 'info')
    return redirect(url_for('login'))

# =================================================================------------
# PANEL PRINCIPAL (DASHBOARD & BENTO GRID)
# =================================================================------------
@app.route('/panel', methods=['GET'])
@login_required
def panel():
    uid = session['usuario_id']
    usuario = query_db("SELECT * FROM usuarios WHERE id = %s", (uid,), one=True)
    hoy = datetime.date.today()
    mes_actual_str = hoy.strftime('%Y-%m')

    # Procesar gastos recurrentes automáticos
    recurrentes = query_db(
        "SELECT * FROM recurrentes WHERE usuario_id = %s AND (ultimo_mes_procesado IS NULL OR ultimo_mes_procesado != %s)",
        (uid, mes_actual_str)
    )
    if recurrentes:
        for rec in recurrentes:
            if hoy.day >= rec.get('dia_mes', 1):
                fecha_cobro = f"{mes_actual_str}-{rec.get('dia_mes', 1):02d}"
                is_ing = (rec['tipo'] == 'ingreso')
                execute_db(
                    "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo, es_ingreso) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (uid, fecha_cobro, f"🔄 {rec['concepto']}", rec['categoria'], rec['importe'], rec['tipo'], is_ing)
                )
                execute_db("UPDATE recurrentes SET ultimo_mes_procesado = %s WHERE id = %s", (mes_actual_str, rec['id']))

    # Selección de Periodo
    periodo_req = request.args.get('periodo')
    if periodo_req:
        session['periodo'] = periodo_req
    periodo = session.get('periodo', 'mes')

    if periodo == 'trimestre':
        fecha_inicio = (hoy - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    elif periodo == 'anio':
        fecha_inicio = hoy.replace(month=1, day=1).strftime('%Y-%m-%d')
    else:
        fecha_inicio = hoy.replace(day=1).strftime('%Y-%m-%d')

    fecha_fin = hoy.strftime('%Y-%m-%d')

    movimientos = query_db(
        "SELECT * FROM movimientos WHERE usuario_id = %s AND fecha >= %s AND fecha <= %s ORDER BY fecha DESC, id DESC",
        (uid, fecha_inicio, fecha_fin)
    )

    es_nuevo = usuario.get('es_nuevo_usuario', True) if usuario else True
    if movimientos and len(movimientos) > 0:
        es_nuevo = False
        if usuario and usuario.get('es_nuevo_usuario'):
            execute_db("UPDATE usuarios SET es_nuevo_usuario = FALSE WHERE id = %s", (uid,))

    total_ingresos = Decimal('0.00')
    total_gastos = Decimal('0.00')
    gastos_por_cat = {}

    for m in (movimientos or []):
        imp = Decimal(str(m['importe']))
        is_ingreso = m.get('es_ingreso') or (m.get('tipo') == 'ingreso')
        if is_ingreso:
            total_ingresos += imp
        else:
            total_gastos += imp
            cat = m['categoria']
            gastos_por_cat[cat] = gastos_por_cat.get(cat, Decimal('0.00')) + imp

    patrimonio_neto = total_ingresos - total_gastos
    tasa_ahorro = (patrimonio_neto / total_ingresos * 100) if total_ingresos > 0 else Decimal('0.0')

    # Runway / Colchón Financiero en meses
    runway = Decimal('0.0')
    if total_gastos > 0:
        gastos_diarios = total_gastos / Decimal(max(1, hoy.day))
        gasto_mensual_estimado = gastos_diarios * Decimal('30')
        if gasto_mensual_estimado > 0 and patrimonio_neto > 0:
            runway = round(patrimonio_neto / gasto_mensual_estimado, 1)

    # Alertas Semafóricas de Presupuesto
    presupuestos = query_db("SELECT * FROM presupuestos WHERE usuario_id = %s", (uid,))
    alertas = []
    for p in (presupuestos or []):
        cat = p['categoria']
        lim = Decimal(str(p.get('limite') or p.get('importe_mensual') or '0.00'))
        gasto_cat = gastos_por_cat.get(cat, Decimal('0.00'))
        if lim > 0:
            pct = (gasto_cat / lim) * 100
            if pct >= 100:
                alertas.append({'cat': cat, 'pct': round(pct, 1), 'nivel': 'danger', 'msg': f'Excedido {cat} ({round(pct, 0)}%)'})
            elif pct >= 80:
                alertas.append({'cat': cat, 'pct': round(pct, 1), 'nivel': 'warning', 'msg': f'Alerta {cat} ({round(pct, 0)}%)'})

    meta = query_db("SELECT * FROM metas WHERE usuario_id = %s ORDER BY id DESC", (uid,), one=True)

    return render_template(
        'dashboard.html',
        es_nuevo_usuario=es_nuevo,
        total_ingresos=total_ingresos,
        total_gastos=total_gastos,
        patrimonio_neto=patrimonio_neto,
        tasa_ahorro=round(tasa_ahorro, 1),
        runway=runway,
        movimientos=(movimientos[:10] if movimientos else []),
        alertas=alertas,
        meta=meta,
        periodo=periodo
    ) if os.path.exists(os.path.join(BASE_DIR, 'templates', 'dashboard.html')) else render_template_string(
        HTML_DASHBOARD_FALLBACK,
        total_ingresos=format_currency(total_ingresos),
        total_gastos=format_currency(total_gastos),
        patrimonio_neto=format_currency(patrimonio_neto),
        tasa_ahorro=round(tasa_ahorro, 1),
        runway=runway,
        es_nuevo_usuario=es_nuevo,
        movimientos=(movimientos[:10] if movimientos else [])
    )

# =================================================================------------
# RUTAS DE INGESTA DE DATOS (ARCHIVOS Y ESCÁNER DE TEXTO IA)
# =================================================================------------
@app.route('/panel/importar_extracto', methods=['POST'])
@login_required
def importar_extracto():
    texto_puro = request.form.get('texto_banco', '')
    if not texto_puro.strip():
        flash('El texto pegado está vacío.', 'warning')
        return redirect(url_for('panel'))

    uid = session['usuario_id']
    añadidos = 0
    
    # Expresión regular universal para fechas, conceptos e importes
    patron = r'(\d{2,4}[/-]\d{2}[/-]\d{2,4})\s+(.+?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d{1,3}(?:,\d{3})*\.\d{2})'
    texto_limpio = texto_puro.replace('€', '').replace('EUR', '').replace('\r', ' ')

    for coincidencia in re.finditer(patron, texto_limpio):
        val_fecha = coincidencia.group(1).strip()
        concepto = coincidencia.group(2).strip()
        val_importe = coincidencia.group(3).strip()

        try:
            fecha = limpiar_fecha(val_fecha)
            importe_val = limpiar_importe(val_importe)
            is_ingreso = (importe_val > 0)
            tipo = 'ingreso' if is_ingreso else 'gasto'
            categoria = categorizar_movimiento(concepto, importe_val, usuario_id=uid)

            execute_db(
                "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo, es_ingreso) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uid, fecha, concepto, categoria, abs(importe_val), tipo, is_ingreso)
            )
            añadidos += 1
        except Exception:
            continue

    if añadidos > 0:
        flash(f'¡Éxito! Se importaron {añadidos} movimientos correctamente.', 'success')
    else:
        flash('No se detectaron transacciones válidas en el texto pegado.', 'warning')

    return redirect(url_for('panel'))

@app.route('/panel/importar_csv', methods=['POST'])
@login_required
def importar_csv():
    if 'archivo_csv' not in request.files:
        flash('No se adjuntó ningún archivo.', 'warning')
        return redirect(url_for('panel'))

    file = request.files['archivo_csv']
    if not file or file.filename == '':
        flash('Seleccione un archivo CSV o Excel válido.', 'warning')
        return redirect(url_for('panel'))

    uid = session['usuario_id']
    nombre_archivo = file.filename.lower()
    añadidos = 0

    try:
        if nombre_archivo.endswith('.xlsx') and openpyxl:
            wb = openpyxl.load_workbook(file, data_only=True)
            sheet = wb.active
            rows = list(sheet.values)
            if len(rows) > 1:
                for row in rows[1:]:
                    if not row or len(row) < 3: continue
                    try:
                        fecha = limpiar_fecha(row[0])
                        concepto = str(row[1]).strip()
                        imp = limpiar_importe(row[2])
                        is_ingreso = (imp > 0)
                        cat = categorizar_movimiento(concepto, imp, uid)
                        execute_db(
                            "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo, es_ingreso) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (uid, fecha, concepto, cat, abs(imp), 'ingreso' if is_ingreso else 'gasto', is_ingreso)
                        )
                        añadidos += 1
                    except Exception:
                        continue
        else:
            raw_data = file.stream.read()
            try: decoded_text = raw_data.decode("UTF-8")
            except UnicodeDecodeError: decoded_text = raw_data.decode("latin-1", errors="ignore")

            stream = io.StringIO(decoded_text, newline=None)
            delimitador = ';' if ';' in stream.readline() else ','
            stream.seek(0)

            reader = csv.reader(stream, delimiter=delimitador)
            next(reader, None) # Saltar cabecera

            for row in reader:
                if not row or len(row) < 3: continue
                try:
                    fecha = limpiar_fecha(row[0].strip())
                    concepto = row[1].strip()
                    imp = limpiar_importe(row[2].strip())
                    is_ingreso = (imp > 0)
                    cat = categorizar_movimiento(concepto, imp, uid)
                    execute_db(
                        "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo, es_ingreso) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (uid, fecha, concepto, cat, abs(imp), 'ingreso' if is_ingreso else 'gasto', is_ingreso)
                    )
                    añadidos += 1
                except Exception:
                    continue

        flash(f'Se procesaron {añadidos} movimientos desde el archivo.', 'success')
    except Exception as e:
        flash(f'Error al procesar archivo: {e}', 'danger')

    return redirect(url_for('panel'))

# =================================================================------------
# MODAL EDITAR & OPERACIONES SOBRE MOVIMIENTOS
# =================================================================------------
@app.route('/panel/editar_movimiento', methods=['POST'])
@login_required
def editar_movimiento():
    uid = session['usuario_id']
    mov_id = request.form.get('id')
    fecha = request.form.get('fecha')
    concepto = request.form.get('concepto', '').strip()
    categoria = request.form.get('categoria', 'Otros')
    
    try:
        importe = float(request.form.get('importe', 0))
        execute_db(
            "UPDATE movimientos SET fecha = %s, concepto = %s, categoria = %s, importe = %s WHERE id = %s AND usuario_id = %s",
            (fecha, concepto, categoria, abs(importe), mov_id, uid)
        )
        
        # Guardar regla de aprendizaje del usuario
        if concepto and categoria:
            patron = normalizar_texto(concepto)
            execute_db(
                "INSERT INTO reglas_categorizacion (usuario_id, patron, categoria) VALUES (%s, %s, %s)",
                (uid, patron, categoria)
            )
            
        flash('Registro actualizado correctamente.', 'success')
    except Exception as e:
        flash(f'Error al actualizar registro: {e}', 'danger')

    return redirect(request.referrer or url_for('panel'))

@app.route('/panel/borrar/<int:mov_id>', methods=['POST'])
@login_required
def borrar_movimiento(mov_id):
    uid = session['usuario_id']
    execute_db("DELETE FROM movimientos WHERE id = %s AND usuario_id = %s", (mov_id, uid))
    flash('Registro eliminado del Libro Mayor.', 'info')
    return redirect(request.referrer or url_for('panel'))

# =================================================================------------
# PLANTILLAS HTML EMBEBIDAS (FALLBACK)
# =================================================================------------
HTML_LOGIN_FALLBACK = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FinDash - Iniciar Sesión</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
        .card { background: #1e293b; padding: 2.5rem; border-radius: 1rem; width: 100%; max-width: 400px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
        h1 { color: #10b981; margin-bottom: 1.5rem; text-align: center; }
        input { width: 100%; padding: 0.75rem; margin-bottom: 1rem; border-radius: 0.5rem; border: 1px solid #334155; background: #0f172a; color: white; box-sizing: border-box; }
        button { width: 100%; padding: 0.75rem; border-radius: 0.5rem; border: none; background: #10b981; color: white; font-weight: bold; cursor: pointer; }
        .footer { text-align: center; margin-top: 1rem; font-size: 0.875rem; color: #94a3b8; }
        a { color: #34d399; text-decoration: none; }
    </style>
</head>
<body>
    <div class="card">
        <h1>FinDash Pro</h1>
        <form method="POST" action="/login">
            <input type="email" name="email" placeholder="Correo Electrónico" required>
            <input type="password" name="password" placeholder="Contraseña" required>
            <button type="submit">Entrar al Panel</button>
        </form>
        <div class="footer">
            ¿No tienes cuenta? <a href="/registro">Crear Cuenta</a>
        </div>
    </div>
</body>
</html>
"""

HTML_REGISTRO_FALLBACK = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FinDash - Registro</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
        .card { background: #1e293b; padding: 2.5rem; border-radius: 1rem; width: 100%; max-width: 400px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
        h1 { color: #10b981; margin-bottom: 1.5rem; text-align: center; }
        input { width: 100%; padding: 0.75rem; margin-bottom: 1rem; border-radius: 0.5rem; border: 1px solid #334155; background: #0f172a; color: white; box-sizing: border-box; }
        button { width: 100%; padding: 0.75rem; border-radius: 0.5rem; border: none; background: #10b981; color: white; font-weight: bold; cursor: pointer; }
        .footer { text-align: center; margin-top: 1rem; font-size: 0.875rem; color: #94a3b8; }
        a { color: #34d399; text-decoration: none; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Crear Cuenta FinDash</h1>
        <form method="POST" action="/registro">
            <input type="text" name="nombre" placeholder="Nombre Completo" required>
            <input type="email" name="email" placeholder="Correo Electrónico" required>
            <input type="password" name="password" placeholder="Contraseña" required>
            <button type="submit">Registrarse</button>
        </form>
        <div class="footer">
            ¿Ya tienes cuenta? <a href="/login">Iniciar Sesión</a>
        </div>
    </div>
</body>
</html>
"""

HTML_DASHBOARD_FALLBACK = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FinDash - Muro de Control</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 1.5rem; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
        .kpi { background: #1e293b; padding: 1.5rem; border-radius: 0.75rem; border: 1px solid #334155; }
        .kpi-title { font-size: 0.875rem; color: #94a3b8; margin-bottom: 0.5rem; }
        .kpi-value { font-size: 1.5rem; font-weight: bold; color: #10b981; }
        .btn-import { background: #3b82f6; color: white; padding: 0.75rem 1.5rem; border-radius: 0.5rem; text-decoration: none; border: none; cursor: pointer; font-weight: bold; }
        textarea { width: 100%; height: 100px; background: #0f172a; border: 1px solid #334155; color: white; border-radius: 0.5rem; padding: 0.5rem; box-sizing: border-box; margin-bottom: 1rem; }
    </style>
</head>
<body>
    <div class="header">
        <h2>FinDash - Control Operativo</h2>
        <a href="/logout" style="color:#ef4444;">Cerrar Sesión</a>
    </div>

    <div class="grid">
        <div class="kpi"><div class="kpi-title">Patrimonio Neto</div><div class="kpi-value">{{ patrimonio_neto }}</div></div>
        <div class="kpi"><div class="kpi-title">Ingresos</div><div class="kpi-value" style="color:#34d399;">{{ total_ingresos }}</div></div>
        <div class="kpi"><div class="kpi-title">Gastos</div><div class="kpi-value" style="color:#f87171;">{{ total_gastos }}</div></div>
        <div class="kpi"><div class="kpi-title">Tasa de Ahorro</div><div class="kpi-value">{{ tasa_ahorro }}%</div></div>
        <div class="kpi"><div class="kpi-title">Colchón Financiero</div><div class="kpi-value">{{ runway }} meses</div></div>
    </div>

    <div style="background:#1e293b; padding:1.5rem; border-radius:0.75rem; margin-bottom:2rem;">
        <h3>Escáner de Texto IA (Pegar Extracto Bancario)</h3>
        <form method="POST" action="/panel/importar_extracto">
            <textarea name="texto_banco" placeholder="Pega aquí los movimientos copiados de tu app bancaria..."></textarea>
            <button type="submit" class="btn-import">Importar Extracto Instantáneo</button>
        </form>
    </div>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(host='0.08.0.0', port=5000, debug=True)
