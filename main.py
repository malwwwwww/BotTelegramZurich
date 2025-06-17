import subprocess
import time
import os
import asyncio
import json
import ipaddress
import logging
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from telegram import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler

#HOSTS INICALES 
from hosts import hosts_cctv, hosts_servers, hosts_switches, hosts_corporativo

# ================= CONFIGURACIÓN =================
# CONFIGURACION DE ARCHIOVS PARA LOGS 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Cargar token desde variable de entorno
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("No se encontró BOT_TOKEN en las variables de entorno")
    raise ValueError("BOT_TOKEN no configurado")

# Archivos de configuración
CREDENCIALES_FILE = "credenciales.json"
ARCHIVO_PERSISTENCIA = "hosts_persistentes.json"
ARCHIVO_RESPALDO = "hosts_persistentes_backup.json"

# Intervalo de ping por grupo (en segundos)
INTERVALOS_PING = {
    "cctv": 5,
    "servers": 5,
    "switches": 5,  # Aumentado para más tolerancia
    "corporativo": 5
}

# Intervalo para alertas persistentes
INTERVALO_ALERTA_PERSISTENTE = 600  # segundos

# Estados para ConversationHandler
LOGIN = 0
GRUPO, IP, NOMBRE, CONFIRMAR_AGREGAR = range(4)
ELIMINAR_GRUPO, ELIMINAR_IP, CONFIRMAR_ELIMINAR = range(3)

# ================= ESTRUCTURA DE DATOS =================
hosts = {
    "cctv": dict(hosts_cctv),
    "servers": dict(hosts_servers),
    "switches": dict(hosts_switches),
    "corporativo": dict(hosts_corporativo)
}

estados = {
    grupo: {
        "activo": True,
        "estado_hosts": {ip: {"activo": True, "fallos": 0, "ultima_alerta": 0} for ip in datos_hosts}
    } for grupo, datos_hosts in hosts.items()
}

sesiones_activas = {}
monitoreo_global = False
data_lock = threading.Lock()
alert_queue = queue.Queue()
MAX_FALLOS = 5  # Aumentado de 3 a 5 para más tolerancia

