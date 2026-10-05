import os
import sqlite3
import datetime
import csv
import io
import secrets
import string
import requests # Añadido para hacer la comprobación con Google
from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for, flash, g, Response, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

# =================================================================
# CONFIGURACIÓN DEL SERVIDOR Y SEGURIDAD DE SESIÓN
# =================================================================
app = Flask(__name__)
app.secret_key = 'findash_secreto_2026_super_seguro'
app.permanent_session_lifetime = datetime.timedelta(days=7)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'database.db')

# =================================================================
# UTILIDADES DE CAPA DE DATOS (SQLITE3)
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
        db.execute('''CREATE TABLE IF NOT EXISTS usuarios (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        nombre TEXT,
                        email TEXT UNIQUE,
                        password TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS movimientos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        usuario_id INTEGER,
                        fecha TEXT,
                        concepto TEXT,
                        categoria TEXT,
                        importe REAL,
                        tipo TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS presupuestos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        usuario_id INTEGER,
                        categoria TEXT,
                        importe_mensual REAL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS metas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        usuario_id INTEGER,
                        nombre TEXT,
                        cantidad_objetivo REAL,
                        cantidad_actual REAL DEFAULT 0)''')
        db.commit()

init_db()

# =================================================================
# FILTROS JINJA2, CSRF Y DECORADORES
# =================================================================
def format_currency(value):
    try:
        return f"{float(value):,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.')
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
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# =================================================================
# RUTAS PÚBLICAS Y AUTENTICACIÓN
# =================================================================
@app.route('/')
def index():
    if 'usuario_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('landing_publica.html')

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/sw.js')
def service_worker():
    response = app.send_static_file('sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Error de seguridad. Recarga la página e inténtalo de nuevo.', 'danger')
            return redirect(url_for('registro'))

        nombre = request.form['nombre'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']

        if len(password) < 6 or not any(c.isdigit() for c in password) or not any(c.isalpha() for c in password):
            flash('La contraseña debe tener al menos 6 caracteres con letras y números.', 'danger')
            return redirect(url_for('registro'))

        if query_db("SELECT id FROM usuarios WHERE email = ?", (email,), one=True):
            flash('Este correo electrónico ya está registrado.', 'danger')
            return redirect(url_for('registro'))

        execute_db("INSERT INTO usuarios (nombre, email, password) VALUES (?, ?, ?)",
                   (nombre, email, generate_password_hash(password)))
        flash('Cuenta creada con éxito. Ya puedes iniciar sesión.', 'success')
        return redirect(url_for('login'))

    return render_template('registro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Error de seguridad. Recarga la página e inténtalo de nuevo.', 'danger')
            return redirect(url_for('login'))

        email = request.form['email'].strip().lower()
        password = request.form['password']
        usuario = query_db("SELECT * FROM usuarios WHERE email = ?", (email,), one=True)

        if usuario and check_password_hash(usuario['password'], password):
            session.permanent = True
            session['usuario_id'] = usuario['id']
            session['nombre'] = usuario['nombre']
            session['periodo'] = 'mes'
            return redirect(url_for('dashboard'))
        else:
            flash('Las credenciales introducidas no son válidas.', 'danger')

    return render_template('login.html')

# NUEVA RUTA: Recibe y procesa el login con Google
@app.route('/login_google', methods=['POST'])
def login_google():
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Error de seguridad. Recarga la página e inténtalo de nuevo.', 'danger')
        return redirect(url_for('login'))

    token = request.form.get('id_token')
    if not token:
        flash('No se recibió información válida de Google.', 'danger')
        return redirect(url_for('login'))

    try:
        # Validamos el token preguntándole directamente a los servidores de Google
        response = requests.get(f"https://oauth2.googleapis.com/tokeninfo?id_token={token}")
        
        if response.status_code != 200:
            flash('Token de Google inválido o expirado.', 'danger')
            return redirect(url_for('login'))

        user_info = response.json()
        email = user_info.get('email').lower()
        nombre = user_info.get('name', 'Usuario de Google')

        # Comprobamos si el usuario ya existe en nuestra base de datos local
        usuario = query_db("SELECT * FROM usuarios WHERE email = ?", (email,), one=True)

        if not usuario:
            # Si no existe, lo REGISTRAMOS automáticamente. 
            # Generamos una contraseña súper compleja ya que entrará usando el botón de Google.
            random_pass = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
            hashed_pass = generate_password_hash(random_pass)

            usuario_id = execute_db(
                "INSERT INTO usuarios (nombre, email, password) VALUES (?, ?, ?)",
                (nombre, email, hashed_pass)
            )
        else:
            usuario_id = usuario['id']
            nombre = usuario['nombre']

        # Iniciamos su sesión
        session.permanent = True
        session['usuario_id'] = usuario_id
        session['nombre'] = nombre
        session['periodo'] = 'mes'

        flash('Has iniciado sesión correctamente.', 'success')
        return redirect(url_for('dashboard'))

    except Exception as e:
        print("Error en auth Google:", e)
        flash('Ocurrió un error al conectar con Google. Inténtalo más tarde.', 'danger')
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    flash('Sesión cerrada correctamente.', 'success')
    return redirect(url_for('login'))

# =================================================================
# PASARELAS DE COMPATIBILIDAD
# =================================================================
@app.route('/añadir')
@app.route('/anadir')
@login_required
def redireccion_antigua_anadir():
    return redirect(url_for('anadir'))

@app.route('/transactions')
@login_required
def redireccion_antigua_transactions():
    return redirect(url_for('transactions'))

# =================================================================
# PANEL PRIVADO - DASHBOARD
# =================================================================
@app.route('/panel', methods=['GET'])
@login_required
def dashboard():
    usuario_id = session['usuario_id']
    hoy = datetime.date.today()
    mes_actual_str = hoy.strftime('%Y-%m')

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

    resultado = query_db(
        "SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE 0 END), 0) as ingresos, "
        "COALESCE(SUM(CASE WHEN tipo='gasto' THEN importe ELSE 0 END), 0) as gastos "
        "FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ?",
        (usuario_id, fecha_inicio, fecha_fin), one=True
    )
    ingresos = resultado['ingresos'] if resultado else 0
    gastos = resultado['gastos'] if resultado else 0
    ahorro = ingresos - gastos
    ratio_ahorro = (ahorro / ingresos * 100) if ingresos > 0 else 0

    movimientos = query_db(
        "SELECT fecha, concepto, categoria, importe, tipo, id FROM movimientos "
        "WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? "
        "ORDER BY fecha DESC, id DESC LIMIT 10",
        (usuario_id, fecha_inicio, fecha_fin)
    )

    saldo_ant = query_db(
        "SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE -importe END), 0) as saldo_previo "
        "FROM movimientos WHERE usuario_id = ? AND fecha < ?",
        (usuario_id, fecha_inicio), one=True
    )
    saldo = saldo_ant['saldo_previo'] if saldo_ant else 0

    movimientos_serie = query_db(
        "SELECT fecha, tipo, importe FROM movimientos "
        "WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? ORDER BY fecha, id",
        (usuario_id, fecha_inicio, fecha_fin)
    )

    saldo_fechas, saldo_valores = [], []
    for mov in movimientos_serie:
        saldo += mov['importe'] if mov['tipo'] == 'ingreso' else -mov['importe']
        saldo_fechas.append(mov['fecha'])
        saldo_valores.append(round(saldo, 2))

    presupuestos_raw = query_db(
        "SELECT categoria, importe_mensual FROM presupuestos WHERE usuario_id = ?", (usuario_id,))
    presupuestos = {row['categoria']: row['importe_mensual'] for row in presupuestos_raw}

    alertas_presupuesto = []
    gastos_mes_agrupados = query_db(
        "SELECT categoria, SUM(importe) as total FROM movimientos "
        "WHERE usuario_id = ? AND tipo = 'gasto' AND strftime('%Y-%m', fecha) = ? GROUP BY categoria",
        (usuario_id, mes_actual_str)
    )
    for gasto in gastos_mes_agrupados:
        presu = presupuestos.get(gasto['categoria'])
        if presu and presu > 0:
            ratio = gasto['total'] / presu
            if ratio >= 1.0:
                alertas_presupuesto.append({
                    'nivel': 'danger',
                    'texto': f"Límite superado en {gasto['categoria']}: {format_currency(gasto['total'])} / {format_currency(presu)}"
                })
            elif ratio >= 0.8:
                alertas_presupuesto.append({
                    'nivel': 'warning',
                    'texto': f"Atención: {ratio*100:.0f}% consumido en {gasto['categoria']}"
                })

    runway = None
    media = query_db(
        "SELECT AVG(total) as media FROM ("
        "SELECT strftime('%Y-%m', fecha) AS mes, SUM(importe) as total "
        "FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' "
        "GROUP BY mes ORDER BY mes DESC LIMIT 3)",
        (usuario_id,), one=True
    )
    if media and media['media'] and media['media'] > 0 and ahorro > 0:
        runway = round(ahorro / media['media'], 1)

    meta = query_db(
        "SELECT nombre, cantidad_objetivo FROM metas WHERE usuario_id = ? ORDER BY id DESC LIMIT 1",
        (usuario_id,), one=True
    )

    ahorro_historico = query_db(
        "SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE -importe END), 0) as total "
        "FROM movimientos WHERE usuario_id = ?",
        (usuario_id,), one=True
    )
    ahorro_acumulado = ahorro_historico['total'] if ahorro_historico else 0

    return render_template(
        'dashboard.html',
        periodo=periodo,
        nombre=session.get('nombre'),
        ingresos=format_currency(ingresos),
        gastos=format_currency(gastos),
        ahorro=format_currency(ahorro),
        ahorro_numerico=ahorro,
        ahorro_acumulado=ahorro_acumulado,
        meta_nombre=meta['nombre'] if meta else None,
        meta_objetivo=meta['cantidad_objetivo'] if meta else 0,
        ratio_ahorro=f"{ratio_ahorro:.1f}%",
        movimientos=movimientos,
        runway=runway,
        alertas_presupuesto=alertas_presupuesto,
        chart_saldo_tiempo={"labels": saldo_fechas, "data": saldo_valores}
    )

# =================================================================
# PANEL PRIVADO - AÑADIR, TRANSACTIONS, ANALYTICS, HISTORICO
# =================================================================
@app.route('/panel/anadir', methods=['GET', 'POST'])
@login_required
def anadir():
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Error de seguridad. Recarga la página.', 'danger')
            return redirect(url_for('anadir'))

        tipo = request.form['tipo']
        fecha = request.form['fecha']
        concepto = request.form['concepto'].strip()
        categoria = request.form.get('categoria', 'Otros')

        try:
            importe = float(request.form['importe'])
            if importe <= 0:
                raise ValueError
        except (ValueError, TypeError):
            flash('El importe debe ser un número mayor que cero.', 'danger')
            return redirect(url_for('anadir'))

        try:
            fecha_validada = datetime.datetime.strptime(fecha, '%Y-%m-%d').date()
            if fecha_validada > datetime.date.today():
                flash('No es posible registrar movimientos en fechas futuras.', 'danger')
                return redirect(url_for('anadir'))
        except ValueError:
            flash('Formato de fecha incorrecto.', 'danger')
            return redirect(url_for('anadir'))

        execute_db(
            "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)",
            (session['usuario_id'], fecha, concepto, categoria, importe, tipo)
        )
        flash('Transacción registrada con éxito.', 'success')
        return redirect(url_for('dashboard'))

    categorias_bd = query_db(
        "SELECT DISTINCT categoria FROM movimientos WHERE usuario_id = ?", (session['usuario_id'],))
    categorias = [c['categoria'] for c in categorias_bd] if categorias_bd else []
    return render_template('anadir.html', categorias=categorias)

@app.route('/panel/transactions')
@login_required
def transactions():
    page = request.args.get('page', 1, type=int)
    per_page = 25
    offset = (page - 1) * per_page

    total_row = query_db(
        "SELECT COUNT(*) as cnt FROM movimientos WHERE usuario_id = ?",
        (session['usuario_id'],), one=True
    )
    total = total_row['cnt'] if total_row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    movimientos = query_db(
        "SELECT * FROM movimientos WHERE usuario_id = ? ORDER BY fecha DESC, id DESC LIMIT ? OFFSET ?",
        (session['usuario_id'], per_page, offset)
    )
    return render_template('transactions.html',
        movimientos=movimientos,
        page=page,
        total_pages=total_pages
    )

@app.route('/panel/analytics', methods=['GET'])
@login_required
def analytics():
    usuario_id = session['usuario_id']
    hoy = datetime.date.today()

    periodo_req = request.args.get('periodo')
    if periodo_req:
        session['periodo'] = periodo_req
    periodo = session.get('periodo', 'mes')

    if periodo == 'trimestre':
        fecha_inicio = (hoy - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
        agrupacion = "strftime('%Y-%m', fecha)"
    elif periodo == 'anio':
        fecha_inicio = hoy.replace(month=1, day=1).strftime('%Y-%m-%d')
        agrupacion = "strftime('%Y-%m', fecha)"
    else:
        fecha_inicio = hoy.replace(day=1).strftime('%Y-%m-%d')
        agrupacion = "fecha"

    fecha_fin = hoy.strftime('%Y-%m-%d')

    resultado = query_db(
        "SELECT COALESCE(SUM(CASE WHEN tipo='ingreso' THEN importe ELSE 0 END), 0) as ingresos, "
        "COALESCE(SUM(CASE WHEN tipo='gasto' THEN importe ELSE 0 END), 0) as gastos "
        "FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ?",
        (usuario_id, fecha_inicio, fecha_fin), one=True
    )
    ingresos = resultado['ingresos'] if resultado else 0
    gastos = resultado['gastos'] if resultado else 0
    ahorro = ingresos - gastos

    runway = None
    media = query_db(
        "SELECT AVG(total) as media FROM ("
        "SELECT strftime('%Y-%m', fecha) AS mes, SUM(importe) as total "
        "FROM movimientos WHERE usuario_id = ? AND tipo = 'gasto' "
        "GROUP BY mes ORDER BY mes DESC LIMIT 3)",
        (usuario_id,), one=True
    )
    if media and media['media'] and media['media'] > 0 and ahorro > 0:
        runway = round(ahorro / media['media'], 1)

    datos_agrupados = query_db(
        f"SELECT {agrupacion} as label, "
        f"SUM(CASE WHEN tipo='ingreso' THEN importe ELSE 0 END) as ingresos, "
        f"SUM(CASE WHEN tipo='gasto' THEN importe ELSE 0 END) as gastos "
        f"FROM movimientos WHERE usuario_id = ? AND fecha >= ? AND fecha <= ? "
        f"GROUP BY label ORDER BY label",
        (usuario_id, fecha_inicio, fecha_fin)
    )

    labels = [d['label'] for d in datos_agrupados] if datos_agrupados else []
    datos_ingresos = [d['ingresos'] for d in datos_agrupados] if datos_agrupados else []
    datos_gastos = [d['gastos'] for d in datos_agrupados] if datos_agrupados else []
    datos_ahorro = [(d['ingresos'] - d['gastos']) for d in datos_agrupados] if datos_agrupados else []

    datos_categorias = query_db(
        "SELECT categoria, SUM(importe) as total FROM movimientos "
        "WHERE usuario_id = ? AND tipo = 'gasto' AND fecha >= ? AND fecha <= ? "
        "GROUP BY categoria ORDER BY total DESC",
        (usuario_id, fecha_inicio, fecha_fin)
    )
    labels_categorias = [d['categoria'] for d in datos_categorias] if datos_categorias else []
    valores_categorias = [d['total'] for d in datos_categorias] if datos_categorias else []
    chart_gastos_categoria = {"labels": labels_categorias, "data": valores_categorias}

    return render_template(
        'analytics.html',
        rango=periodo,
        ingresos=format_currency(ingresos),
        gastos=format_currency(gastos),
        ahorro=format_currency(ahorro),
        ahorro_numerico=ahorro,
        ingresos_numerico=ingresos,
        runway=runway,
        chart_ahorro_mes={"labels": labels, "data": datos_ahorro},
        chart_ingresos_mes={"labels": labels, "data": datos_ingresos},
        chart_gastos_mes={"labels": labels, "data": datos_gastos},
        chart_gastos_categoria=chart_gastos_categoria
    )

@app.route('/panel/historico')
@login_required
def historico():
    usuario_id = session['usuario_id']
    resumen_mensual = query_db(
        "SELECT strftime('%Y-%m', fecha) as mes, "
        "SUM(CASE WHEN tipo='ingreso' THEN importe ELSE 0 END) as ingresos, "
        "SUM(CASE WHEN tipo='gasto' THEN importe ELSE 0 END) as gastos "
        "FROM movimientos WHERE usuario_id = ? "
        "GROUP BY mes ORDER BY mes DESC",
        (usuario_id,)
    )
    return render_template('historico.html',
        resumen_mensual=resumen_mensual if resumen_mensual else [],
        grafico_ahorro_mes=None
    )

# =================================================================
# SETTINGS
# =================================================================
@app.route('/panel/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        if not validate_csrf(request.form.get('csrf_token')):
            flash('Error de seguridad. Recarga la página.', 'danger')
            return redirect(url_for('settings'))

        accion = request.form.get('accion')

        if accion == 'guardar_presupuestos':
            execute_db("DELETE FROM presupuestos WHERE usuario_id = ?", (session['usuario_id'],))
            for k, v in request.form.items():
                if k.startswith('presu_') and v.strip():
                    try:
                        val = float(v)
                        if val > 0:
                            categoria = k.replace('presu_', '')
                            execute_db(
                                "INSERT INTO presupuestos (usuario_id, categoria, importe_mensual) VALUES (?, ?, ?)",
                                (session['usuario_id'], categoria, val)
                            )
                    except ValueError:
                        pass
            flash('Presupuestos actualizados.', 'success')

        elif accion == 'nueva_meta':
            meta_nombre = request.form.get('meta_nombre', '').strip()
            try:
                meta_objetivo = float(request.form.get('meta_objetivo', 0))
                if meta_nombre and meta_objetivo > 0:
                    execute_db(
                        "INSERT INTO metas (usuario_id, nombre, cantidad_objetivo, cantidad_actual) VALUES (?, ?, ?, 0)",
                        (session['usuario_id'], meta_nombre, meta_objetivo)
                    )
                    flash('Objetivo creado con éxito.', 'success')
                else:
                    flash('El nombre y el importe son obligatorios.', 'danger')
            except (ValueError, TypeError):
                flash('El importe del objetivo no es válido.', 'danger')

        elif accion == 'borrar_meta':
            meta_id = request.form.get('meta_id')
            if meta_id:
                execute_db("DELETE FROM metas WHERE id = ? AND usuario_id = ?",
                           (meta_id, session['usuario_id']))
                flash('Meta eliminada.', 'success')

        elif accion == 'guardar_perfil':
            nombrevisible = request.form.get('nombrevisible', '').strip()
            if nombrevisible:
                execute_db("UPDATE usuarios SET nombre = ? WHERE id = ?",
                           (nombrevisible, session['usuario_id']))
                session['nombre'] = nombrevisible
                flash('Perfil actualizado.', 'success')

        return redirect(url_for('settings'))

    usuario = query_db("SELECT email FROM usuarios WHERE id = ?", (session['usuario_id'],), one=True)
    cat_db = query_db("SELECT DISTINCT categoria FROM movimientos WHERE usuario_id = ?", (session['usuario_id'],))
    categorias_usadas = [row['categoria'] for row in cat_db] if cat_db else []

    return render_template(
        'settings.html',
        nombre=session.get('nombre'),
        email_real=usuario['email'] if usuario else '',
        presupuestos=query_db(
            "SELECT categoria, importe_mensual FROM presupuestos WHERE usuario_id = ? ORDER BY categoria",
            (session['usuario_id'],)
        ),
        metas=query_db(
            "SELECT id, nombre, cantidad_objetivo FROM metas WHERE usuario_id = ? ORDER BY id DESC",
            (session['usuario_id'],)
        ),
        categorias_usadas=categorias_usadas
    )

@app.route('/panel/settings/preferencias', methods=['POST'])
@login_required
def guardar_preferencias():
    data = request.json
    session['tema'] = data.get('tema', 'dark')
    session['privacidad'] = 1 if data.get('privacidad') else 0
    session['animaciones'] = 1 if data.get('animaciones') else 0
    return jsonify({"status": "success"})

# =================================================================
# ACCIONES SOBRE MOVIMIENTOS
# =================================================================
@app.route('/panel/borrar/<int:movimiento_id>', methods=['POST'])
@login_required
def borrar(movimiento_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Error de seguridad.', 'danger')
        return redirect(url_for('transactions'))
    execute_db("DELETE FROM movimientos WHERE id = ? AND usuario_id = ?",
               (movimiento_id, session['usuario_id']))
    flash('Transacción eliminada.', 'success')
    return redirect(request.referrer or url_for('transactions'))

@app.route('/panel/duplicar/<int:movimiento_id>', methods=['POST'])
@login_required
def duplicar(movimiento_id):
    if not validate_csrf(request.form.get('csrf_token')):
        flash('Error de seguridad.', 'danger')
        return redirect(url_for('transactions'))
    mov = query_db(
        "SELECT fecha, concepto, categoria, importe, tipo FROM movimientos WHERE id = ? AND usuario_id = ?",
        (movimiento_id, session['usuario_id']), one=True
    )
    if mov:
        execute_db(
            "INSERT INTO movimientos (usuario_id, fecha, concepto, categoria, importe, tipo) VALUES (?, ?, ?, ?, ?, ?)",
            (session['usuario_id'], datetime.date.today().strftime('%Y-%m-%d'),
             f"{mov['concepto']} (Copia)", mov['categoria'], mov['importe'], mov['tipo'])
        )
        flash('Registro duplicado con éxito.', 'success')
    return redirect(url_for('transactions'))

# =================================================================
# EXPORTACIÓN CSV
# =================================================================
@app.route('/exportar_csv')
@login_required
def exportar_csv():
    movimientos = query_db(
        "SELECT fecha, tipo, concepto, categoria, importe FROM movimientos "
        "WHERE usuario_id = ? ORDER BY fecha DESC",
        (session['usuario_id'],)
    )
    if not movimientos:
        flash('No hay movimientos para exportar.', 'warning')
        return redirect(request.referrer or url_for('transactions'))

    si = io.StringIO()
    cw = csv.writer(si, delimiter=';', lineterminator='\n')
    cw.writerow(['Fecha', 'Tipo', 'Concepto', 'Categoria', 'Importe (EUR)'])
    for row in movimientos:
        importe_str = f"{row['importe']:.2f}".replace('.', ',')
        cw.writerow([row['fecha'], row['tipo'].upper(), row['concepto'], row['categoria'], importe_str])

    output = si.getvalue().encode('utf-8-sig')
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=findash_{datetime.date.today().strftime('%Y%m%d')}.csv"}
    )

if __name__ == '__main__':
    app.run(debug=True)