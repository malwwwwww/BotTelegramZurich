import subprocess
import time
import os
import asyncio
import json
import ipaddress
import logging
from datetime import datetime
from telegram import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, Update, Bot
from telegram.ext import CommandHandler, MessageHandler, filters, ConversationHandler
from collections import defaultdict
from shutil import copyfile

# Importa tus hosts personalizados
from hots import hosts_cctv, hosts_servers, hosts_switches, hosts_corporativo

# ================= CONFIGURACIÓN =================
# Configuración de logging
logging.basicConfig(
    filename='bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
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
    "switches": 10,
    "corporativo": 5
}

# Estados para ConversationHandler
LOGIN = 0
GRUPO, IP, NOMBRE = range(3)
ELIMINAR_GRUPO, ELIMINAR_IP = range(2)

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
        "estado_hosts": {ip: True for ip in datos_hosts}
    } for grupo, datos_hosts in hosts.items()
}

sesiones_activas = {}
monitoreo_global = False
usuarios_agregando_host = defaultdict(dict)

# ================= FUNCIONES BÁSICAS =================
def ping(ip):
    """Ejecuta un ping a una IP, compatible con IPv4 e IPv6."""
    try:
        ip_addr = ipaddress.ip_address(ip)
        param = "-n" if os.name == "nt" else "-c"
        command = ["ping6" if ip_addr.version == 6 else "ping", param, "1", "-w", "300", ip]
        result = subprocess.call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.debug(f"Ping a {ip}: {'Éxito' if result == 0 else 'Fallo'}")
        return result == 0
    except (subprocess.SubprocessError, ValueError) as e:
        logger.error(f"Error en ping a {ip}: {e}")
        return False

async def enviar_mensaje(bot, chat_id, texto):
    """Envía un mensaje a Telegram, manejando límites de longitud."""
    try:
        max_length = 4096
        if len(texto) > max_length:
            for i in range(0, len(texto), max_length):
                await bot.send_message(chat_id=chat_id, text=texto[i:i+max_length])
        else:
            await bot.send_message(chat_id=chat_id, text=texto)
    except Exception as e:
        logger.error(f"Error enviando mensaje a {chat_id}: {e}")