# ================= FUNCIONES BÁSICAS =================
def ping(ip):
    """Ejecuta un ping a una IP con timeout extendido y asi evitar falsos negativos."""
    try:
        ip_addr = ipaddress.ip_address(ip)
        param = "-n" if os.name == "nt" else "-c"
        command = ["ping6" if ip_addr.version == 6 else "ping", param, "1", "-w", "2000", ip]  # Timeout a 2000 ms
        logger.debug(f"Ejecutando comando: {' '.join(command)}")
        result = subprocess.call(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        logger.info(f"[PING]{ip}--> {'OK' if result == 0 else 'FALLÓ'}")
        return result == 0
    except (subprocess.SubprocessError, ValueError) as e:
        logger.error(f"ERROR en ping a {ip}: {e}")
        return False

async def enviar_mensaje(app, chat_id, texto, parse_mode="Markdown", **kwargs):
    """Envía un mensaje a Telegram con formato."""
    try:
        max_length = 4096
        if len(texto) > max_length:
            for i in range(0, len(texto), max_length):
                await app.bot.send_message(chat_id=chat_id, text=texto[i:i + max_length], parse_mode=parse_mode, **kwargs)
        else:
            await app.bot.send_message(chat_id=chat_id, text=texto, parse_mode=parse_mode, **kwargs)
        logger.info(f"MENSAJE enviado a {chat_id}: {texto[:50]}...")
    except Exception as e:
        logger.error(f"ERROR enviando MENSAJE a {chat_id}: {e}")

async def procesar_alertas(app):
    """Procesa alertas y las envía a todos los usuarios autenticados."""
    while True:
        try:
            mensaje = alert_queue.get_nowait()
            with data_lock:
                for chat_id in sesiones_activas:
                    asyncio.create_task(enviar_mensaje(app, chat_id, mensaje))
            alert_queue.task_done()
        except queue.Empty:
            await asyncio.sleep(0.1)

# ================= PERSISTENCIA =================
def cargar_credenciales():
    """Carga credenciales desde el archivo JSON."""
    try:
        with open(CREDENCIALES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Error cargando credenciales: {e}")
        raise FileNotFoundError("Crea un archivo credenciales.json con identificadores válidos")

def guardar_hosts():
    """Guarda hosts en el archivo JSON con respaldo."""
    try:
        logger.debug("Adquiriendo data_lock para guardar_hosts")
        with data_lock:
            logger.debug("data_lock adquirido")
            # Copia manual para evitar bloqueos con shutil.copyfile
            if os.path.exists(ARCHIVO_PERSISTENCIA):
                with open(ARCHIVO_PERSISTENCIA, "rb") as src, open(ARCHIVO_RESPALDO, "wb") as dst:
                    dst.write(src.read())
                logger.debug(f"Respaldo creado en {ARCHIVO_RESPALDO}")
            with open(ARCHIVO_PERSISTENCIA, "w", encoding="utf-8") as f:
                json.dump(hosts, f, indent=2)
            logger.info("Hosts guardados correctamente")
        logger.debug("data_lock liberado")
    except (OSError, IOError) as e:
        logger.error(f"Error de E/S en guardar_hosts: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Error inesperado en guardar_hosts: {e}", exc_info=True)
        raise

def cargar_hosts():
    """Carga hosts desde el archivo JSON."""
    try:
        with data_lock:
            with open(ARCHIVO_PERSISTENCIA, "r", encoding="utf-8") as f:
                datos = json.load(f)
                for grupo in hosts:
                    if grupo in datos:
                        hosts[grupo].update(datos[grupo])
                        estados[grupo]["estado_hosts"].update({ip: {"activo": True, "fallos": 0, "ultima_alerta": 0} for ip in datos[grupo]})
            logger.info("Hosts cargados desde persistencia")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Usando hosts iniciales de hosts.py")

# ================= MONITOREO CON HILOS =================
def monitoreo_host(ip, nombre, grupo):
    """Monitorea un host individual."""
    estado_actual = ping(ip)
    current_time = time.time()
    with data_lock:
        estado_anterior = estados[grupo]["estado_hosts"][ip]["activo"]
        fallos = estados[grupo]["estado_hosts"][ip]["fallos"]
        ultima_alerta = estados[grupo]["estado_hosts"][ip]["ultima_alerta"]
        
        if not estado_actual:
            fallos += 1
            if fallos >= MAX_FALLOS:
                if estado_anterior:
                    estados[grupo]["estado_hosts"][ip]["activo"] = False
                    estados[grupo]["estado_hosts"][ip]["ultima_alerta"] = current_time
                    mensaje = f"🚨 *[{grupo.upper()}] ¡ALERTA!*\n`{nombre}` ({ip}) NO responde.\n---"
                    alert_queue.put(mensaje)
                    logger.warning(f"Host {nombre} ({ip}) en {grupo} no responde tras {fallos} intentos")
                elif current_time - ultima_alerta >= INTERVALO_ALERTA_PERSISTENTE:
                    estados[grupo]["estado_hosts"][ip]["ultima_alerta"] = current_time
                    mensaje = f"🚨 *[{grupo.upper()}] ¡ALERTA PERSISTENTE!*\n`{nombre}` ({ip}) sigue sin responder.\n---"
                    alert_queue.put(mensaje)
                    logger.warning(f"Host {nombre} ({ip}) en {grupo} sigue sin responder")
        else:
            if not estado_anterior:
                estados[grupo]["estado_hosts"][ip]["activo"] = True
                estados[grupo]["estado_hosts"][ip]["ultima_alerta"] = current_time
                mensaje = f"✅ *[{grupo.upper()}] RECUPERADO*\n`{nombre}` ({ip}) ha vuelto a responder.\n---"
                alert_queue.put(mensaje)
                logger.info(f"Host {nombre} ({ip}) en {grupo} volvió a responder")
            fallos = 0
        
        estados[grupo]["estado_hosts"][ip]["fallos"] = fallos

def monitoreo_grupo_thread(grupo, executor):
    """Monitorea un grupo de hosts usando un ThreadPoolExecutor."""
    logger.info(f"Iniciando monitoreo para {grupo}")
    while True:
        with data_lock:
            if not estados[grupo]["activo"] or not monitoreo_global:
                logger.info(f"Deteniendo monitoreo de {grupo}")
                break
            hosts_copy = hosts[grupo].copy()
        
        futures = []
        for ip, nombre in hosts_copy.items():
            futures.append(executor.submit(monitoreo_host, ip, nombre, grupo))
        
        for future in futures:
            future.result()
        
        time.sleep(INTERVALOS_PING[grupo])

# ================= TECLADOS =================
def teclado_principal(es_admin=False):
    """Teclado principal del bot, con opción extra para admins."""
    botones = [
        ["🟢 Iniciar todo", "🔴 Detener todo"],
        ["📊 Estado general", "📋 Listar sesiones"],
        ["🟢 Hosts activos", "🔴 Hosts inactivos"],
        ["➕ Agregar Host", "🗑 Eliminar Host"],
        ["⚙ Control por grupo", "🚪 Cerrar sesión"]
    ]
    if es_admin:
        botones.insert(4, ["🛡️ Cerrar sesiones no admin"])
    return ReplyKeyboardMarkup(botones, resize_keyboard=True)

def teclado_grupos():
    """Teclado para seleccionar grupos."""
    return ReplyKeyboardMarkup([
        ["📷 CCTV", "💻 Servidores"],
        ["🔌 Switches", "🏢 Corporativo"],
        ["🔑 Menú principal"]
    ], resize_keyboard=True)

def teclado_confirmar():
    """Teclado para confirmar o cancelar una acción."""
    return ReplyKeyboardMarkup([
        ["✅ Confirmar", "❌ Cancelar"]
    ], resize_keyboard=True)

# ================= HANDLERS DE LOGIN =================
async def start(update: Update, context):
    """Inicia el flujo de login solicitando el identificador."""
    chat_id = update.message.chat_id
    logger.info(f"Recibido /start de chat_id {chat_id}")
    if chat_id in sesiones_activas:
        es_admin = sesiones_activas[chat_id].get("rol") == "admin"
        await update.message.reply_text(
            "✅ *Ya estás autenticado*\nUsa los botones para interactuar con el bot.",
            reply_markup=teclado_principal(es_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "🔐 *Ingresa tu identificador:*\nEjemplo: `clave123`",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    logger.info(f"Inicio de login para chat_id {chat_id}")
    return LOGIN

async def verificar_identificador(update: Update, context):
    """Verifica el identificador ingresado."""
    chat_id = update.message.chat_id
    identificador = update.message.text.strip()
    logger.info(f"Verificando identificador para chat_id {chat_id}: {identificador}")
    credenciales = cargar_credenciales()
    
    for cred in credenciales:
        if identificador == cred["identificador"]:
            sesiones_activas[chat_id] = {
                "identificador": identificador,
                "rol": cred.get("rol", "user"),
                "timestamp": time.time()
            }
            es_admin = cred.get("rol", "user") == "admin"
            await update.message.reply_text(
                "✅ *Autenticación exitosa*\nUsa los botones para gestionar el monitoreo:\n- 🟢 Iniciar/detener\n- 📊 Ver estado\n- ➕ Agregar/eliminar hosts",
                reply_markup=teclado_principal(es_admin),
                parse_mode="Markdown"
            )
            logger.info(f"Login exitoso para chat_id {chat_id} con identificador {identificador}, rol: {cred.get('rol', 'user')}")
            return ConversationHandler.END
    
    await update.message.reply_text(
        "❌ *Identificador incorrecto*\nIngresa un identificador válido.\nEjemplo: `clave123`",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    logger.warning(f"Intento de login fallido para chat_id {chat_id}")
    return LOGIN

async def cancelar_login(update: Update, context):
    """Cancela el flujo de login."""
    await update.message.reply_text("❌ *Login cancelado*", parse_mode="Markdown")
    logger.info(f"Login cancelado para chat_id {update.message.chat_id}")
    return ConversationHandler.END

# ================= HANDLERS DE MENSAJES =================
async def manejar_mensaje(update: Update, context):
    """Maneja los mensajes de texto recibidos."""
    global monitoreo_global
    texto = update.message.text
    chat_id = update.message.chat_id
    app = context.application
    logger.info(f"Mensaje recibido de chat_id {chat_id}: {texto}")
    
    if chat_id not in sesiones_activas:
        await update.message.reply_text("❌ *Usa /start para autenticarte*", parse_mode="Markdown")
        return

    es_admin = sesiones_activas[chat_id].get("rol") == "admin"
    executor = context.user_data.get("executor", ThreadPoolExecutor(max_workers=20))
    context.user_data["executor"] = executor

    if texto == "🟢 Iniciar todo":
        with data_lock:
            monitoreo_global = True
            logger.info(f"Monitoreo global iniciado por {chat_id}")
        for grupo in estados:
            with data_lock:
                if estados[grupo]["activo"]:
                    logger.info(f"Creando hilo de monitoreo para {grupo}")
                    threading.Thread(target=monitoreo_grupo_thread, args=(grupo, executor), daemon=True).start()
        await update.message.reply_text("✅ *Monitoreo iniciado*", parse_mode="Markdown")
        logger.info(f"Monitoreo global iniciado por {chat_id}")

    elif texto == "🔴 Detener todo":
        with data_lock:
            monitoreo_global = False
        await update.message.reply_text("🛑 *Monitoreo global DETENIDO*", parse_mode="Markdown")
        logger.info(f"Monitoreo global detenido por {chat_id}")

    elif texto == "📊 Estado general":
        mensaje = "*🌐 Estado del Bot*\n\n"
        with data_lock:
            for grupo in estados:
                activos = sum(1 for ip in estados[grupo]["estado_hosts"] if estados[grupo]["estado_hosts"][ip]["activo"])
                total = len(estados[grupo]["estado_hosts"])
                estado = "🟢 Activado" if estados[grupo]["activo"] else "🔴 Desactivado"
                mensaje += f"*{grupo.upper()}* ({estado})\n  - Hosts: {activos}/{total} activos\n"
        mensaje += f"\n*Monitoreo global*: {'🟢 Activado' if monitoreo_global else '🔴 Desactivado'}"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
        logger.info(f"Estado general solicitado por {chat_id}")

    elif texto == "🟢 Hosts activos":
        mensaje = "*🟢 Hosts Activos*\n\n"
        with data_lock:
            for grupo in estados:
                mensaje += f"*{grupo.upper()}*\n"
                activos = [f"  `{name}` ({ip})" for ip, name in hosts[grupo].items() if estados[grupo]["estado_hosts"][ip]["activo"]]
                mensaje += "\n".join(activos) + "\n\n" if activos else "  - Ninguno\n\n"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
        logger.info(f"Hosts activos solicitados por {chat_id}")

    elif texto == "🔴 Hosts inactivos":
        mensaje = "*🔴 Hosts Inactivos*\n\n"
        with data_lock:
            for grupo in estados:
                mensaje += f"*{grupo.upper()}*\n"
                inactivos = [f"  `{name}` ({ip})" for ip, name in hosts[grupo].items() if not estados[grupo]["estado_hosts"][ip]["activo"]]
                mensaje += "\n".join(inactivos) + "\n\n" if inactivos else "  - Ninguno\n\n"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
        logger.info(f"Hosts inactivos solicitados por {chat_id}")

    elif texto == "📋 Listar sesiones":
        with data_lock:
            if not sesiones_activas:
                await update.message.reply_text("*📋 Sesiones*\n\nNinguna sesión activa", parse_mode="Markdown")
                return
            mensaje = "*📋 Sesiones Activas*\n\n"
            for sid, sesion in sesiones_activas.items():
                tiempo = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sesion["timestamp"]))
                rol = sesion.get("rol", "user")
                mensaje += f"*ID*: {sid}\n*Identificador*: `{sesion['identificador']}`\n*Rol*: {rol}\n*Conexión*: {tiempo}\n\n"
        await update.message.reply_text(mensaje, parse_mode="Markdown")
        logger.info(f"Sesiones activas solicitadas por {chat_id}")

    elif texto == "🛡️ Cerrar sesiones no admin" and es_admin:
        with data_lock:
            sesiones_a_cerrar = [sid for sid, sesion in sesiones_activas.items() if sesion.get("rol", "user") != "admin"]
            for sid in sesiones_a_cerrar:
                del sesiones_activas[sid]
                await enviar_mensaje(app, sid, "⚠️ *Tu sesión ha sido cerrada por un administrador.*", parse_mode="Markdown")
                logger.info(f"Sesión {sid} cerrada por admin {chat_id}")
        await update.message.reply_text(f"✅ *{len(sesiones_a_cerrar)} sesiones no admin cerradas*", parse_mode="Markdown")
        logger.info(f"{len(sesiones_a_cerrar)} sesiones no admin eliminadas por {chat_id}")

    elif texto == "⚙ Control por grupo":
        await update.message.reply_text("Selecciona grupo:", reply_markup=teclado_grupos(), parse_mode="Markdown")
        logger.info(f"Control por grupo solicitado por {chat_id}")

    elif texto in ["📷 CCTV", "💻 Servidores", "🔌 Switches", "🏢 Corporativo"]:
        grupo = texto.lower().replace("📷 ", "").replace("💻 ", "").replace("🔌 ", "").replace("🏢 ", "").strip()
        with data_lock:
            estados[grupo]["activo"] = not estados[grupo]["activo"]
            estado = "ACTIVADO" if estados[grupo]["activo"] else "DESACTIVADO"
            if estados[grupo]["activo"] and monitoreo_global:
                logger.info(f"Creando hilo de monitoreo para {grupo}")
                threading.Thread(target=monitoreo_grupo_thread, args=(grupo, executor), daemon=True).start()
        await update.message.reply_text(f"✅ *Monitoreo de {grupo.upper()} {estado}*", parse_mode="Markdown")
        logger.info(f"Monitoreo de {grupo} {estado.lower()} por {chat_id}")

    elif texto == "🚪 Cerrar sesión":
        with data_lock:
            if chat_id in sesiones_activas:
                del sesiones_activas[chat_id]
        await update.message.reply_text("✅ *Sesión cerrada*", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        logger.info(f"Sesión cerrada por {chat_id}")

    elif texto == "🔑 Menú principal":
        await update.message.reply_text(
            "*Menú Principal:*",
            reply_markup=teclado_principal(es_admin),
            parse_mode="Markdown"
        )
        logger.info(f"Menú principal solicitado por {chat_id}")

# ================= CONVERSATION HANDLERS =================
async def agregar_host(update: Update, context):
    """Inicia el flujo para agregar un host."""
    chat_id = update.message.chat_id
    if chat_id not in sesiones_activas:
        await update.message.reply_text("❌ *Usa /start para autenticarte*", parse_mode="Markdown")
        return ConversationHandler.END
    logger.info(f"Botón 'Agregar Host' presionado por {chat_id}")
    await update.message.reply_text(
        "➕ *Agregar Host*\nSelecciona un grupo para continuar:\n(e.g., CCTV, Servidores)\n\n"
        "Usa /cancel o '🔑 Menú principal' para salir.\n\n*Instrucciones completas*:\n"
        "1. Selecciona un grupo.\n2. Ingresa la IP (e.g., `192.168.1.10`).\n"
        "3. Ingresa el nombre (e.g., `Camara1`).",
        reply_markup=teclado_grupos(),
        parse_mode="Markdown"
    )
    return GRUPO

async def recibir_grupo(update: Update, context):
    """Recibe el grupo seleccionado para agregar un host."""
    texto = update.message.text.lower().replace("📷 ", "").replace("💻 ", "").replace("🔌 ", "").replace("🏢 ", "").strip()
    chat_id = update.message.chat_id
    logger.info(f"Grupo recibido por {chat_id}: {texto}")
    if texto == "menú principal":
        es_admin = sesiones_activas.get(chat_id, {}).get("rol") == "admin"
        await update.message.reply_text(
            "*Menú Principal:*",
            reply_markup=teclado_principal(es_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    if texto not in hosts:
        await update.message.reply_text(
            "❌ *Grupo inválido*\nSelecciona un grupo válido (CCTV, Servidores, Switches, Corporativo):",
            reply_markup=teclado_grupos(),
            parse_mode="Markdown"
        )
        return GRUPO
    context.user_data["grupo"] = texto
    await update.message.reply_text(
        f"📌 *Ingresa la IP para {texto.upper()}*\nEjemplo: `192.168.1.10` o `2001:db8::1`\nUsa /cancel para salir.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return IP

async def recibir_ip(update: Update, context):
    """Recibe y valida la IP del host."""
    ip = update.message.text.strip()
    grupo = context.user_data.get("grupo")
    chat_id = update.message.chat_id
    logger.info(f"IP recibida por {chat_id} para {grupo}: {ip}")
    try:
        ip_addr = ipaddress.ip_address(ip)
        if ip in hosts[grupo]:
            await update.message.reply_text(
                f"❌ *IP `{ip}` ya existe en {grupo.upper()}*\nIngresa una IP diferente.\nEjemplo: `192.168.1.10`",
                parse_mode="Markdown"
            )
            return IP
        context.user_data["ip"] = str(ip_addr)
        await update.message.reply_text(
            f"📌 *Ingresa el nombre para `{ip}`*\nMáx. 50 caracteres. Ejemplo: `Camara1`\nUsa /cancel para salir.",
            parse_mode="Markdown"
        )
        return NOMBRE
    except ValueError:
        await update.message.reply_text(
            "❌ *IP inválida*\nIngresa una IP válida (e.g., `192.168.1.10` o `2001:db8::1`):",
            parse_mode="Markdown"
        )
        return IP

async def recibir_nombre(update: Update, context):
    """Recibe el nombre del host y pide confirmación."""
    nombre = update.message.text.strip()
    chat_id = update.message.chat_id
    logger.info(f"Nombre recibido por {chat_id}: {nombre}")
    if not nombre or len(nombre) > 50:
        await update.message.reply_text(
            "❌ *Nombre inválido*\nIngresa un nombre entre 1 y 50 caracteres.\nEjemplo: `Camara1`",
            parse_mode="Markdown"
        )
        return NOMBRE
    context.user_data["nombre"] = nombre
    grupo = context.user_data.get("grupo")
    ip = context.user_data.get("ip")
    await update.message.reply_text(
        f"*Confirmación*\n\nVas a agregar:\n- *Grupo*: {grupo.upper()}\n- *IP*: `{ip}`\n- *Nombre*: `{nombre}`\n\n¿Confirmas?",
        reply_markup=teclado_confirmar(),
        parse_mode="Markdown"
    )
    return CONFIRMAR_AGREGAR

async def confirmar_agregar(update: Update, context):
    """Confirma o cancela la adición del host."""
    texto = update.message.text
    chat_id = update.message.chat_id
    logger.info(f"Confirmación de agregar recibida por {chat_id}: {texto}")
    
    try:
        es_admin = sesiones_activas.get(chat_id, {}).get("rol") == "admin"
        logger.debug(f"Datos en context.user_data: {context.user_data}")
        
        if texto == "❌ Cancelar":
            logger.info(f"Adición de host cancelada por {chat_id}")
            await update.message.reply_text(
                "❌ *Operación cancelada*",
                reply_markup=teclado_principal(es_admin),
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        
        if texto == "✅ Confirmar":
            grupo = context.user_data.get("grupo")
            ip = context.user_data.get("ip")
            nombre = context.user_data.get("nombre")
            
            if not all([grupo, ip, nombre]):
                logger.error(f"Datos incompletos en confirmar_agregar por {chat_id}: grupo={grupo}, ip={ip}, nombre={nombre}")
                await update.message.reply_text(
                    "❌ *Error: Datos incompletos*\nPor favor, inicia el proceso de nuevo.",
                    reply_markup=teclado_principal(es_admin),
                    parse_mode="Markdown"
                )
                return ConversationHandler.END
            
            logger.info(f"Intentando agregar host: {nombre} ({ip}) a {grupo} por {chat_id}")
            with data_lock:
                hosts[grupo][ip] = nombre
                estados[grupo]["estado_hosts"][ip] = {"activo": True, "fallos": 0, "ultima_alerta": 0}
            logger.debug(f"Antes de guardar_hosts para {ip} en {grupo}")
            await asyncio.to_thread(guardar_hosts)  # Ejecutar en hilo separado
            logger.debug(f"Después de guardar_hosts para {ip} en {grupo}")
            
            await update.message.reply_text(
                f"✅ *Host `{nombre}` ({ip}) agregado a {grupo.upper()}*",
                reply_markup=teclado_principal(es_admin),
                parse_mode="Markdown"
            )
            logger.info(f"Host {nombre} ({ip}) agregado a {grupo} por {chat_id}")
            return ConversationHandler.END
        
        logger.warning(f"Opción inválida en confirmar_agregar por {chat_id}: {texto}")
        await update.message.reply_text(
            "❌ *Opción inválida*\nSelecciona *✅ Confirmar* o *❌ Cancelar*:",
            reply_markup=teclado_confirmar(),
            parse_mode="Markdown"
        )
        return CONFIRMAR_AGREGAR
    
    except Exception as e:
        logger.error(f"Error en confirmar_agregar por {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ *Error interno*\nPor favor, intenta de nuevo o contacta al administrador.",
            reply_markup=teclado_principal(es_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def eliminar_host(update: Update, context):
    """Inicia el flujo para eliminar un host."""
    chat_id = update.message.chat_id
    if chat_id not in sesiones_activas:
        await update.message.reply_text("❌ *Usa /start para autenticarte*", parse_mode="Markdown")
        return ConversationHandler.END
    logger.info(f"Botón 'Eliminar Host' presionado por {chat_id}")
    await update.message.reply_text(
        "🗑 *Eliminar Host*\nSelecciona un grupo para continuar:\n(e.g., CCTV, Servidores)\n\n"
        "Usa /cancel o '🔑 Menú principal' para salir.\n\n*Instrucciones completas*:\n"
        "1. Selecciona un grupo.\n2. Ingresa la IP a eliminar (e.g., `192.168.1.10`).",
        reply_markup=teclado_grupos(),
        parse_mode="Markdown"
    )
    return ELIMINAR_GRUPO

async def recibir_grupo_eliminar(update: Update, context):
    """Recibe el grupo para eliminar un host."""
    texto = update.message.text.lower().replace("📷 ", "").replace("💻 ", "").replace("🔌 ", "").replace("🏢 ", "").strip()
    chat_id = update.message.chat_id
    logger.info(f"Grupo recibido para eliminar por {chat_id}: {texto}")
    if texto == "menú principal":
        es_admin = sesiones_activas.get(chat_id, {}).get("rol") == "admin"
        await update.message.reply_text(
            "*Menú Principal:*",
            reply_markup=teclado_principal(es_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    if texto not in hosts:
        await update.message.reply_text(
            "❌ *Grupo inválido*\nSelecciona un grupo válido (CCTV, Servidores, Switches, Corporativo):",
            reply_markup=teclado_grupos(),
            parse_mode="Markdown"
        )
        return ELIMINAR_GRUPO
    context.user_data["grupo"] = texto
    mensaje = f"📌 *Ingresa la IP para eliminar de {texto.upper()}*\n\n*Hosts disponibles*:\n"
    with data_lock:
        if not hosts[texto]:
            await update.message.reply_text(
                f"❌ *No hay hosts en {texto.upper()}*\nSelecciona otro grupo:",
                reply_markup=teclado_grupos(),
                parse_mode="Markdown"
            )
            return ELIMINAR_GRUPO
        for ip, nombre in hosts[texto].items():
            mensaje += f"- `{ip}` ({nombre})\n"
    mensaje += "\nEjemplo: `192.168.1.10`\nUsa /cancel para salir."
    await update.message.reply_text(mensaje, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return ELIMINAR_IP

async def recibir_ip_eliminar(update: Update, context):
    """Recibe la IP del host y pide confirmación."""
    ip = update.message.text.strip()
    grupo = context.user_data.get("grupo")
    chat_id = update.message.chat_id
    logger.info(f"IP recibida para eliminar por {chat_id} en {grupo}: {ip}")
    with data_lock:
        if ip not in hosts[grupo]:
            mensaje = f"❌ *IP `{ip}` no encontrada en {grupo.upper()}*\n\n*Hosts disponibles*:\n"
            for host_ip, nombre in hosts[grupo].items():
                mensaje += f"- `{host_ip}` ({nombre})\n"
            mensaje += "\nIngresa una IP válida.\nEjemplo: `192.168.1.10`\nUsa /cancel para salir."
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            return ELIMINAR_IP
        nombre = hosts[grupo][ip]
    context.user_data["ip"] = ip
    context.user_data["nombre"] = nombre
    await update.message.reply_text(
        f"*Confirmación*\n\nVas a eliminar:\n- *Grupo*: {grupo.upper()}\n- *IP*: `{ip}`\n- *Nombre*: `{nombre}`\n\n¿Confirmas?",
        reply_markup=teclado_confirmar(),
        parse_mode="Markdown"
    )
    return CONFIRMAR_ELIMINAR

async def confirmar_eliminar(update: Update, context):
    """Confirma o cancela la eliminación del host."""
    texto = update.message.text
    chat_id = update.message.chat_id
    logger.info(f"Confirmación de eliminar recibida por {chat_id}: {texto}")
    
    try:
        es_admin = sesiones_activas.get(chat_id, {}).get("rol") == "admin"
        logger.debug(f"Datos en context.user_data: {context.user_data}")
        
        if texto == "❌ Cancelar":
            logger.info(f"Eliminación de host cancelada por {chat_id}")
            await update.message.reply_text(
                "❌ *Operación cancelada*",
                reply_markup=teclado_principal(es_admin),
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        
        if texto == "✅ Confirmar":
            grupo = context.user_data.get("grupo")
            ip = context.user_data.get("ip")
            nombre = context.user_data.get("nombre")
            
            if not all([grupo, ip, nombre]):
                logger.error(f"Datos incompletos en confirmar_eliminar por {chat_id}: grupo={grupo}, ip={ip}, nombre={nombre}")
                await update.message.reply_text(
                    "❌ *Error: Datos incompletos*\nPor favor, inicia el proceso de nuevo.",
                    reply_markup=teclado_principal(es_admin),
                    parse_mode="Markdown"
                )
                return ConversationHandler.END
            
            logger.info(f"Intentando eliminar host: {nombre} ({ip}) de {grupo} por {chat_id}")
            with data_lock:
                if ip in hosts[grupo]:
                    del hosts[grupo][ip]
                    del estados[grupo]["estado_hosts"][ip]
                else:
                    logger.warning(f"IP {ip} no encontrada en {grupo} al intentar eliminar por {chat_id}")
                    await update.message.reply_text(
                        f"❌ *Error: IP `{ip}` no encontrada en {grupo.upper()}*",
                        reply_markup=teclado_principal(es_admin),
                        parse_mode="Markdown"
                    )
                    return ConversationHandler.END
            logger.debug(f"Antes de guardar_hosts para eliminar {ip} en {grupo}")
            await asyncio.to_thread(guardar_hosts)  # Ejecutar en hilo separado
            logger.debug(f"Después de guardar_hosts para eliminar {ip} en {grupo}")
            
            await update.message.reply_text(
                f"✅ *Host `{nombre}` ({ip}) eliminado de {grupo.upper()}*",
                reply_markup=teclado_principal(es_admin),
                parse_mode="Markdown"
            )
            logger.info(f"Host {nombre} ({ip}) eliminado de {grupo} por {chat_id}")
            return ConversationHandler.END
        
        logger.warning(f"Opción inválida en confirmar_eliminar por {chat_id}: {texto}")
        await update.message.reply_text(
            "❌ *Opción inválida*\nSelecciona *✅ Confirmar* o *❌ Cancelar*:",
            reply_markup=teclado_confirmar(),
            parse_mode="Markdown"
        )
        return CONFIRMAR_ELIMINAR
    
    except Exception as e:
        logger.error(f"Error en confirmar_eliminar por {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ *Error interno*\nPor favor, intenta de nuevo o contacta al administrador.",
            reply_markup=teclado_principal(es_admin),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def cancelar(update: Update, context):
    """Cancela una operación de conversación."""
    chat_id = update.message.chat_id
    es_admin = sesiones_activas.get(chat_id, {}).get("rol") == "admin"
    await update.message.reply_text(
        "❌ *Operación cancelada*",
        reply_markup=teclado_principal(es_admin),
        parse_mode="Markdown"
    )
    logger.info(f"Operación cancelada por {chat_id}")
    return ConversationHandler.END

# ================= INICIALIZACIÓN =================
async def main():
    """Función principal para iniciar el bot."""
    logger.info("Iniciando bot...")
    cargar_hosts()
    
    app = Application.builder().token(TOKEN).build()
    
    # Handler para login
    login_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, verificar_identificador)],
        },
        fallbacks=[CommandHandler("cancel", cancelar_login)],
    )
    
    # Handlers para agregar host
    conv_handler_agregar = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"➕ Agregar Host"), agregar_host)],
        states={
            GRUPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_grupo)],
            IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_ip)],
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)],
            CONFIRMAR_AGREGAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_agregar)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )
    
    # Handlers para eliminar host
    conv_handler_eliminar = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"🗑 Eliminar Host"), eliminar_host)],
        states={
            ELIMINAR_GRUPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_grupo_eliminar)],
            ELIMINAR_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_ip_eliminar)],
            CONFIRMAR_ELIMINAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_eliminar)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )
    
    # Registrar handlers
    app.add_handler(login_handler)
    app.add_handler(conv_handler_agregar)
    app.add_handler(conv_handler_eliminar)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    
    logger.info("Bot iniciado correctamente, comenzando polling...")
    
    # Iniciar tarea para procesar alertas
    asyncio.create_task(procesar_alertas(app))
    
    # Iniciar el bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    try:
        while True:
            await asyncio.sleep(30)
    except KeyboardInterrupt:
        logger.info("Deteniendo bot...")
        with data_lock:
            monitoreo_global = False
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("Bot detenido correctamente")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    #AGREGAR TOKEN AL ENTORNO VIRTUAL
    #$env:BOT_TOKEN="8004671936:AAGtMa8_oThlpXKeXXKHKx8snuyTDEt1MDE"