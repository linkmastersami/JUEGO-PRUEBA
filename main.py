"""
Estratega de Códigos - Prototipo web fiel al reglamento oficial, para 2-4 jugadores en línea.

Cómo correrlo:
    pip install -r requirements.txt
    uvicorn main:app --reload
Luego abre http://localhost:8000 en varias pestañas/dispositivos,
usa el mismo código de sala y nombres diferentes.
"""

import json
import os
import random
import asyncio
import time
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope
from supabase import create_client, Client


class UTF8StaticFiles(StaticFiles):
    """StaticFiles que fuerza charset=utf-8 en las respuestas de texto.

    Sin esto, algunos navegadores/dispositivos (TVs, smart TVs, etc.) no
    detectan automáticamente que el HTML/JS es UTF-8 y lo interpretan con
    otra codificación, mostrando "" en vez de tildes, "ñ" y emojis — y
    a veces incluso rompiendo el parseo del <script>, lo que deja botones
    como "Entrar" o "Registrarse" sin funcionar.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith(("text/", "application/javascript")) and "charset" not in content_type:
            response.headers["content-type"] = f"{content_type}; charset=utf-8"
        return response

app = FastAPI()

# ---------------------------------------------------------------------------
# Base de datos y Autenticación (Supabase)
# ---------------------------------------------------------------------------
# El login/registro (usuario+contraseña) lo maneja Supabase Auth desde el
# frontend. El backend solo necesita la SERVICE ROLE KEY para leer/escribir
# puntos y victorias en la tabla "profiles" sin quedar sujeto a las políticas
# de RLS (esta llave es secreta: nunca debe usarse en el frontend).
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("⚠️  SUPABASE_URL / SUPABASE_SERVICE_KEY no configuradas: los puntos no se guardarán.")

RANGOS = [
    "Adivino de Feria", "Curioso Empedernido", "Observador Casual", 
    "Estudiante de Probabilidades", "Detective Aficionado", "Analista de Patrones", 
    "Perfilador de Códigos", "Calculador Frío", "Estratega Silencioso", 
    "Zorro Ártico", "Mente de Neón", "Maestro de la Navaja", 
    "Cerebro de Cristal", "Oráculo de Bolsillo", "El Predicador"
]

# ---------------------------------------------------------------------------
# Catálogo de avatares (carpeta frontend/gif/)
# ---------------------------------------------------------------------------
# Convención de nombres: los primeros 3 dígitos del archivo son el precio en
# monedas ("15001.gif" -> 150, "35001.gif" -> 350). El resto del nombre solo
# distingue variantes del mismo precio. Como la carpeta vive dentro de
# "frontend/", StaticFiles ya la sirve tal cual en /gif/<archivo>: no hace
# falta un endpoint aparte para las imágenes.
AVATAR_DIR = os.path.join("frontend", "gif")
AVATAR_EXTENSIONS = (".gif", ".webp", ".png")


def cargar_catalogo_avatares() -> List[dict]:
    """Lee frontend/gif/ y arma el catálogo a partir del nombre de archivo.
    Subir un archivo nuevo a esa carpeta lo agrega solo a la tienda, sin
    tocar Supabase ni reiniciar nada más que el proceso."""
    catalogo = []
    if not os.path.isdir(AVATAR_DIR):
        print(f"⚠️  Carpeta de avatares no encontrada: {AVATAR_DIR}")
        return catalogo

    for archivo in sorted(os.listdir(AVATAR_DIR)):
        nombre, ext = os.path.splitext(archivo)
        if ext.lower() not in AVATAR_EXTENSIONS:
            continue
        if len(nombre) < 3 or not nombre[:3].isdigit():
            print(f"⚠️  Ignorado (no respeta la convención de precio): {archivo}")
            continue
        precio = int(nombre[:3])
        catalogo.append({"archivo": archivo, "precio": precio})

    catalogo.sort(key=lambda a: (a["precio"], a["archivo"]))
    return catalogo


CATALOGO_AVATARES: List[dict] = cargar_catalogo_avatares()
PRECIO_AVATAR_GRATIS = 150  # el avatar gratis de bienvenida sale de este precio

def obtener_rango(puntos: int, victorias: int):
    if victorias == 0:
        return "Novato en Desactivación"
    
    indice = (puntos - 1) // 60000 if puntos > 0 else 0
    indice = max(0, min(14, indice))
    return RANGOS[indice]

def update_player_score(username: str, points_change: int) -> int:
    """Suma/resta puntos al perfil del usuario autenticado en Supabase.

    El nombre con el que se juega es siempre el "username" con el que la
    persona se registró/inició sesión (ver frontend), así que esta fila ya
    existe desde el registro (la crea un trigger en Supabase). Si por algún
    motivo no existiera, se crea aquí como respaldo.
    """
    if supabase is None:
        print(f"⚠️  No se guardaron los puntos de {username}: Supabase no configurado.")
        return 0

    try:
        resp = supabase.table("profiles").select("puntos, victorias").eq("username", username).execute()

        if resp.data:
            current_puntos = resp.data[0]["puntos"] or 0
            current_victorias = resp.data[0]["victorias"] or 0
        else:
            current_puntos, current_victorias = 0, 0

        new_puntos = max(0, current_puntos + points_change)
        new_victorias = current_victorias + (1 if points_change > 0 else 0)

        if resp.data:
            update_resp = supabase.table("profiles").update(
                {"puntos": new_puntos, "victorias": new_victorias}
            ).eq("username", username).execute()
            if not update_resp.data:
                print(f"⚠️  UPDATE de puntos para {username} no afectó ninguna fila (revisa RLS/policies).")
        else:
            supabase.table("profiles").insert(
                {"username": username, "puntos": new_puntos, "victorias": new_victorias}
            ).execute()

        print(f"✅ Puntos de {username} actualizados: {current_puntos} → {new_puntos}")
        return new_puntos
    except Exception as e:
        print(f"❌ ERROR guardando puntos de {username} en Supabase: {e}")
        return 0


# Montos posibles de la caja fuerte (ver Room.grant_coin_reward). Nunca se
# multiplican por el x1/x2/x3 de puntos: son un premio aparte y fijo, y no
# hay tope diario — mientras el jugador siga acertando, sigue ganando.
COIN_REWARD_OPTIONS = [100, 75, 50]


def add_coins(username: str, amount: int) -> int:
    """Suma monedas al perfil del usuario en Supabase. Independiente del
    multiplicador de puntos: siempre se suma el monto tal cual, sin tope
    diario."""
    if supabase is None:
        print(f"⚠️  No se sumaron monedas a {username}: Supabase no configurado.")
        return 0
    if amount <= 0:
        return 0

    try:
        resp = supabase.table("profiles").select("monedas").eq("username", username).execute()
        current_monedas = (resp.data[0].get("monedas") or 0) if resp.data else 0
        new_monedas = current_monedas + amount

        if resp.data:
            supabase.table("profiles").update({"monedas": new_monedas}).eq("username", username).execute()
        else:
            supabase.table("profiles").insert({"username": username, "monedas": new_monedas}).execute()

        print(f"🪙 Monedas de {username}: {current_monedas} → {new_monedas} (+{amount})")
        return new_monedas
    except Exception as e:
        print(f"❌ ERROR sumando monedas de {username} en Supabase: {e}")
        return 0


@app.get("/perfil/{username}")
async def obtener_perfil(username: str):
    """Devuelve puntos/victorias/rango/monedas/avatar acumulados para un
    usuario registrado en Supabase."""
    if supabase is None:
        return {"username": username, "puntos": 0, "victorias": 0, "monedas": 0, "avatar_actual": None, "rango": obtener_rango(0, 0)}

    resp = supabase.table("profiles").select("puntos, victorias, monedas, avatar_actual").eq("username", username).execute()
    if resp.data:
        puntos = resp.data[0]["puntos"] or 0
        victorias = resp.data[0]["victorias"] or 0
        monedas = resp.data[0].get("monedas") or 0
        avatar_actual = resp.data[0].get("avatar_actual")
    else:
        puntos, victorias, monedas, avatar_actual = 0, 0, 0, None

    rango = obtener_rango(puntos, victorias)
    return {"username": username, "puntos": puntos, "victorias": victorias, "monedas": monedas,
            "avatar_actual": avatar_actual, "rango": rango}


def obtener_avatar_y_rango(username: str) -> tuple:
    """Consulta rápida al unirse a una sala: trae el avatar_actual y el
    rango del jugador para mandarlos junto con su ficha a los demás."""
    if supabase is None:
        return None, obtener_rango(0, 0)
    try:
        resp = supabase.table("profiles").select("puntos, victorias, avatar_actual").eq("username", username).execute()
    except Exception as e:
        print(f"❌ ERROR obteniendo avatar/rango de {username}: {e}")
        return None, obtener_rango(0, 0)
    if not resp.data:
        return None, obtener_rango(0, 0)
    fila = resp.data[0]
    puntos = fila.get("puntos") or 0
    victorias = fila.get("victorias") or 0
    return fila.get("avatar_actual"), obtener_rango(puntos, victorias)


@app.get("/ranking")
async def obtener_ranking(limite: int = 50):
    """Tabla de posiciones: nombre, rango, puntos y avatar actual de cada
    jugador registrado, ordenados de mayor a menor puntaje."""
    if supabase is None:
        return {"ranking": []}

    try:
        resp = (
            supabase.table("profiles")
            .select("username, puntos, victorias, avatar_actual")
            .order("puntos", desc=True)
            .limit(max(1, min(limite, 200)))
            .execute()
        )
    except Exception as e:
        print(f"❌ ERROR obteniendo ranking de Supabase: {e}")
        return {"ranking": []}

    ranking = []
    for i, fila in enumerate(resp.data or []):
        puntos = fila.get("puntos") or 0
        victorias = fila.get("victorias") or 0
        ranking.append({
            "posicion": i + 1,
            "username": fila.get("username"),
            "puntos": puntos,
            "rango": obtener_rango(puntos, victorias),
            "avatar": fila.get("avatar_actual"),
        })
    return {"ranking": ranking}


# ---------------------------------------------------------------------------
# Tienda de avatares + selector + solicitudes (con cooldown de 15 días)
# ---------------------------------------------------------------------------
SOLICITUD_COOLDOWN_DIAS = 15


def _obtener_perfil_avatares(username: str) -> dict:
    """Trae monedas/avatar_actual/avatares_comprados de una fila. Si no
    existe todavía, devuelve los valores por defecto (cuenta nueva)."""
    resp = supabase.table("profiles").select(
        "monedas, avatar_actual, avatares_comprados"
    ).eq("username", username).execute()
    if resp.data:
        fila = resp.data[0]
        return {
            "monedas": fila.get("monedas") or 0,
            "avatar_actual": fila.get("avatar_actual"),
            "avatares_comprados": fila.get("avatares_comprados") or [],
        }
    return {"monedas": 0, "avatar_actual": None, "avatares_comprados": []}


@app.get("/tienda/{username}")
async def tienda(username: str):
    """Catálogo completo con "comprado: true/false" según los avatares que
    ya son de este jugador. Esto es la tienda; el selector (solo lo ya
    comprado) es /avatares/{username}."""
    if supabase is None:
        return {"catalogo": [{**a, "comprado": False} for a in CATALOGO_AVATARES], "monedas": 0, "es_cuenta_nueva": True}

    perfil = _obtener_perfil_avatares(username)
    comprados = set(perfil["avatares_comprados"])
    catalogo = [{**a, "comprado": a["archivo"] in comprados} for a in CATALOGO_AVATARES]
    return {
        "catalogo": catalogo,
        "monedas": perfil["monedas"],
        "es_cuenta_nueva": len(comprados) == 0,
        "precio_avatar_gratis": PRECIO_AVATAR_GRATIS,
    }


@app.get("/avatares/{username}")
async def avatares_del_jugador(username: str):
    """Selector: solo los avatares que este jugador ya compró/reclamó."""
    if supabase is None:
        return {"avatares": [], "avatar_actual": None}
    perfil = _obtener_perfil_avatares(username)
    return {"avatares": perfil["avatares_comprados"], "avatar_actual": perfil["avatar_actual"]}


@app.post("/tienda/comprar")
async def comprar_avatar(payload: dict):
    """Compra (o reclama gratis la primera vez) un avatar del catálogo.

    Regla de "primer avatar gratis": si el jugador todavía no tiene ningún
    avatar comprado, puede elegir cualquiera de precio 150 sin que se le
    cobren monedas. De ahí en adelante todo se cobra normal — esto se
    detecta solo con "¿ya tiene algo comprado?", sin bandera aparte.
    """
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    username = str(payload.get("username", "")).strip()
    archivo = str(payload.get("archivo", "")).strip()
    if not username or not archivo:
        return {"ok": False, "error": "Falta username o archivo."}

    item = next((a for a in CATALOGO_AVATARES if a["archivo"] == archivo), None)
    if item is None:
        return {"ok": False, "error": "Ese avatar no existe en el catálogo."}

    perfil = _obtener_perfil_avatares(username)
    comprados = list(perfil["avatares_comprados"])
    if archivo in comprados:
        return {"ok": False, "error": "Ya tienes ese avatar."}

    es_primer_avatar = len(comprados) == 0
    gratis = es_primer_avatar and item["precio"] == PRECIO_AVATAR_GRATIS

    if not gratis:
        if perfil["monedas"] < item["precio"]:
            return {"ok": False, "error": "No te alcanzan las monedas."}

    nuevas_monedas = perfil["monedas"] if gratis else perfil["monedas"] - item["precio"]
    comprados.append(archivo)
    nuevo_actual = perfil["avatar_actual"] or archivo  # si no tenía ninguno puesto, este queda activo

    try:
        supabase.table("profiles").update({
            "monedas": nuevas_monedas,
            "avatares_comprados": comprados,
            "avatar_actual": nuevo_actual,
        }).eq("username", username).execute()
    except Exception as e:
        print(f"❌ ERROR comprando avatar de {username}: {e}")
        return {"ok": False, "error": "Error guardando la compra."}

    print(f"🛍️  {username} {'reclamó gratis' if gratis else 'compró'} {archivo}.")
    return {"ok": True, "gratis": gratis, "monedas": nuevas_monedas, "avatar_actual": nuevo_actual}


@app.post("/avatar/elegir")
async def elegir_avatar(payload: dict):
    """Cambia el avatar_actual del jugador a uno que ya tiene comprado."""
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    username = str(payload.get("username", "")).strip()
    archivo = str(payload.get("archivo", "")).strip()
    if not username or not archivo:
        return {"ok": False, "error": "Falta username o archivo."}

    perfil = _obtener_perfil_avatares(username)
    if archivo not in perfil["avatares_comprados"]:
        return {"ok": False, "error": "No has comprado ese avatar todavía."}

    try:
        supabase.table("profiles").update({"avatar_actual": archivo}).eq("username", username).execute()
    except Exception as e:
        print(f"❌ ERROR cambiando avatar de {username}: {e}")
        return {"ok": False, "error": "Error guardando el cambio."}

    return {"ok": True, "avatar_actual": archivo}


@app.get("/avatar/solicitar/estado/{username}")
async def estado_solicitud_avatar(username: str):
    """Dice si el jugador puede pedir un avatar nuevo o si está en cooldown
    (15 días desde su última solicitud)."""
    if supabase is None:
        return {"puede_solicitar": True, "dias_restantes": 0}

    try:
        resp = (
            supabase.table("solicitudes_avatar")
            .select("fecha")
            .eq("username", username)
            .order("fecha", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"❌ ERROR consultando solicitudes de {username}: {e}")
        return {"puede_solicitar": True, "dias_restantes": 0}

    if not resp.data:
        return {"puede_solicitar": True, "dias_restantes": 0}

    from datetime import datetime, timezone
    ultima = datetime.fromisoformat(resp.data[0]["fecha"].replace("Z", "+00:00"))
    dias_pasados = (datetime.now(timezone.utc) - ultima).days
    if dias_pasados >= SOLICITUD_COOLDOWN_DIAS:
        return {"puede_solicitar": True, "dias_restantes": 0}
    return {"puede_solicitar": False, "dias_restantes": SOLICITUD_COOLDOWN_DIAS - dias_pasados}


@app.post("/avatar/solicitar")
async def solicitar_avatar(payload: dict):
    """Registra el pedido de un avatar nuevo. No se borran filas viejas: al
    chequear el cooldown simplemente se ignoran las de más de 15 días, así
    se conserva el historial completo de pedidos."""
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    username = str(payload.get("username", "")).strip()
    texto = str(payload.get("texto", "")).strip()[:500]
    if not username or not texto:
        return {"ok": False, "error": "Falta username o el texto del pedido."}

    estado = await estado_solicitud_avatar(username)
    if not estado["puede_solicitar"]:
        return {"ok": False, "error": f"Ya pediste un avatar hace poco. Vuelve a intentar en {estado['dias_restantes']} día(s)."}

    try:
        supabase.table("solicitudes_avatar").insert({"username": username, "texto": texto}).execute()
    except Exception as e:
        print(f"❌ ERROR guardando solicitud de {username}: {e}")
        return {"ok": False, "error": "Error guardando el pedido."}

    return {"ok": True}


# ---------------------------------------------------------------------------
# Configuración del Juego y Salas WebSocket
# ---------------------------------------------------------------------------
rooms: Dict[str, "Room"] = {}
room_counter = 1  # Contador global para emparejamiento automático

MIN_PLAYERS = 2
MAX_PLAYERS = 4
DISCONNECT_TIMEOUT = 20  # segundos de gracia antes de que un jugador desconectado pierda automáticamente

# ---------------------------------------------------------------------------
# Chat grupal (lobby) y contador de jugadores en línea
# ---------------------------------------------------------------------------
# Es un chat global, independiente de las salas: nunca se limpia por sí solo,
# solo conserva los últimos 50 mensajes (se van borrando los más viejos).
# Solo es visible para quien ya puso su nombre. "En línea" cuenta a
# cualquier jugador conectado y con nombre puesto, esté en el lobby o ya
# dentro de una sala/partida.
LOBBY_CHAT_LIMIT = 50
lobby_sockets: Dict[str, WebSocket] = {}   # id de conexión -> websocket
lobby_chat_messages: List[dict] = []
lobby_chat_seq = 0
online_names: Dict[str, int] = {}          # nombre -> nº de conexiones activas


def _add_online(name: str) -> None:
    online_names[name] = online_names.get(name, 0) + 1


def _remove_online(name: str) -> None:
    if name not in online_names:
        return
    online_names[name] -= 1
    if online_names[name] <= 0:
        del online_names[name]


async def _broadcast_lobby() -> None:
    payload = json.dumps({
        "type": "lobby_state",
        "chat": lobby_chat_messages,
        "online_count": len(online_names),
    })
    caidos = []
    for sid, sock in lobby_sockets.items():
        try:
            await sock.send_text(payload)
        except Exception:
            caidos.append(sid)
    for sid in caidos:
        lobby_sockets.pop(sid, None)


COLORS = ["green", "pink", "blue", "red", "orange"]
COLOR_HEX = {
    "green": "#3f9142",
    "pink": "#d17fae",
    "blue": "#3f97a6",
    "red": "#d1453f",
    "orange": "#e8933f",
}

def tile_info(n: int) -> dict:
    color = COLORS[(n - 1) % 5]
    column = (n - 1) // 5 + 1
    dots = ((column - 1) % 3) + 1
    return {"number": n, "color": color, "dots": dots}


class ClueTile:
    def __init__(self, kind: str, tile: dict, notch: Optional[int] = None,
                 slot: Optional[int] = None, same: Optional[bool] = None):
        self.kind = kind  
        self.tile = tile
        self.notch = notch  
        self.slot = slot  
        self.same = same  

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "tile": self.tile}
        if self.kind == "categorize":
            d["notch"] = self.notch
        else:
            d["slot"] = self.slot
            d["same"] = self.same
        return d


class Player:
    def __init__(self, pid: str, name: str, ws: WebSocket, avatar: Optional[str] = None, rango: Optional[str] = None):
        self.id = pid
        self.name = name
        self.ws = ws
        self.secret: List[dict] = []  
        self.clues: List[ClueTile] = []
        self.eliminated = False  
        self.resolved = False  
        self.connected = True
        self.avatar = avatar  # nombre de archivo en frontend/gif/, o None
        self.rango = rango or obtener_rango(0, 0)

    def notch_for(self, number: int) -> int:
        return sum(1 for t in self.secret if t["number"] < number)

    def to_dict(self, viewer_id: str, for_spectator: bool = False) -> dict:
        is_owner = (not for_spectator) and viewer_id == self.id
        if is_owner:
            # Ni siquiera el dueño ve el número ni los puntitos de su
            # propio código: solo el color de cada ficha, en orden.
            secret_view = [{"hidden": True, "color": t["color"]} for t in self.secret]
        else:
            # Nadie más —ni otros jugadores ni espectadores— puede ver el
            # número real del código de otro jugador. Sí se ve el color y
            # la cantidad de puntitos de cada ficha.
            secret_view = [{"hidden": True, "color": t["color"], "dots": t["dots"]} for t in self.secret]
        return {
            "id": self.id,
            "name": self.name,
            "is_you": is_owner,
            "secret": secret_view,
            "clues": [c.to_dict() for c in self.clues],
            "eliminated": self.eliminated,
            "resolved": self.resolved,
            "connected": self.connected,
            "avatar": self.avatar,
            "rango": self.rango,
        }


class Room:
    def __init__(self, code: str, mode: str = "multi"):
        self.code = code
        self.mode = mode  # "multi" o "solo"
        self.players: List[Player] = []
        self.status = "waiting"  
        self.phase = "reveal"  
        self.color_piles: Dict[str, List[int]] = {c: [] for c in COLORS}
        self.faceup: List[dict] = []
        self.turn_index = 0
        self.winner: Optional[str] = None
        self.log: List[str] = []
        self.last_score_gained = 0
        self.event_seq = 0
        self.last_event: Optional[dict] = None
        self.events: List[dict] = []  
        self.pending_guess: Optional[dict] = None
        # --- Monedas (caja fuerte) ---
        # Recompensa fija (100/75/50) por acertar el propio código, separada
        # del multiplicador de puntos. Se guarda por jugador y por partida
        # (llave = id del jugador) para que refrescar la página no permita
        # reclamarla dos veces.
        self.coin_rewards: Dict[str, int] = {}
        # --- Manejo de desconexiones ---
        # Solo se activa una cuenta regresiva cuando le toca jugar a un
        # jugador desconectado (o se desconecta estando en su turno). Los
        # demás jugadores no se enteran de nada hasta ese momento.
        self.disconnect_player_id: Optional[str] = None
        self.disconnect_deadline: Optional[float] = None
        self.disconnect_token: int = 0
        # --- Chat de texto ---
        self.chat_messages: List[dict] = []
        self.chat_seq = 0
        # --- Espectadores: no ocupan lugar de jugador ni ven códigos ---
        self.spectators: Dict[str, WebSocket] = {}

    def add_chat_message(self, sender: "Player", text: str) -> None:
        text = text.strip()[:300]
        if not text:
            return
        self.chat_seq += 1
        self.chat_messages.append({
            "seq": self.chat_seq,
            "player_id": sender.id,
            "player_name": sender.name,
            "text": text,
            "is_spectator": False,
        })
        self.chat_messages = self.chat_messages[-100:]

    def add_spectator_chat_message(self, viewer_name: str, text: str) -> None:
        text = text.strip()[:300]
        if not text:
            return
        self.chat_seq += 1
        self.chat_messages.append({
            "seq": self.chat_seq,
            "player_id": None,
            "player_name": viewer_name,
            "text": text,
            "is_spectator": True,
        })
        self.chat_messages = self.chat_messages[-100:]

    def get_player(self, pid: str) -> Optional[Player]:
        return next((p for p in self.players if p.id == pid), None)

    def start(self):
        by_color: Dict[str, List[int]] = {c: [] for c in COLORS}
        for n in range(1, 61):
            by_color[tile_info(n)["color"]].append(n)
        for c in COLORS:
            random.shuffle(by_color[c])

        # Se queman 5 fichas en total (1 por cada color), tal como marca el
        # reglamento — antes se quemaban 5 por color (25 en total), lo cual
        # dejaba el contador del mazo mal calculado (p. ej. 25 en vez de 45
        # en una partida de 2 jugadores).
        for c in COLORS:
            if by_color[c]: by_color[c].pop()

        for p in self.players:
            secret = [tile_info(by_color[c].pop()) for c in COLORS]
            secret.sort(key=lambda t: t["number"])
            p.secret = secret
            p.clues = []
            p.eliminated = False
            p.resolved = False

        self.faceup = [] 
        self.color_piles = by_color
        self.status = "playing"
        self.phase = "reveal"
        self.turn_index = 0
        self.winner = None
        self.last_score_gained = 0
        self.event_seq = 0
        self.last_event = None
        self.events = []
        self.pending_guess = None
        self.coin_rewards = {}
        self._clear_disconnect_timer()
        self.log.append("La partida ha comenzado. ¡Suerte!")

    def current_player(self) -> Player:
        return self.players[self.turn_index]

    def colors_available(self) -> Dict[str, bool]:
        return {c: len(self.color_piles[c]) > 0 for c in COLORS}

    def reveal_tile(self, color: str) -> Optional[dict]:
        pile = self.color_piles.get(color, [])
        if not pile:
            return None
        n = pile.pop()
        tile = tile_info(n)
        tile["used"] = False
        self.faceup.append(tile)
        cur = self.current_player()
        self.log.append(f"{cur.name} reveló el {n} y lo puso junto a la reserva.")
        self.phase = "clue"
        return tile

    def apply_categorize(self, faceup_index: int) -> bool:
        if not (0 <= faceup_index < len(self.faceup)):
            return False
        tile = self.faceup[faceup_index]
        if tile.get("used"):
            return False
        cur = self.current_player()
        notch = cur.notch_for(tile["number"])
        cur.clues.append(ClueTile("categorize", dict(tile), notch=notch))
        tile["used"] = True
        self.log.append(f"{cur.name} pidió CATEGORIZAR el {tile['number']} en su código.")
        self._end_clue_step()
        return True

    def apply_compare(self, faceup_index: int, slot: int) -> bool:
        if not (0 <= faceup_index < len(self.faceup)):
            return False
        tile = self.faceup[faceup_index]
        if tile.get("used"):
            return False
        cur = self.current_player()
        if not (0 <= slot < len(cur.secret)):
            return False
        same = tile["dots"] == cur.secret[slot]["dots"]
        cur.clues.append(ClueTile("compare", dict(tile), slot=slot, same=same))
        tile["used"] = True
        ans = "sí coinciden" if same else "no coinciden"
        self.log.append(
            f"{cur.name} comparó el {tile['number']} con su ficha #{slot + 1}: los puntitos {ans}."
        )
        self._end_clue_step()
        return True

    def _end_clue_step(self):
        self.advance_turn()
        self.phase = "reveal"

    def advance_turn(self):
        n = len(self.players)
        for _ in range(n):
            self.turn_index = (self.turn_index + 1) % n
            p = self.players[self.turn_index]
            if not p.eliminated and not p.resolved:
                break
        self._sync_disconnect_timer()

    # --- Manejo de desconexiones ---
    def _clear_disconnect_timer(self):
        self.disconnect_player_id = None
        self.disconnect_deadline = None
        self.disconnect_token += 1

    def _start_disconnect_timer(self, player: "Player"):
        self.disconnect_player_id = player.id
        self.disconnect_deadline = time.time() + DISCONNECT_TIMEOUT
        self.disconnect_token += 1
        self.log.append(
            f"{player.name} está desconectado y le tocaba jugar: tiene {DISCONNECT_TIMEOUT}s para volver o pierde."
        )
        asyncio.create_task(_watch_disconnect(self.code, player.id, self.disconnect_token))

    def _sync_disconnect_timer(self):
        """Revisa si al jugador en turno le toca cuenta regresiva por estar
        desconectado. Solo aplica en partidas multijugador en curso."""
        if self.mode != "multi" or self.status != "playing" or not self.players:
            if self.disconnect_player_id is not None:
                self._clear_disconnect_timer()
            return
        cur = self.current_player()
        if cur.connected:
            if self.disconnect_player_id is not None:
                self._clear_disconnect_timer()
        elif self.disconnect_player_id != cur.id:
            self._start_disconnect_timer(cur)

    def eliminate_disconnected(self, player: "Player") -> None:
        """Un jugador desconectado no volvió a tiempo (20s): pierde
        automáticamente y la partida continúa sin él."""
        if player.eliminated or player.resolved:
            return
        was_current = self.current_player().id == player.id
        player.eliminated = True
        self.log.append(f"{player.name} perdió por no volver a tiempo tras desconectarse.")
        self._record_event("disconnected_out", player, 0)
        if self.disconnect_player_id == player.id:
            self._clear_disconnect_timer()
        finished = self._finish_if_one_active_remains("exploded")
        if not finished and was_current:
            self.advance_turn()
            self.phase = "reveal"

    def leave_game(self, player: "Player") -> bool:
        """Un jugador presiona 'Salir' durante una partida en curso: pierde
        automáticamente y la partida continúa sin él."""
        if self.status != "playing" or player.eliminated or player.resolved:
            return False
        was_current = self.current_player().id == player.id
        player.eliminated = True
        player.connected = False
        self.log.append(f"{player.name} salió de la partida y perdió automáticamente.")
        self._record_event("left", player, 0)
        if self.disconnect_player_id == player.id:
            self._clear_disconnect_timer()
        finished = self._finish_if_one_active_remains("exploded")
        if not finished and was_current:
            self.advance_turn()
            self.phase = "reveal"
        return True

    def active_players(self) -> List[Player]:
        return [p for p in self.players if not p.eliminated and not p.resolved]

    def deck_remaining(self) -> int:
        return sum(len(pile) for pile in self.color_piles.values())

    def _record_event(self, event_type: str, player: Player, points: int, multiplier: Optional[int] = None,
                       coins: int = 0):
        self.event_seq += 1
        self.last_event = {
            "seq": self.event_seq,
            "type": event_type, 
            "player_id": player.id,
            "player_name": player.name,
            "points": points,
            "multiplier": multiplier,
            "coins": coins,
            "deck_remaining": self.deck_remaining(),
        }
        self.events.append(self.last_event)
        self.events = self.events[-10:]  
        self.last_score_gained = points

    def _finish_if_one_active_remains(self, trigger_type: str) -> bool:
        active = self.active_players()
        if len(active) > 1:
            return False

        self.status = "finished"
        if len(active) == 1:
            last = active[0]
            if trigger_type == "exploded":
                bonus = self.deck_remaining() * 1000
                update_player_score(last.name, bonus)
                self.winner = last.id
                self.log.append(
                    f"{last.name} es el último jugador activo: gana automático con un bono de {bonus} puntos."
                )
                self._record_event("auto_win", last, bonus, multiplier=1)
            else:  
                last.eliminated = True
                penalty = self.deck_remaining() * 1000
                update_player_score(last.name, -penalty)
                self.winner = self.last_event["player_id"] if self.last_event else None
                self.log.append(
                    f"Al quedar 1 contra 1 con su rival ya resuelto, {last.name} explota "
                    f"automáticamente y pierde {penalty} puntos."
                )
                self._record_event("auto_exploded", last, penalty)
        else:
            self.winner = None
        return True

    def grant_coin_reward(self, player: "Player") -> int:
        """Otorga la caja fuerte (100/75/50 monedas) por haber acertado el
        propio código. Nunca lleva multiplicador y no hay tope diario: se
        aplica igual en solitario, 1v1 o multijugador. Memoizado por
        jugador+partida para que un refresh no la duplique."""
        if player.id in self.coin_rewards:
            return self.coin_rewards[player.id]
        amount = random.choice(COIN_REWARD_OPTIONS)
        self.coin_rewards[player.id] = amount
        add_coins(player.name, amount)
        return amount

    def _guess_solo(self, cur: "Player", numbers: List[int], correct: List[int]) -> None:
        """Modo solitario: 1 jugador contra el mazo. Gana la mitad de las
        fichas que quedan en el mazo (redondeado hacia abajo) al descifrar
        su propio código. Si falla, la partida termina sin puntos."""
        if numbers == correct:
            cur.resolved = True
            earned_points = (self.deck_remaining() // 2) * 1000
            update_player_score(cur.name, earned_points)
            coins = self.grant_coin_reward(cur)
            self.status = "finished"
            self.winner = cur.id
            self.log.append(
                f"¡{cur.name} descifró su código en solitario! Gana {earned_points} puntos "
                f"(mitad del mazo restante, redondeado hacia abajo) y {coins} monedas."
            )
            self._record_event("correct", cur, earned_points, multiplier=1, coins=coins)
        else:
            cur.eliminated = True
            self.status = "finished"
            self.winner = None
            self.log.append(
                f"{cur.name} falló su código en solitario. Partida terminada sin puntos."
            )
            self._record_event("exploded", cur, 0)

    def guess(self, pid: str, numbers: List[int]) -> None:
        cur = self.get_player(pid)
        if cur is None or cur.eliminated or cur.resolved or self.status != "playing":
            return
        correct = [t["number"] for t in cur.secret]

        if self.mode == "solo":
            self._guess_solo(cur, numbers, correct)
            return

        was_current = self.current_player().id == pid

        if numbers == correct:
            active_opponents = [
                p for p in self.players if p.id != pid and not p.eliminated and not p.resolved
            ]
            multiplier = max(1, len(active_opponents))
            cur.resolved = True

            earned_points = self.deck_remaining() * 1000 * multiplier
            update_player_score(cur.name, earned_points)
            coins = self.grant_coin_reward(cur)
            self.log.append(
                f"¡{cur.name} gritó DESACTIVAR! y acertó. Gana {earned_points} puntos (×{multiplier}) "
                f"y {coins} monedas."
            )
            self._record_event("correct", cur, earned_points, multiplier=multiplier, coins=coins)

            if not self._finish_if_one_active_remains("correct") and was_current:
                self.advance_turn()
                self.phase = "reveal"
            return

        cur.eliminated = True
        lost_points = self.deck_remaining() * 1000
        update_player_score(cur.name, -lost_points)
        self.log.append(
            f"{cur.name} gritó DESACTIVAR! pero falló y pierde {lost_points} puntos. Queda eliminado."
        )
        self._record_event("exploded", cur, lost_points)

        if not self._finish_if_one_active_remains("exploded") and was_current:
            self.advance_turn()
            self.phase = "reveal"

    def state_for(self, viewer_id: str) -> dict:
        cur = self.current_player() if self.players and self.status == "playing" else None
        disc_player = self.get_player(self.disconnect_player_id) if self.disconnect_player_id else None
        return {
            "type": "state",
            "status": self.status,
            "phase": self.phase,
            "room": self.code,
            "mode": self.mode,
            "faceup": self.faceup,
            "colors_available": self.colors_available(),
            "color_hex": COLOR_HEX,
            "current_turn": cur.id if cur else None,
            "current_turn_name": cur.name if cur else None,
            "players": [p.to_dict(viewer_id) for p in self.players],
            "log": self.log[-16:],
            "winner": self.winner,
            "last_score_gained": self.last_score_gained,
            "deck_remaining": self.deck_remaining(),
            "event_seq": self.event_seq,
            "last_event": self.last_event,
            "events": self.events,
            "pending_guess": self.pending_guess,
            "disconnect_player_id": self.disconnect_player_id,
            "disconnect_player_name": disc_player.name if disc_player else None,
            "disconnect_deadline_ms": int(self.disconnect_deadline * 1000) if self.disconnect_deadline else None,
            "chat": self.chat_messages[-50:],
            "your_id": viewer_id,
            "min_players": MIN_PLAYERS,
            "max_players": MAX_PLAYERS,
            "can_start": self.status == "waiting" and (
                self.mode == "solo" or len(self.players) >= MIN_PLAYERS
            ),
            "is_spectator": False,
            "spectator_count": len(self.spectators),
        }

    def spectator_state_for(self) -> dict:
        """Igual que state_for, pero viendo a TODOS los jugadores como si
        fueran oponentes (color + puntitos visibles, número siempre
        oculto — la misma regla que ya aplica entre jugadores). Así el
        espectador ve absolutamente todo lo que pasa en la partida."""
        base = self.state_for("__espectador__")
        base["players"] = [p.to_dict("__espectador__", for_spectator=True) for p in self.players]
        base["your_id"] = None
        base["is_spectator"] = True
        return base


async def broadcast(room: Room):
    for p in room.players:
        if not p.connected:
            continue
        try:
            await p.ws.send_text(json.dumps(room.state_for(p.id)))
        except Exception:
            pass

    if room.spectators:
        payload = json.dumps(room.spectator_state_for())
        caidos = []
        for sid, sock in room.spectators.items():
            try:
                await sock.send_text(payload)
            except Exception:
                caidos.append(sid)
        for sid in caidos:
            room.spectators.pop(sid, None)


async def _watch_disconnect(room_code: str, pid: str, token: int):
    """Espera 20s y, si el jugador sigue desconectado y sigue siendo el
    turno pendiente, lo elimina automáticamente para que la partida siga."""
    await asyncio.sleep(DISCONNECT_TIMEOUT)
    room = rooms.get(room_code)
    if room is None:
        return
    # Si el token ya no coincide, es que el jugador volvió, cambió el turno,
    # o la partida ya terminó: esta cuenta regresiva quedó obsoleta.
    if room.disconnect_token != token or room.disconnect_player_id != pid:
        return
    player = room.get_player(pid)
    if player is None or player.connected or player.eliminated or player.resolved:
        return
    if room.status != "playing":
        return
    room.eliminate_disconnected(player)
    try:
        await broadcast(room)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ENDPOINT DE MATCHMAKING
# ---------------------------------------------------------------------------
@app.get("/matchmake")
async def matchmake():
    global room_counter
    
    # 1. Buscar una sala existente que esté "waiting" y tenga espacio
    for room_id, room in rooms.items():
        if room_id.isdigit(): 
            if room.status == "waiting" and len(room.players) < MAX_PLAYERS:
                return {"room": room_id}
    
    # 2. Si no hay, crear nueva
    new_room_id = f"{room_counter:03d}"
    room_counter += 1
    
    # Inicializamos la sala automáticamente (opcional, pero asegura que el diccionario la reciba)
    rooms[new_room_id] = Room(new_room_id)
    return {"room": new_room_id}


@app.get("/buscar-partida/{player_name}")
async def buscar_partida(player_name: str):
    """Busca si ya existe una partida activa (esperando o en curso) donde
    ese nombre esté jugando, para poder reconectar sin pedir el código de
    sala de nuevo."""
    clean = player_name.strip().lower()
    if not clean:
        return {"found": False}
    for code, room in rooms.items():
        if room.status not in ("waiting", "playing"):
            continue
        for p in room.players:
            if p.name.strip().lower() == clean:
                return {"found": True, "room": code, "mode": room.mode}
    return {"found": False}


@app.get("/salas-en-curso")
async def salas_en_curso():
    """Lista salas con partidas esperando o en curso (incluye modo
    solitario) para que cualquiera pueda entrar como espectador."""
    out = []
    for code, room in rooms.items():
        if room.status not in ("waiting", "playing") or not room.players:
            continue
        out.append({
            "code": code,
            "mode": room.mode,
            "status": room.status,
            "players": len(room.players),
            "spectators": len(room.spectators),
        })
    out.sort(key=lambda r: (r["status"] != "playing", r["code"]))
    return {"rooms": out}


@app.websocket("/ws/lobby/{player_name}")
async def ws_lobby(websocket: WebSocket, player_name: str):
    """Conexión del chat grupal + contador de jugadores en línea. Se abre en
    cuanto el jugador escribe su nombre, antes incluso de elegir sala, y es
    independiente de cualquier partida."""
    await websocket.accept()
    clean_name = (player_name.strip() or "Agente")[:20]
    sid = f"{clean_name}-{random.randint(100000, 999999)}"
    lobby_sockets[sid] = websocket
    _add_online(clean_name)
    await _broadcast_lobby()

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "chat":
                text = str(msg.get("text", "")).strip()[:300]
                if text:
                    global lobby_chat_seq
                    lobby_chat_seq += 1
                    lobby_chat_messages.append({
                        "seq": lobby_chat_seq,
                        "player_name": clean_name,
                        "text": text,
                    })
                    del lobby_chat_messages[:-LOBBY_CHAT_LIMIT]
                    await _broadcast_lobby()
    except WebSocketDisconnect:
        pass
    finally:
        lobby_sockets.pop(sid, None)
        _remove_online(clean_name)
        try:
            await _broadcast_lobby()
        except Exception:
            pass


@app.websocket("/ws/{room_code}/{player_name}")
async def ws_endpoint(websocket: WebSocket, room_code: str, player_name: str):
    await websocket.accept()
    room_mode = "solo" if room_code.upper().startswith("SOLO-") else "multi"
    room = rooms.setdefault(room_code, Room(room_code, mode=room_mode))

    clean_name = player_name.strip()
    player = next((p for p in room.players if p.name.strip().lower() == clean_name.lower()), None)

    if player:
        player.ws = websocket
        player.connected = True
        if room.disconnect_player_id == player.id:
            room._clear_disconnect_timer()
        room.log.append(f"{player.name} se reconectó a la partida.")
        await broadcast(room)
    else:
        if room.status == "playing":
            # La partida ya empezó con este código y el nombre no es de
            # ningún jugador ya en la sala: lo mandamos a entrar como
            # espectador en vez de rechazarlo.
            await websocket.send_text(json.dumps(
                {"type": "redirect_spectator", "room": room_code,
                 "message": "La partida ya comenzó: entrarás como espectador."}
            ))
            await websocket.close()
            return
        if room.status != "waiting" or len(room.players) >= MAX_PLAYERS:
            await websocket.send_text(json.dumps(
                {"type": "error", "message": "La sala ya está llena."}
            ))
            await websocket.close()
            return

        pid = f"{clean_name}-{random.randint(1000, 9999)}"
        avatar, rango = obtener_avatar_y_rango(clean_name)
        player = Player(pid, clean_name, websocket, avatar=avatar, rango=rango)
        room.players.append(player)
        room.log.append(f"{clean_name} se unió a la sala.")
        await broadcast(room)

    _add_online(clean_name)
    await _broadcast_lobby()

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "ping":
                # Solo mantiene viva la conexión (heartbeat desde el celular);
                # no requiere ninguna acción de juego.
                continue

            elif mtype == "start" and room.status == "waiting":
                if room.mode == "solo" or len(room.players) >= MIN_PLAYERS:
                    room.start()
                    await broadcast(room)

            elif mtype == "reveal" and room.status == "playing" and room.phase == "reveal" and room.pending_guess is None:
                if room.current_player().id == player.id:
                    color = msg.get("color")
                    if room.reveal_tile(color) is not None:
                        await broadcast(room)

            elif mtype == "clue" and room.status == "playing" and room.phase == "clue" and room.pending_guess is None:
                if room.current_player().id == player.id:
                    action = msg.get("action")
                    faceup_index = msg.get("faceup_index")
                    ok = False
                    if action == "categorize":
                        ok = room.apply_categorize(faceup_index)
                    elif action == "compare":
                        ok = room.apply_compare(faceup_index, msg.get("slot"))
                    if ok:
                        await broadcast(room)

            elif mtype == "guess" and room.status == "playing":
                numbers = msg.get("numbers", [])

                if room.pending_guess is None:
                    cur = room.get_player(player.id)
                    if cur is not None and not cur.eliminated and not cur.resolved:
                        room.pending_guess = {"player_id": cur.id, "player_name": cur.name}
                        room.log.append(f"{cur.name} intenta desactivar su bomba...")
                        await broadcast(room)

                        async def _resolver_intento_desactivacion(
                            room_code: str = room.code, pid: str = player.id, nums: List[int] = numbers
                        ):
                            # Cuenta regresiva de 5 segundos antes de resolver el intento
                            # (también en modo solitario), para que la animación se vea
                            # en simultáneo con la cuenta regresiva en pantalla.
                            await asyncio.sleep(5)
                            target_room = rooms.get(room_code)
                            if target_room is None:
                                return
                            target_room.pending_guess = None
                            target_room.guess(pid, nums)
                            try:
                                await broadcast(target_room)
                            except Exception:
                                pass

                        asyncio.create_task(_resolver_intento_desactivacion())

            elif mtype == "chat":
                text = str(msg.get("text", ""))
                if text.strip():
                    room.add_chat_message(player, text)
                    await broadcast(room)

            elif mtype == "leave":
                if room.status == "playing":
                    room.leave_game(player)
                elif room.status == "waiting":
                    room.players = [p for p in room.players if p.id != player.id]
                    room.log.append(f"{player.name} salió de la sala.")
                    player.connected = False

                _remove_online(player.name)
                try:
                    await _broadcast_lobby()
                except Exception:
                    pass

                try:
                    await websocket.send_text(json.dumps({"type": "left"}))
                except Exception:
                    pass
                try:
                    await broadcast(room)
                except Exception:
                    pass

                if not any(p.connected for p in room.players):
                    if room.code in rooms:
                        del rooms[room.code]

                try:
                    await websocket.close()
                except Exception:
                    pass
                return

    except WebSocketDisconnect:
        player.connected = False
        # No avisamos en el historial de inmediato: los demás jugadores no
        # deben enterarse de la desconexión hasta que le toque su turno.
        if room.status == "playing" and room.mode == "multi" and not player.eliminated and not player.resolved:
            if room.players and room.current_player().id == player.id:
                room._start_disconnect_timer(player)
        try:
            await broadcast(room)
        except Exception:
            pass

        _remove_online(player.name)
        try:
            await _broadcast_lobby()
        except Exception:
            pass
        
        # Limpieza de memoria (cierre de sala) si todos se desconectaron de esta sala
        if not any(p.connected for p in room.players):
            if room.code in rooms:
                del rooms[room.code]


@app.websocket("/ws/espectador/{room_code}/{viewer_name}")
async def ws_espectador(websocket: WebSocket, room_code: str, viewer_name: str):
    """Conexión de solo lectura: cualquiera puede entrar a ver una partida
    en curso (incluso en solitario) con acceso al chat de la sala, pero
    nunca ve el código de ningún jugador."""
    await websocket.accept()
    room = rooms.get(room_code)
    if room is None or not room.players:
        await websocket.send_text(json.dumps(
            {"type": "error", "message": "Esa sala no existe o todavía no tiene partida."}
        ))
        await websocket.close()
        return

    clean_name = (viewer_name.strip() or "Espectador")[:20]
    sid = f"esp-{clean_name}-{random.randint(100000, 999999)}"
    room.spectators[sid] = websocket

    try:
        await websocket.send_text(json.dumps(room.spectator_state_for()))
    except Exception:
        room.spectators.pop(sid, None)
        return

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "ping":
                continue
            if msg.get("type") == "chat":
                text = str(msg.get("text", ""))
                if text.strip():
                    room.add_spectator_chat_message(clean_name, text)
                    await broadcast(room)
    except WebSocketDisconnect:
        pass
    finally:
        room.spectators.pop(sid, None)


app.mount("/", UTF8StaticFiles(directory="frontend", html=True), name="frontend")