# ================= PERSISTENCIA =================
def cargar_credenciales():
    """Carga credenciales desde el archivo JSON."""
    try:
        with open(CREDENCIALES_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.error("Archivo de credenciales no encontrado o corrupto")
        raise FileNotFoundError("Crea un archivo credenciales.json con identificadores válidos")

def guardar_hosts():
    """Guarda hosts en el archivo JSON con respaldo."""
    try:
        if os.path.exists(ARCHIVO_PERSISTENCIA):
            copyfile(ARCHIVO_PERSISTENCIA, ARCHIVO_RESPALDO)
        with open(ARCHIVO_PERSISTENCIA, "w") as f:
            json.dump(hosts, f, indent=2)
        logger.info("Hosts guardados correctamente")
    except Exception as e:
        logger.error(f"Error guardando hosts: {e}")

def cargar_hosts():
    """Carga hosts desde el archivo JSON."""
    try:
        with open(ARCHIVO_PERSISTENCIA, "r") as f:
            datos = json.load(f)
            for grupo in hosts:
                if grupo in datos:
                    hosts[grupo].update(datos[grupo])
                    estados[grupo]["estado_hosts"].update({ip: True for ip in datos[grupo]})
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Usando hosts iniciales de hosts.py")

# ================= MONITOREO =================
async def monitoreo_grupo(bot, grupo, chat_id):
    """Monitorea un grupo de hosts y notifica cambios de estado."""
    while estados[grupo]["activo"] and monitoreo_global:
        for ip, nombre in hosts[grupo].items():
            if not estados[grupo]["activo"] or not monitoreo_global:
                break
            estado_actual = ping(ip)
            estado_anterior = estados[grupo]["estado_hosts"][ip]
            if not estado_actual and estado_anterior:
                estados[grupo]["estado_hosts"][ip] = False
                await enviar_mensaje(bot, chat_id, f"⚠ [{grupo.upper()}] ¡ALERTA! '{nombre}' ({ip}) NO responde.")
                logger.warning(f"Host {nombre} ({ip}) en {grupo} no responde")
            elif estado_actual and not estado_anterior:
                estados[grupo]["estado_hosts"][ip] = True
                await enviar_mensaje(bot, chat_id, f"✅ [{grupo.upper()}] '{nombre}' ({ip}) ha vuelto a responder.")
                logger.info(f"Host {nombre} ({ip}) en {grupo} volvió a responder")
        await asyncio.sleep(INTERVALOS_PING[grupo])

# ================= TECLADOS =================
def teclado_principal():
    """Teclado principal del bot."""
    return ReplyKeyboardMarkup([
        ["🟢 Iniciar todo", "🔴 Detener todo"],
        ["📊 Estado general", "📋 Listar sesiones"],
        ["🟢 Hosts activos", "🔴 Hosts inactivos"],
        ["➕ Agregar host", "🗑 Eliminar host"],
        ["⚙ Control por grupo", "🚪 Cerrar sesión"]
    ], resize_keyboard=True)

def teclado_grupos():
    """Teclado para seleccionar grupos."""
    return ReplyKeyboardMarkup([
        ["📷 CCTV", "💻 Servidores"],
        ["🔌 Switches", "🏢 Corporativo"],
        ["🔙 Menú principal"]
    ], resize_keyboard=True)

# ================= HANDLERS DE LOGIN =================
async def start(update: Update, context):
    """Inicia el flujo de login solicitando el identificador."""
    chat_id = update.message.chat_id
    if chat_id in sesiones_activas:
        await update.message.reply_text("✅ Ya estás autenticado", reply_markup=teclado_principal())
        return ConversationHandler.END
    await update.message.reply_text("🔐 Ingresa tu identificador:", reply_markup=ReplyKeyboardRemove())
    logger.info(f"Inicio de login para chat_id {chat_id}")
    return LOGIN

async def verificar_identificador(update: Update, context):
    """Verifica el identificador ingresado."""
    chat_id = update.message.chat_id
    identificador = update.message.text
    credenciales = cargar_credenciales()
    
    for cred in credenciales:
        if identificador == cred["identificador"]:
            sesiones_activas[chat_id] = {
                "identificador": identificador,
                "timestamp": time.time()
            }
            await update.message.reply_text("✅ Autenticación exitosa", reply_markup=teclado_principal())
            logger.info(f"Login exitoso para chat_id {chat_id} con identificador {identificador}")
            return ConversationHandler.END
    
    await update.message.reply_text("❌ Identificador incorrecto. Intenta de nuevo:")
    logger.warning(f"Intento de login fallido para chat_id {chat_id}")
    return LOGIN

async def cancelar_login(update: Update, context):
    """Cancela el flujo de login."""
    await update.message.reply_text("❌ Login cancelado")
    logger.info(f"Login cancelado para chat_id {update.message.chat_id}")
    return ConversationHandler.END

# ================= HANDLERS DE MENSAJES =================
async def manejar_mensaje(update: Update, context):
    """Maneja los mensajes de texto recibidos."""
    texto = update.message.text
    chat_id = update.message.chat_id
    bot = context.bot
    
    if chat_id not in sesiones_activas:
        await update.message.reply_text("❌ Usa /start para autenticarte")
        return

    if texto == "🟢 Iniciar todo":
        monitoreo_global = True
        for grupo in estados:
            if estados[grupo]["activo"]:
                asyncio.create_task(monitoreo_grupo(bot, grupo, chat_id))
        await update.message.reply_text("✅ Monitoreo global INICIADO")
        logger.info(f"Monitoreo global iniciado por {chat_id}")

    elif texto == "🔴 Detener todo":
        monitoreo_global = False
        await update.message.reply_text("🛑 Monitoreo global DETENIDO")
        logger.info(f"Monitoreo global detenido por {chat_id}")

    elif texto == "📊 Estado general":
        mensaje = "📊 ESTADO GENERAL:\n\n"
        for grupo in estados:
            activos = sum(estados[grupo]["estado_hosts"].values())
            total = len(estados[grupo]["estado_hosts"])
            mensaje += f"{grupo.upper()}: {activos}/{total} activos\n"
        await update.message.reply_text(mensaje)
        logger.info(f"Estado general solicitado por {chat_id}")

    elif texto == "🟢 Hosts activos":
        mensaje = "🟢 HOSTS ACTIVOS:\n\n"
        for grupo in estados:
            mensaje += f"=== {grupo.upper()} ===\n"
            for ip, nombre in hosts[grupo].items():
                if estados[grupo]["estado_hosts"][ip]:
                    mensaje += f"• {nombre} ({ip})\n"
            mensaje += "\n"
        await update.message.reply_text(mensaje)
        logger.info(f"Hosts activos solicitados por {chat_id}")

    elif texto == "🔴 Hosts inactivos":
        mensaje = "🔴 HOSTS INACTIVOS:\n\n"
        for grupo in estados:
            mensaje += f"=== {grupo.upper()} ===\n"
            for ip, nombre in hosts[grupo].items():
                if not estados[grupo]["estado_hosts"][ip]:
                    mensaje += f"• {nombre} ({ip})\n"
            mensaje += "\n"
        await update.message.reply_text(mensaje)
        logger.info(f"Hosts inactivos solicitados por {chat_id}")

    elif texto == "📋 Listar sesiones":
        if not sesiones_activas:
            await update.message.reply_text("No hay sesiones activas")
            return
        mensaje = "🔍 SESIONES ACTIVAS:\n\n"
        for sid, sesion in sesiones_activas.items():
            tiempo = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sesion["timestamp"]))
            mensaje += f"ID: {sid}\nIdentificador: {sesion['identificador']}\nConectado: {tiempo}\n\n"
        await update.message.reply_text(mensaje)
        logger.info(f"Sesiones activas solicitadas por {chat_id}")

    elif texto == "⚙ Control por grupo":
        await update.message.reply_text("Selecciona grupo:", reply_markup=teclado_grupos())
        logger.info(f"Control por grupo solicitado por {chat_id}")

    elif texto in ["📷 CCTV", "💻 Servidores", "🔌 Switches", "🏢 Corporativo"]:
        grupo = texto.lower().replace("📷 ", "").replace("💻 ", "").replace("🔌 ", "").replace("🏢 ", "")
        estados[grupo]["activo"] = not estados[grupo]["activo"]
        estado = "ACTIVADO" if estados[grupo]["activo"] else "DESACTIVADO"
        await update.message.reply_text(f"✅ Monitoreo de {grupo.upper()} {estado}")
        logger.info(f"Monitoreo de {grupo} {estado.lower()} por {chat_id}")

    elif texto == "🚪 Cerrar sesión":
        if chat_id in sesiones_activas:
            del sesiones_activas[chat_id]
        await update.message.reply_text("✅ Sesión cerrada", reply_markup=ReplyKeyboardRemove())
        logger.info(f"Sesión cerrada por {chat_id}")

    elif texto == "🔙 Menú principal":
        await update.message.reply_text("Menú principal:", reply_markup=teclado_principal())
        logger.info(f"Menú principal solicitado por {chat_id}")

# ================= CONVERSATION HANDLERS =================
async def agregar_host(update: Update, context):
    """Inicia el flujo para agregar un host."""
    if update.message.chat_id not in sesiones_activas:
        await update.message.reply_text("❌ Usa /start para autenticarte")
        return ConversationHandler.END
    await update.message.reply_text("Selecciona el grupo:", reply_markup=teclado_grupos())
    logger.info(f"Inicio de agregar host por {update.message.chat_id}")
    return GRUPO

async def recibir_grupo(update: Update, context):
    """Recibe el grupo seleccionado para agregar un host."""
    texto = update.message.text.lower().replace("📷 ", "").replace("💻 ", "").replace("🔌 ", "").replace("🏢 ", "")
    if texto not in hosts:
        await update.message.reply_text("Grupo inválido. Selecciona uno válido:", reply_markup=teclado_grupos())
        return GRUPO
    context.user_data["grupo"] = texto
    await update.message.reply_text("Ingresa la IP del host:", reply_markup=ReplyKeyboardRemove())
    return IP

async def recibir_ip(update: Update, context):
    """Recibe y valida la IP del host."""
    ip = update.message.text
    try:
        ipaddress.ip_address(ip)
        if ip in hosts[context.user_data["grupo"]]:
            await update.message.reply_text("Esta IP ya existe en el grupo. Ingresa otra IP:")
            return IP
        context.user_data["ip"] = ip
        await update.message.reply_text("Ingresa el nombre del host:")
        return NOMBRE
    except ValueError:
        await update.message.reply_text("IP inválida. Ingresa una IP correcta:")
        return IP

async def recibir_nombre(update: Update, context):
    """Recibe el nombre del host y lo guarda."""
    nombre = update.message.text
    grupo = context.user_data["grupo"]
    ip = context.user_data["ip"]
    
    hosts[grupo][ip] = nombre
    estados[grupo]["estado_hosts"][ip] = True
    guardar_hosts()
    
    await update.message.reply_text(f"✅ Host '{nombre}' ({ip}) agregado a {grupo.upper()}", reply_markup=teclado_principal())
    logger.info(f"Host {nombre} ({ip}) agregado a {grupo} por {update.message.chat_id}")
    return ConversationHandler.END

async def eliminar_host(update: Update, context):
    """Inicia el flujo para eliminar un host."""
    if update.message.chat_id not in sesiones_activas:
        await update.message.reply_text("❌ Usa /start para autenticarte")
        return ConversationHandler.END
    await update.message.reply_text("Selecciona el grupo:", reply_markup=teclado_grupos())
    logger.info(f"Inicio de eliminar host por {update.message.chat_id}")
    return ELIMINAR_GRUPO

async def recibir_grupo_eliminar(update: Update, context):
    """Recibe el grupo para eliminar un host."""
    texto = update.message.text.lower().replace("📷 ", "").replace("💻 ", "").replace("🔌 ", "").replace("🏢 ", "")
    if texto not in hosts:
        await update.message.reply_text("Grupo inválido. Selecciona uno válido:", reply_markup=teclado_grupos())
        return ELIMINAR_GRUPO
    context.user_data["grupo"] = texto
    mensaje = f"Hosts en {texto.upper()}:\n"
    for ip, nombre in hosts[texto].items():
        mensaje += f"• {nombre} ({ip})\n"
    mensaje += "\nIngresa la IP del host a eliminar:"
    await update.message.reply_text(mensaje, reply_markup=ReplyKeyboardRemove())
    return ELIMINAR_IP

async def recibir_ip_eliminar(update: Update, context):
    """Recibe y elimina la IP del host."""
    ip = update.message.text
    grupo = context.user_data["grupo"]
    if ip not in hosts[grupo]:
        await update.message.reply_text("IP no encontrada en el grupo. Ingresa una IP válida:")
        return ELIMINAR_IP
    nombre = hosts[grupo][ip]
    del hosts[grupo][ip]
    del estados[grupo]["estado_hosts"][ip]
    guardar_hosts()
    await update.message.reply_text(f"✅ Host '{nombre}' ({ip}) eliminado de {grupo.upper()}", reply_markup=teclado_principal())
    logger.info(f"Host {nombre} ({ip}) eliminado de {grupo} por {update.message.chat_id}")
    return ConversationHandler.END

async def cancelar(update: Update, context):
    """Cancela una operación de conversación."""
    await update.message.reply_text("❌ Operación cancelada", reply_markup=teclado_principal())
    logger.info(f"Operación cancelada por {update.message.chat_id}")
    return ConversationHandler.END

# ================= INICIALIZACIÓN =================
async def main():
    """Función principal para iniciar el bot."""
    cargar_hosts()
    
    bot = Bot(token=TOKEN)
    offset = 0

    # Configurar handlers
    login_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, verificar_identificador)],
        },
        fallbacks=[CommandHandler("cancel", cancelar_login)],
    )
    
    conv_handler_agregar = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Agregar host$"), agregar_host)],
        states={
            GRUPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_grupo)],
            IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_ip)],
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )
    
    conv_handler_eliminar = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑 Eliminar host$"), eliminar_host)],
        states={
            ELIMINAR_GRUPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_grupo_eliminar)],
            ELIMINAR_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_ip_eliminar)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )

    handlers = [
        login_handler,
        conv_handler_agregar,
        conv_handler_eliminar,
        MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje),
    ]

    logger.info("Bot iniciado correctamente")

    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30)
            for update in updates:
                offset = max(offset, update.update_id + 1)
                for handler in handlers:
                    if await handler.check_update(update):
                        await handler.handle_update(update, bot, context={})
                        break
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error en el bucle de polling: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())