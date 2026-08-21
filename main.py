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
import string
import sys
import time
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# En Windows, la consola por defecto no es UTF-8 (usa cp1252 o similar). Como
# el resto de este archivo usa emojis en los print() de log (⚠️ ✅ ❌ 🪙...),
# sin esto el proceso truena con UnicodeEncodeError apenas intenta imprimir
# el primer emoji y el servidor nunca llega a levantar.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


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


def verify_supabase_token(token: Optional[str]) -> Optional[str]:
    """Verifica un access_token de Supabase Auth contra el servidor de
    Supabase y devuelve el username autenticado (el mismo con el que la
    persona juega, guardado en user_metadata al registrarse), o None si no
    hay token o no es válido.

    Antes, cada endpoint que mueve monedas/avatares (y el WebSocket de
    partida) confiaba ciegamente en el "username" que mandaba el cliente en
    el body/URL: bastaba con conocer el nombre de otra persona (público en
    el ranking) para comprar/cambiar sus avatares o jugar partidas "a
    nombre de" ella y alterar sus puntos, sin necesitar su contraseña. Con
    esto, la identidad siempre sale del token verificado, nunca de lo que
    el cliente dice ser.

    `token` puede venir como "Bearer <jwt>" (header Authorization) o como
    el jwt pelado (query param del WebSocket, que no admite headers).
    """
    if supabase is None or not token:
        return None
    jwt = token[7:].strip() if token.lower().startswith("bearer ") else token.strip()
    if not jwt:
        return None
    try:
        resp = supabase.auth.get_user(jwt)
    except Exception as e:
        print(f"⚠️  Token de Supabase inválido/expirado: {e}")
        return None
    user = getattr(resp, "user", None) if resp else None
    if user is None:
        return None
    username = (user.user_metadata or {}).get("username")
    return str(username).strip() if username else None


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

# ---------------------------------------------------------------------------
# Catálogo de tableros (carpeta frontend/tableros/) — mismo mecanismo que los
# avatares: el nombre del archivo trae el precio adentro, así que subir una
# imagen nueva a la carpeta la agrega sola a la tienda. Acá el nombre son 6
# dígitos en vez de los ~5 de los avatares (los primeros 3 son el precio
# igual, los últimos 3 son el identificador — cargar_catalogo_avatares() no
# le presta atención al largo total, así que sirve tal cual para los dos).
TABLERO_DIR = os.path.join("frontend", "tableros")
TABLERO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def cargar_catalogo_tableros() -> List[dict]:
    catalogo = []
    if not os.path.isdir(TABLERO_DIR):
        print(f"⚠️  Carpeta de tableros no encontrada: {TABLERO_DIR}")
        return catalogo

    for archivo in sorted(os.listdir(TABLERO_DIR)):
        nombre, ext = os.path.splitext(archivo)
        if ext.lower() not in TABLERO_EXTENSIONS:
            continue
        if len(nombre) < 3 or not nombre[:3].isdigit():
            print(f"⚠️  Ignorado (no respeta la convención de precio): {archivo}")
            continue
        precio = int(nombre[:3])
        catalogo.append({"archivo": archivo, "precio": precio})

    catalogo.sort(key=lambda a: (a["precio"], a["archivo"]))
    return catalogo


CATALOGO_TABLEROS: List[dict] = cargar_catalogo_tableros()

def obtener_rango(puntos: int, victorias: int):
    if victorias == 0:
        return "Novato en Desactivación"

    # 30000 = 60 fichas * PUNTOS_POR_FICHA: cada rango sigue valiendo "una
    # partida ganada con el mazo lleno", ahora que cada ficha vale la mitad.
    indice = (puntos - 1) // (60 * PUNTOS_POR_FICHA) if puntos > 0 else 0
    indice = max(0, min(14, indice))
    return RANGOS[indice]

def update_player_score(username: str, points_change: int,
                         puntos_col: str = "puntos", victorias_col: str = "victorias") -> int:
    """Suma/resta puntos al perfil del usuario autenticado en Supabase.

    El nombre con el que se juega es siempre el "username" con el que la
    persona se registró/inició sesión (ver frontend), así que esta fila ya
    existe desde el registro (la crea un trigger en Supabase). Si por algún
    motivo no existiera, se crea aquí como respaldo.

    puntos_col/victorias_col dejan reusar esta misma función para el
    ranking de Batalla de Avatares (puntos_batalla/victorias_batalla),
    que vive en las mismas filas de "profiles" pero en columnas aparte —
    ver BatallaRoom más abajo. Por defecto sigue siendo el ranking de
    Estratega de Códigos, así que ningún llamado existente cambia.
    """
    if supabase is None:
        print(f"⚠️  No se guardaron los puntos de {username}: Supabase no configurado.")
        return 0

    try:
        resp = supabase.table("profiles").select(f"{puntos_col}, {victorias_col}").eq("username", username).execute()

        if resp.data:
            current_puntos = resp.data[0][puntos_col] or 0
            current_victorias = resp.data[0][victorias_col] or 0
        else:
            current_puntos, current_victorias = 0, 0

        new_puntos = max(0, current_puntos + points_change)
        new_victorias = current_victorias + (1 if points_change > 0 else 0)

        if resp.data:
            update_resp = supabase.table("profiles").update(
                {puntos_col: new_puntos, victorias_col: new_victorias}
            ).eq("username", username).execute()
            if not update_resp.data:
                print(f"⚠️  UPDATE de puntos para {username} no afectó ninguna fila (revisa RLS/policies).")
        else:
            supabase.table("profiles").insert(
                {"username": username, puntos_col: new_puntos, victorias_col: new_victorias}
            ).execute()

        print(f"✅ Puntos ({puntos_col}) de {username} actualizados: {current_puntos} → {new_puntos}")
        return new_puntos
    except Exception as e:
        print(f"❌ ERROR guardando puntos de {username} en Supabase: {e}")
        return 0


# Montos posibles de la caja fuerte (ver grant_coin_reward). Nunca se
# multiplican por el x1/x2/x3 de puntos: son un premio aparte y fijo, y no
# hay tope diario — mientras el jugador siga acertando, sigue ganando.
COIN_REWARD_OPTIONS = [100, 75, 50]
# Batalla de Avatares da partidas más rápidas, así que la caja fuerte paga
# un poco menos para compensar.
COIN_REWARD_OPTIONS_BATALLA = [75, 50, 25]

# Puntos que vale cada ficha restante del mazo al calcular una recompensa o
# penalización en Estratega de Códigos (deck_remaining() * PUNTOS_POR_FICHA).
# Antes era 1000; se bajó a la mitad a pedido del usuario.
PUNTOS_POR_FICHA = 500


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


def grant_coin_reward(rewards: Dict[str, int], player, options: List[int]) -> int:
    """Caja fuerte al ganar: monto al azar entre `options`, memoizado en
    `rewards` (el dict propio de la sala, ej. Room.coin_rewards o
    BatallaRoom.coin_rewards) para que un refresh de página no la duplique.
    Compartida entre los dos juegos — cada Room solo le pasa su propio
    diccionario y su propia lista de montos posibles."""
    if player.id in rewards:
        return rewards[player.id]
    amount = random.choice(options)
    rewards[player.id] = amount
    add_coins(player.name, amount)
    return amount


@app.get("/perfil/{username}")
async def obtener_perfil(username: str):
    """Devuelve puntos/victorias/rango/monedas/avatar acumulados para un
    usuario registrado en Supabase, para los dos juegos: Estratega de
    Códigos (puntos/victorias/rango) y Batalla de Avatares
    (puntos_batalla/victorias_batalla/rango_batalla) — monedas y avatar son
    compartidos entre ambos."""
    if supabase is None:
        rango_cero = obtener_rango(0, 0)
        return {"username": username, "puntos": 0, "victorias": 0, "monedas": 0, "avatar_actual": None,
                "tablero_actual": None, "rango": rango_cero, "puntos_batalla": 0, "victorias_batalla": 0,
                "rango_batalla": rango_cero}

    try:
        resp = supabase.table("profiles").select(
            "puntos, victorias, monedas, avatar_actual, puntos_batalla, victorias_batalla"
        ).eq("username", username).execute()
        fila = resp.data[0] if resp.data else {}
    except Exception as e:
        # Si todavía no se corrió el ALTER TABLE de puntos_batalla/
        # victorias_batalla en Supabase, esta consulta fallaría entera y
        # tiraba abajo /perfil (incluido lo de Estratega de Códigos, que no
        # tiene nada que ver). Se reintenta solo con las columnas viejas
        # para no perder el resto del perfil por eso.
        print(f"⚠️  /perfil de {username}: fallo consultando puntos_batalla/victorias_batalla ({e}); ¿falta el ALTER TABLE? Sigo solo con Estratega de Códigos.")
        try:
            resp = supabase.table("profiles").select(
                "puntos, victorias, monedas, avatar_actual"
            ).eq("username", username).execute()
            fila = resp.data[0] if resp.data else {}
        except Exception as e2:
            print(f"❌ ERROR obteniendo perfil de {username} en Supabase: {e2}")
            fila = {}

    puntos = fila.get("puntos") or 0
    victorias = fila.get("victorias") or 0
    monedas = fila.get("monedas") or 0
    avatar_actual = fila.get("avatar_actual")
    puntos_batalla = fila.get("puntos_batalla") or 0
    victorias_batalla = fila.get("victorias_batalla") or 0

    # Consulta aparte y con su propio try/except (no metida en el select de
    # arriba): si todavía no se corrió el ALTER TABLE de tablero_actual, que
    # falle solo esto y no se lleve puesto el resto del perfil.
    tablero_actual = None
    try:
        resp_tablero = supabase.table("profiles").select("tablero_actual").eq("username", username).execute()
        if resp_tablero.data:
            tablero_actual = resp_tablero.data[0].get("tablero_actual")
    except Exception:
        pass

    return {"username": username, "puntos": puntos, "victorias": victorias, "monedas": monedas,
            "avatar_actual": avatar_actual, "tablero_actual": tablero_actual, "rango": obtener_rango(puntos, victorias),
            "puntos_batalla": puntos_batalla, "victorias_batalla": victorias_batalla,
            "rango_batalla": obtener_rango(puntos_batalla, victorias_batalla)}


def obtener_avatar_y_rango(username: str, puntos_col: str = "puntos", victorias_col: str = "victorias") -> tuple:
    """Consulta rápida al unirse a una sala: trae el avatar_actual, el
    tablero_actual y el rango del jugador para mandarlos junto con su
    ficha a los demás. puntos_col/victorias_col permite pedir el rango de
    Batalla de Avatares en vez del de Estratega de Códigos sin duplicar
    esta función."""
    if supabase is None:
        return None, obtener_rango(0, 0), None
    try:
        resp = supabase.table("profiles").select(
            f"{puntos_col}, {victorias_col}, avatar_actual, tablero_actual"
        ).eq("username", username).execute()
    except Exception:
        # Probablemente falta el ALTER TABLE de tablero_actual todavía:
        # seguimos andando igual (avatar/rango son más importantes), solo
        # sin tablero de fondo hasta que se corra la migración.
        try:
            resp = supabase.table("profiles").select(
                f"{puntos_col}, {victorias_col}, avatar_actual"
            ).eq("username", username).execute()
        except Exception as e2:
            print(f"❌ ERROR obteniendo avatar/rango de {username}: {e2}")
            return None, obtener_rango(0, 0), None
    if not resp.data:
        return None, obtener_rango(0, 0), None
    fila = resp.data[0]
    puntos = fila.get(puntos_col) or 0
    victorias = fila.get(victorias_col) or 0
    return fila.get("avatar_actual"), obtener_rango(puntos, victorias), fila.get("tablero_actual")


@app.get("/ranking")
async def obtener_ranking(limite: int = 50, juego: str = "codigos"):
    """Tabla de posiciones: nombre, rango, puntos y avatar actual de cada
    jugador registrado, ordenados de mayor a menor puntaje.
    juego="batalla" trae el ranking separado de Batalla de Avatares
    (columnas puntos_batalla/victorias_batalla) en vez del de Estratega de
    Códigos — mismo endpoint, dos rankings independientes."""
    if supabase is None:
        return {"ranking": []}

    puntos_col = "puntos_batalla" if juego == "batalla" else "puntos"
    victorias_col = "victorias_batalla" if juego == "batalla" else "victorias"

    try:
        resp = (
            supabase.table("profiles")
            .select(f"username, {puntos_col}, {victorias_col}, avatar_actual")
            .order(puntos_col, desc=True)
            .limit(max(1, min(limite, 200)))
            .execute()
        )
    except Exception as e:
        print(f"❌ ERROR obteniendo ranking de Supabase: {e}")
        return {"ranking": []}

    ranking = []
    for i, fila in enumerate(resp.data or []):
        puntos = fila.get(puntos_col) or 0
        victorias = fila.get(victorias_col) or 0
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
async def comprar_avatar(payload: dict, authorization: Optional[str] = Header(None)):
    """Compra (o reclama gratis la primera vez) un avatar del catálogo.

    Regla de "primer avatar gratis": si el jugador todavía no tiene ningún
    avatar comprado, puede elegir cualquiera de precio 150 sin que se le
    cobren monedas. De ahí en adelante todo se cobra normal — esto se
    detecta solo con "¿ya tiene algo comprado?", sin bandera aparte.
    """
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    # La identidad sale del token verificado, no del "username" del body:
    # ese campo del body ya no se usa para nada (ver verify_supabase_token).
    username = verify_supabase_token(authorization)
    if username is None:
        return JSONResponse(status_code=401, content={
            "ok": False, "error": "Sesión inválida o expirada: vuelve a iniciar sesión."
        })

    archivo = str(payload.get("archivo", "")).strip()
    if not archivo:
        return {"ok": False, "error": "Falta el archivo."}

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
async def elegir_avatar(payload: dict, authorization: Optional[str] = Header(None)):
    """Cambia el avatar_actual del jugador a uno que ya tiene comprado."""
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    username = verify_supabase_token(authorization)
    if username is None:
        return JSONResponse(status_code=401, content={
            "ok": False, "error": "Sesión inválida o expirada: vuelve a iniciar sesión."
        })

    archivo = str(payload.get("archivo", "")).strip()
    if not archivo:
        return {"ok": False, "error": "Falta el archivo."}

    perfil = _obtener_perfil_avatares(username)
    if archivo not in perfil["avatares_comprados"]:
        return {"ok": False, "error": "No has comprado ese avatar todavía."}

    try:
        supabase.table("profiles").update({"avatar_actual": archivo}).eq("username", username).execute()
    except Exception as e:
        print(f"❌ ERROR cambiando avatar de {username}: {e}")
        return {"ok": False, "error": "Error guardando el cambio."}

    return {"ok": True, "avatar_actual": archivo}


# ---------------------------------------------------------------------------
# Tienda de tableros — mismo patrón que la de avatares de arriba, calcado
# endpoint por endpoint. Sin avatar gratis acá (no hace falta: el jugador ya
# puede jugar sin tablero, se queda con el fondo por defecto).
# ---------------------------------------------------------------------------
def _obtener_perfil_tableros(username: str) -> dict:
    resp = supabase.table("profiles").select(
        "monedas, tablero_actual, tableros_comprados"
    ).eq("username", username).execute()
    if resp.data:
        fila = resp.data[0]
        return {
            "monedas": fila.get("monedas") or 0,
            "tablero_actual": fila.get("tablero_actual"),
            "tableros_comprados": fila.get("tableros_comprados") or [],
        }
    return {"monedas": 0, "tablero_actual": None, "tableros_comprados": []}


@app.get("/tienda-tableros/{username}")
async def tienda_tableros(username: str):
    if supabase is None:
        return {"catalogo": [{**t, "comprado": False} for t in CATALOGO_TABLEROS], "monedas": 0}

    perfil = _obtener_perfil_tableros(username)
    comprados = set(perfil["tableros_comprados"])
    catalogo = [{**t, "comprado": t["archivo"] in comprados} for t in CATALOGO_TABLEROS]
    return {"catalogo": catalogo, "monedas": perfil["monedas"]}


@app.get("/mis-tableros/{username}")
async def tableros_del_jugador(username: str):
    """Selector: solo los tableros que este jugador ya compró, más cuál
    tiene puesto ahora mismo (puede ser None: sin tablero, fondo por
    defecto).

    OJO: este endpoint NO puede llamarse "/tableros/{username}" — las
    imágenes de los tableros se sirven como archivos estáticos bajo ese
    mismo prefijo (frontend/tableros/300001.png -> /tableros/300001.png,
    vía el StaticFiles montado en "/"), así que "/tableros/{username}"
    le "robaba" esas rutas: /tableros/300001.png se resolvía contra este
    endpoint (con "300001.png" como si fuera el username) en vez de
    servir la imagen, y por eso los tableros nunca se veían."""
    if supabase is None:
        return {"tableros": [], "tablero_actual": None}
    perfil = _obtener_perfil_tableros(username)
    return {"tableros": perfil["tableros_comprados"], "tablero_actual": perfil["tablero_actual"]}


@app.post("/tienda-tableros/comprar")
async def comprar_tablero(payload: dict, authorization: Optional[str] = Header(None)):
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    username = verify_supabase_token(authorization)
    if username is None:
        return JSONResponse(status_code=401, content={
            "ok": False, "error": "Sesión inválida o expirada: vuelve a iniciar sesión."
        })

    archivo = str(payload.get("archivo", "")).strip()
    if not archivo:
        return {"ok": False, "error": "Falta el archivo."}

    item = next((t for t in CATALOGO_TABLEROS if t["archivo"] == archivo), None)
    if item is None:
        return {"ok": False, "error": "Ese tablero no existe en el catálogo."}

    perfil = _obtener_perfil_tableros(username)
    comprados = list(perfil["tableros_comprados"])
    if archivo in comprados:
        return {"ok": False, "error": "Ya tienes ese tablero."}
    if perfil["monedas"] < item["precio"]:
        return {"ok": False, "error": "No te alcanzan las monedas."}

    nuevas_monedas = perfil["monedas"] - item["precio"]
    comprados.append(archivo)
    # A diferencia del avatar, comprar un tablero NO lo pone puesto solo —
    # el jugador ya está jugando sin tablero (fondo por defecto) y puede
    # preferir seguir así hasta elegirlo a propósito.
    try:
        supabase.table("profiles").update({
            "monedas": nuevas_monedas,
            "tableros_comprados": comprados,
        }).eq("username", username).execute()
    except Exception as e:
        print(f"❌ ERROR comprando tablero de {username}: {e}")
        return {"ok": False, "error": "Error guardando la compra."}

    print(f"🖼️  {username} compró el tablero {archivo}.")
    return {"ok": True, "monedas": nuevas_monedas}


@app.post("/tablero/elegir")
async def elegir_tablero(payload: dict, authorization: Optional[str] = Header(None)):
    """Cambia el tablero_actual del jugador a uno que ya tiene comprado, o
    lo quita (archivo vacío/null) para volver al fondo por defecto."""
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    username = verify_supabase_token(authorization)
    if username is None:
        return JSONResponse(status_code=401, content={
            "ok": False, "error": "Sesión inválida o expirada: vuelve a iniciar sesión."
        })

    archivo = str(payload.get("archivo") or "").strip() or None

    if archivo is not None:
        perfil = _obtener_perfil_tableros(username)
        if archivo not in perfil["tableros_comprados"]:
            return {"ok": False, "error": "No has comprado ese tablero todavía."}

    try:
        supabase.table("profiles").update({"tablero_actual": archivo}).eq("username", username).execute()
    except Exception as e:
        print(f"❌ ERROR cambiando tablero de {username}: {e}")
        return {"ok": False, "error": "Error guardando el cambio."}

    return {"ok": True, "tablero_actual": archivo}


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
async def solicitar_avatar(payload: dict, authorization: Optional[str] = Header(None)):
    """Registra el pedido de un avatar nuevo. No se borran filas viejas: al
    chequear el cooldown simplemente se ignoran las de más de 15 días, así
    se conserva el historial completo de pedidos."""
    if supabase is None:
        return {"ok": False, "error": "Supabase no configurado."}

    username = verify_supabase_token(authorization)
    if username is None:
        return JSONResponse(status_code=401, content={
            "ok": False, "error": "Sesión inválida o expirada: vuelve a iniciar sesión."
        })

    texto = str(payload.get("texto", "")).strip()[:500]
    if not texto:
        return {"ok": False, "error": "Falta el texto del pedido."}

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
MATCHMAKE_PREFIX = "MM-"  # distingue salas de emparejamiento automático de códigos privados

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

    def reveal_tile(self) -> Optional[dict]:
        """Saca una ficha al azar del mazo restante (ya no se elige color).

        Cada color_piles[c] ya viene barajado (ver start()), así que para
        que la probabilidad sea uniforme sobre TODAS las fichas restantes
        -no solo "1 de 5 colores por igual"- el color se sortea con peso
        según cuántas fichas le quedan a cada uno, y recién ahí se saca la
        de arriba de ese color. Con eso, un color con más fichas restantes
        tiene proporcionalmente más chances, igual que si todo el mazo
        estuviera mezclado en un solo montón.
        """
        colores_con_fichas = [c for c in COLORS if self.color_piles[c]]
        if not colores_con_fichas:
            return None
        pesos = [len(self.color_piles[c]) for c in colores_con_fichas]
        color = random.choices(colores_con_fichas, weights=pesos, k=1)[0]
        n = self.color_piles[color].pop()
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
                bonus = self.deck_remaining() * PUNTOS_POR_FICHA
                update_player_score(last.name, bonus)
                self.winner = last.id
                self.log.append(
                    f"{last.name} es el último jugador activo: gana automático con un bono de {bonus} puntos."
                )
                self._record_event("auto_win", last, bonus, multiplier=1)
            else:  
                last.eliminated = True
                penalty = self.deck_remaining() * PUNTOS_POR_FICHA
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
        jugador+partida para que un refresh no la duplique. (Ver
        grant_coin_reward() a nivel de módulo — BatallaRoom usa la misma
        función con su propia lista de montos.)"""
        return grant_coin_reward(self.coin_rewards, player, COIN_REWARD_OPTIONS)

    def _guess_solo(self, cur: "Player", numbers: List[int], correct: List[int]) -> None:
        """Modo solitario: 1 jugador contra el mazo. Gana la mitad de las
        fichas que quedan en el mazo (redondeado hacia abajo) al descifrar
        su propio código. Si falla, la partida termina sin puntos."""
        if numbers == correct:
            cur.resolved = True
            earned_points = (self.deck_remaining() // 2) * PUNTOS_POR_FICHA
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

            earned_points = self.deck_remaining() * PUNTOS_POR_FICHA * multiplier
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
        lost_points = self.deck_remaining() * PUNTOS_POR_FICHA
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
            "game": "codigos",
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


async def broadcast(room):
    """Genérico para cualquier tipo de sala (Room de Estratega de Códigos o
    BatallaRoom): a ambas les alcanza con implementar .players, .spectators,
    .state_for(viewer_id) y .spectator_state_for() con la misma forma. Los
    jugadores bot de Batalla no tienen socket (ws=None): se saltan acá, no
    hace falta que cada sala se acuerde de filtrarlos."""
    for p in room.players:
        if not p.connected or p.ws is None:
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


# =============================================================================
# BATALLA DE AVATARES — segundo juego. Comparte servidor, cuenta, monedas,
# avatar elegido y espacio de códigos de sala con Estratega de Códigos (ver
# `rooms`, más abajo se unifican en el mismo diccionario), pero tiene su
# propio ranking de puntos (puntos_batalla/victorias_batalla en "profiles")
# y su propia lógica de juego, totalmente independiente.
#
# Reglas (resumen — manual completo de la sala aparte): cada jugador es un
# personaje (su avatar), representado por 5 copias idénticas ("en pie" /
# "fuera de combate"). Se juega con un mazo de 57 cartas de acción (acá no
# hace falta modelar las cartas de "Personaje" del manual físico: en la
# versión digital el personaje es directamente el contador de 5 fichas de
# cada jugador). Cada turno: jugar 1 carta con su efecto, o descartar
# cualquier cantidad y robar esa misma cantidad sin efecto. Gana quien deja
# a los 5 personajes del rival Fuera de Combate.
# =============================================================================

BATALLA_CARTAS = {
    "bomba":        {"nombre": "Bomba",                       "emoji": "💣", "cantidad": 19},
    "hada":         {"nombre": "Hada Curandera",              "emoji": "🧚", "cantidad": 8},
    "bombardeo":    {"nombre": "Bombardeo General",           "emoji": "☢️", "cantidad": 3},
    "misil":        {"nombre": "Misil Teledirigido",          "emoji": "🚀", "cantidad": 8},
    "escudo":       {"nombre": "Escudo",                      "emoji": "🛡️", "cantidad": 10},
    "campo_fuerza": {"nombre": "Campo de Fuerza",             "emoji": "💠", "cantidad": 5},
    "dron":         {"nombre": "Dron Antiaéreo",              "emoji": "🛸", "cantidad": 6},
    "campo_dron":   {"nombre": "Campo de Fuerza del Dron",    "emoji": "🌀", "cantidad": 3},
}
# Orden de "pelado" de capas de defensa, de la más externa a la más interna.
# La Bomba solo llega hasta las dos terrestres (viene por tierra, el Dron no
# la detecta); el Misil y el Bombardeo General recorren la cadena completa.
BATALLA_CAPAS_AEREAS = ("campo_dron", "dron")
BATALLA_CAPAS_TERRESTRES = ("campo_fuerza", "escudo")
BATALLA_MANO_SIZE = 3
BATALLA_PERSONAJES_INICIALES = 5
BATALLA_MATCHMAKE_TIMEOUT = 10  # segundos de espera antes de completar con un bot

# Nombres para el bot de relleno del matchmaking: tienen que sonar a
# jugador de verdad (el usuario no debería poder distinguir a simple vista
# que está jugando contra un bot).
BATALLA_NOMBRES_BOT = [
    "Vale_23", "ElCraneo", "Nano.gg", "LunaPixel", "Ryu_Fan99", "Kenji_X",
    "MoritaGamer", "DarkTaco", "Pepper_XY", "Zoe.exe", "ChispaAzul",
    "Rojo_Fantasma", "Mika_88", "ElNene_Pro", "Andy_Vortex", "Toti_Uy",
]


def _crear_mazo_batalla() -> List[str]:
    mazo = []
    for tipo, info in BATALLA_CARTAS.items():
        mazo.extend([tipo] * info["cantidad"])
    random.shuffle(mazo)
    return mazo


def _personaje_nuevo() -> dict:
    return {"estado": "pie", "escudo": False, "campo_fuerza": False, "dron": False, "campo_dron": False}


def _pelar_capa(p: dict, capas) -> Optional[str]:
    """Rompe la capa de defensa más externa que tenga `p` dentro de la
    secuencia `capas` (ya ordenada de afuera hacia adentro) y devuelve su
    nombre, o None si no le queda ninguna de esas capas."""
    for capa in capas:
        if p[capa]:
            p[capa] = False
            return capa
    return None


BATALLA_NOMBRE_CAPA = {
    "campo_dron": "Campo de Fuerza del Dron",
    "dron": "Dron Antiaéreo",
    "campo_fuerza": "Campo de Fuerza",
    "escudo": "Escudo",
}


class BatallaPlayer:
    def __init__(self, pid: str, name: str, ws: Optional[WebSocket], avatar: Optional[str] = None,
                 rango: Optional[str] = None, is_bot: bool = False, tablero: Optional[str] = None):
        self.id = pid
        self.name = name
        self.ws = ws
        self.avatar = avatar
        self.rango = rango or obtener_rango(0, 0)
        self.is_bot = is_bot
        self.tablero = tablero
        self.connected = True
        self.en_pie = BATALLA_PERSONAJES_INICIALES
        self.personajes: List[dict] = [_personaje_nuevo() for _ in range(BATALLA_PERSONAJES_INICIALES)]
        self.mano: List[str] = []

    def to_dict(self, viewer_id: str) -> dict:
        # A diferencia de Estratega de Códigos, acá no hay ningún código
        # secreto que ocultar: el estado del escuadrón (en pie/caído,
        # capas de defensa) es información pública para ambos
        # jugadores y para cualquier espectador, tal como en el juego de
        # mesa físico. Lo único privado es qué cartas tiene cada uno en la
        # mano — esas solo se mandan al dueño.
        es_propio = viewer_id == self.id
        return {
            "id": self.id,
            "name": self.name,
            "avatar": self.avatar,
            "rango": self.rango,
            "tablero": self.tablero,
            "en_pie": self.en_pie,
            "personajes": self.personajes,
            "mano_count": len(self.mano),
            "mano": self.mano if es_propio else None,
            "connected": self.connected,
            "is_bot": self.is_bot,
            "is_you": es_propio,
        }


class BatallaRoom:
    def __init__(self, code: str):
        self.code = code
        self.game = "batalla"
        self.mode = "batalla"  # por si algo genérico mira room.mode (salas-en-curso, etc.)
        self.players: List[BatallaPlayer] = []
        self.status = "waiting"
        self.mazo: List[str] = []
        self.descarte: List[str] = []
        self.turn_index = 0
        self.winner: Optional[str] = None
        self.log: List[str] = []
        self.coin_rewards: Dict[str, int] = {}
        self.points_gained = 0
        self.coins_gained = 0
        self.matchmaking_deadline: Optional[float] = None
        self.chat_messages: List[dict] = []
        self.chat_seq = 0
        self.spectators: Dict[str, WebSocket] = {}
        # Piedra/Papel/Tijera para decidir quién arranca (status == "rps").
        self.rps_choices: Dict[str, str] = {}
        self.rps_resultado: Optional[dict] = None
        # Revancha al terminar (status == "finished"): si los dos la piden
        # se reinicia la sala entera, de vuelta al piedra/papel/tijera.
        self.rematch_requested: Dict[str, bool] = {}
        # Última carta jugada, en forma estructurada — el frontend la usa
        # para saber exactamente qué animar (ver jugar_carta). action_seq
        # sube en cada jugada para que el cliente note que es una jugada
        # nueva y no la misma de antes repetida en otro broadcast.
        self.action_seq = 0
        self.last_action: Optional[dict] = None

    def get_player(self, pid: str) -> Optional[BatallaPlayer]:
        return next((p for p in self.players if p.id == pid), None)

    def current_player(self) -> BatallaPlayer:
        return self.players[self.turn_index]

    def _rival(self, jugador: BatallaPlayer) -> Optional[BatallaPlayer]:
        return next((p for p in self.players if p.id != jugador.id), None)

    def add_chat_message(self, sender: "BatallaPlayer", text: str) -> None:
        text = text.strip()[:300]
        if not text:
            return
        self.chat_seq += 1
        self.chat_messages.append({
            "seq": self.chat_seq, "player_id": sender.id, "player_name": sender.name,
            "text": text, "is_spectator": False,
        })
        self.chat_messages = self.chat_messages[-100:]

    def add_spectator_chat_message(self, viewer_name: str, text: str) -> None:
        text = text.strip()[:300]
        if not text:
            return
        self.chat_seq += 1
        self.chat_messages.append({
            "seq": self.chat_seq, "player_id": None, "player_name": viewer_name,
            "text": text, "is_spectator": True,
        })
        self.chat_messages = self.chat_messages[-100:]

    def agregar_bot(self) -> None:
        nombre = random.choice(BATALLA_NOMBRES_BOT)
        avatar = random.choice(CATALOGO_AVATARES)["archivo"] if CATALOGO_AVATARES else None
        tablero = random.choice(CATALOGO_TABLEROS)["archivo"] if CATALOGO_TABLEROS else None
        pid = f"bot-{random.randint(100000, 999999)}"
        bot = BatallaPlayer(pid, nombre, None, avatar=avatar, rango=obtener_rango(0, 0), is_bot=True, tablero=tablero)
        self.players.append(bot)
        self.log.append(f"{nombre} se unió a la sala.")

    def start(self, primero: Optional[str] = None) -> None:
        self.mazo = _crear_mazo_batalla()
        self.descarte = []
        for jugador in self.players:
            jugador.en_pie = BATALLA_PERSONAJES_INICIALES
            jugador.personajes = [_personaje_nuevo() for _ in range(BATALLA_PERSONAJES_INICIALES)]
            jugador.mano = [self.mazo.pop() for _ in range(BATALLA_MANO_SIZE)]
        if primero is not None and self.get_player(primero) is not None:
            self.turn_index = self.players.index(self.get_player(primero))
        else:
            self.turn_index = random.randint(0, 1)
        self.status = "playing"
        self.matchmaking_deadline = None
        self.winner = None
        self.points_gained = 0
        self.coins_gained = 0
        self.coin_rewards = {}
        self.rps_choices = {}
        self.rps_resultado = None
        self.rematch_requested = {}
        self.action_seq = 0
        self.last_action = None
        self.log.append("¡La batalla ha comenzado! Que gane el mejor escuadrón.")

    def iniciar_rps(self) -> None:
        """Antes de empezar (o de una revancha) se juega piedra/papel/
        tijera para decidir quién tiene el primer turno."""
        self.status = "rps"
        self.rps_choices = {}
        self.rps_resultado = None
        self.matchmaking_deadline = None
        # Si esto es una revancha, la última jugada de la partida anterior
        # (ej. el Bombardeo que la terminó) seguía viajando en el estado —
        # el cliente la volvía a animar al entrar acá, y encima eso le
        # descuadraba el contador para la primera jugada de la partida
        # nueva (nunca se llegaba a animar porque el seq quedaba "gastado"
        # en la jugada vieja).
        self.action_seq = 0
        self.last_action = None
        self.log.append("¡Piedra, papel o tijera para ver quién empieza!")

    def elegir_rps(self, pid: str, opcion: str) -> Tuple[bool, str]:
        if self.status != "rps":
            return False, "No estamos en la elección de quién empieza."
        if opcion not in ("piedra", "papel", "tijera"):
            return False, "Elegí piedra, papel o tijera."
        if self.get_player(pid) is None:
            return False, "No estás en esta sala."
        if pid in self.rps_choices:
            return False, "Ya elegiste."
        self.rps_resultado = None
        self.rps_choices[pid] = opcion
        if len(self.rps_choices) < len(self.players):
            return True, ""

        gana_a = {"piedra": "tijera", "papel": "piedra", "tijera": "papel"}
        a, b = self.players[0], self.players[1]
        ea, eb = self.rps_choices[a.id], self.rps_choices[b.id]
        if ea == eb:
            self.rps_resultado = {"empate": True, "choices": dict(self.rps_choices)}
            self.rps_choices = {}
            self.log.append(f"Piedra/papel/tijera: empate ({ea} contra {eb}), tiran de nuevo.")
        else:
            ganador = a if gana_a[ea] == eb else b
            self.rps_resultado = {"empate": False, "choices": dict(self.rps_choices), "ganador": ganador.id}
            self.log.append(f"Piedra/papel/tijera: {ganador.name} gana ({ea} contra {eb}) y empieza.")
        return True, ""

    def pedir_revancha(self, pid: str) -> Tuple[bool, str]:
        if self.status != "finished":
            return False, "La partida todavía no terminó."
        jugador = self.get_player(pid)
        if jugador is None:
            return False, "No estás en esta sala."
        self.rematch_requested[pid] = True
        rival = self._rival(jugador)
        # Contra un bot no tiene sentido hacerlo esperar: acepta al toque.
        if rival and rival.is_bot:
            self.rematch_requested[rival.id] = True
        if rival and self.rematch_requested.get(rival.id):
            self.iniciar_rps()
        return True, ""

    def _reponer_mano(self, jugador: BatallaPlayer) -> None:
        while len(jugador.mano) < BATALLA_MANO_SIZE:
            if not self.mazo:
                if not self.descarte:
                    break
                self.mazo = self.descarte
                self.descarte = []
                random.shuffle(self.mazo)
            jugador.mano.append(self.mazo.pop())

    def _slot_valido(self, jugador: BatallaPlayer, slot) -> bool:
        return isinstance(slot, int) and 0 <= slot < len(jugador.personajes)

    def jugar_carta(self, pid: str, carta: str, objetivo: dict) -> Tuple[bool, str]:
        """objetivo: {"slot": int} — sobre un personaje propio (hada,
        escudo, campo_fuerza, dron, campo_dron) o de un rival (bomba,
        misil). Bombardeo no
        necesita objetivo. Devuelve (ok, motivo_si_falló)."""
        cur = self.get_player(pid)
        if cur is None or self.status != "playing" or self.current_player().id != pid:
            return False, "No es tu turno."
        if carta not in cur.mano:
            return False, "No tienes esa carta."
        rival = self._rival(cur)
        objetivo = objetivo or {}
        slot = objetivo.get("slot")
        # Se arma en cada rama y se guarda al final en self.last_action —
        # es lo único que el frontend necesita para saber exactamente qué
        # animar (quién atacó/protegió a quién con qué, y qué pasó), sin
        # tener que adivinar comparando el tablero de antes y de después.
        efecto: dict = {}

        if carta == "bomba":
            if rival is None or not self._slot_valido(rival, slot):
                return False, "Elige un personaje del rival."
            p = rival.personajes[slot]
            if p["estado"] != "pie":
                return False, "Ese personaje ya está Fuera de Combate."
            # La Bomba viene por tierra: el Dron y su Campo de Fuerza no la
            # detectan, así que se salta directo a las capas terrestres.
            capa = _pelar_capa(p, BATALLA_CAPAS_TERRESTRES)
            efecto = {"objetivo_id": rival.id, "slot": slot, "capa": capa, "derribado": capa is None}
            if capa:
                self.log.append(
                    f"{cur.name} lanzó una Bomba y destruyó el {BATALLA_NOMBRE_CAPA[capa]} "
                    f"de un personaje de {rival.name}."
                )
            else:
                p["estado"] = "caido"
                rival.en_pie -= 1
                self.log.append(f"{cur.name} lanzó una Bomba y dejó Fuera de Combate a un personaje de {rival.name}.")

        elif carta == "hada":
            if not self._slot_valido(cur, slot) or cur.personajes[slot]["estado"] != "caido":
                return False, "Elige uno de tus personajes que esté Fuera de Combate."
            cur.personajes[slot]["estado"] = "pie"
            cur.en_pie += 1
            efecto = {"objetivo_id": cur.id, "slot": slot}
            self.log.append(f"{cur.name} revivió a un personaje con una Hada Curandera.")

        elif carta == "bombardeo":
            efectos = []
            for jugador in self.players:
                for i, p in enumerate(jugador.personajes):
                    if p["estado"] == "pie":
                        capa = _pelar_capa(p, BATALLA_CAPAS_AEREAS + BATALLA_CAPAS_TERRESTRES)
                        if not capa:
                            p["estado"] = "caido"
                            jugador.en_pie -= 1
                        efectos.append({"jugador_id": jugador.id, "slot": i, "capa": capa, "derribado": capa is None})
            efecto = {"efectos": efectos}
            self.log.append(f"{cur.name} soltó un Bombardeo General sobre toda la mesa.")

        elif carta == "escudo":
            if not self._slot_valido(cur, slot) or cur.personajes[slot]["escudo"]:
                return False, "Elige un personaje tuyo que todavía no tenga Escudo."
            cur.personajes[slot]["escudo"] = True
            efecto = {"objetivo_id": cur.id, "slot": slot}
            self.log.append(f"{cur.name} protegió a un personaje con un Escudo.")

        elif carta == "campo_fuerza":
            if not self._slot_valido(cur, slot):
                return False, "Objetivo inválido."
            p = cur.personajes[slot]
            if not p["escudo"] or p["campo_fuerza"]:
                return False, "Elige un personaje tuyo que ya tenga Escudo y no tenga Campo de Fuerza."
            p["campo_fuerza"] = True
            efecto = {"objetivo_id": cur.id, "slot": slot}
            self.log.append(f"{cur.name} reforzó un Escudo con un Campo de Fuerza.")

        elif carta == "dron":
            if not self._slot_valido(cur, slot) or cur.personajes[slot]["dron"]:
                return False, "Elige un personaje tuyo que todavía no tenga Dron Antiaéreo."
            cur.personajes[slot]["dron"] = True
            efecto = {"objetivo_id": cur.id, "slot": slot}
            self.log.append(f"{cur.name} desplegó un Dron Antiaéreo.")

        elif carta == "campo_dron":
            if not self._slot_valido(cur, slot):
                return False, "Objetivo inválido."
            p = cur.personajes[slot]
            if not p["dron"] or p["campo_dron"]:
                return False, "Elige un personaje tuyo que ya tenga Dron Antiaéreo y no tenga Campo de Fuerza del Dron."
            p["campo_dron"] = True
            efecto = {"objetivo_id": cur.id, "slot": slot}
            self.log.append(f"{cur.name} reforzó un Dron Antiaéreo con un Campo de Fuerza.")

        elif carta == "misil":
            if rival is None or not self._slot_valido(rival, slot):
                return False, "Objetivo inválido."
            p = rival.personajes[slot]
            if p["estado"] != "pie":
                return False, "Ese personaje ya está Fuera de Combate."
            capa = _pelar_capa(p, BATALLA_CAPAS_AEREAS + BATALLA_CAPAS_TERRESTRES)
            efecto = {"objetivo_id": rival.id, "slot": slot, "capa": capa, "derribado": capa is None}
            if capa:
                self.log.append(
                    f"{cur.name} disparó un Misil y destruyó el {BATALLA_NOMBRE_CAPA[capa]} "
                    f"de un personaje de {rival.name}."
                )
            else:
                p["estado"] = "caido"
                rival.en_pie -= 1
                self.log.append(f"{cur.name} disparó un Misil y dejó Fuera de Combate a un personaje de {rival.name}.")

        else:
            return False, "Carta desconocida."

        self.action_seq += 1
        self.last_action = {"seq": self.action_seq, "jugador_id": cur.id, "carta": carta, **efecto}

        cur.mano.remove(carta)
        self.descarte.append(carta)
        self._reponer_mano(cur)
        if not self._revisar_victoria():
            self.turn_index = 1 - self.turn_index
        return True, ""

    def _carta_jugable(self, jugador: BatallaPlayer, rival: Optional[BatallaPlayer], carta: str) -> bool:
        """¿Tiene esta carta algún objetivo legal ahora mismo? Se usa para
        no dejar descartar mientras haya al menos una jugada real posible
        (evita el "no ataco nunca y descarto para siempre")."""
        if carta in ("bomba", "misil"):
            return rival is not None and any(p["estado"] == "pie" for p in rival.personajes)
        if carta == "hada":
            return any(p["estado"] == "caido" for p in jugador.personajes)
        if carta == "escudo":
            return any(not p["escudo"] for p in jugador.personajes)
        if carta == "campo_fuerza":
            return any(p["escudo"] and not p["campo_fuerza"] for p in jugador.personajes)
        if carta == "dron":
            return any(not p["dron"] for p in jugador.personajes)
        if carta == "campo_dron":
            return any(p["dron"] and not p["campo_dron"] for p in jugador.personajes)
        if carta == "bombardeo":
            return True
        return False

    def tiene_jugada_legal(self, jugador: BatallaPlayer) -> bool:
        rival = self._rival(jugador)
        return any(self._carta_jugable(jugador, rival, c) for c in jugador.mano)

    def descartar_y_robar(self, pid: str, cartas: List[str]) -> Tuple[bool, str]:
        cur = self.get_player(pid)
        if cur is None or self.status != "playing" or self.current_player().id != pid:
            return False, "No es tu turno."
        if not cartas:
            return False, "Elige al menos una carta para descartar."
        if self.tiene_jugada_legal(cur):
            return False, "Tenés al menos una carta jugable: no podés descartar sin jugarla."
        restante = list(cur.mano)
        for c in cartas:
            if c not in restante:
                return False, "No tienes esa carta."
            restante.remove(c)
        for c in cartas:
            cur.mano.remove(c)
            self.descarte.append(c)
        self._reponer_mano(cur)
        self.log.append(f"{cur.name} descartó {len(cartas)} carta(s) y robó de nuevo.")
        self.turn_index = 1 - self.turn_index
        return True, ""

    def leave_game(self, player: "BatallaPlayer") -> None:
        """Si se va a mitad de partida pierde automáticamente (gana el
        rival, igual de espíritu que Room.leave_game en Estratega de
        Códigos). Si todavía se estaba esperando rival, simplemente se lo
        saca de la sala."""
        if self.status == "playing":
            for p in player.personajes:
                p["estado"] = "caido"
            player.en_pie = 0
            self._revisar_victoria()
        elif self.status in ("waiting", "rps"):
            self.players = [p for p in self.players if p.id != player.id]
            self.status = "waiting"
            self.rps_choices = {}
            self.rps_resultado = None
            self.log.append(f"{player.name} salió de la sala.")

    def _revisar_victoria(self) -> bool:
        caidos = [j for j in self.players if j.en_pie <= 0]
        if not caidos:
            return False
        if len(caidos) == len(self.players):
            # Empate: un Bombardeo General dejó a los dos escuadrones en
            # cero a la vez. Nadie gana, nadie pierde: no se reparten
            # puntos ni monedas para que no quede como una victoria trucha.
            self.status = "finished"
            self.winner = None
            self.log.append("¡Empate! El Bombardeo General dejó a los dos escuadrones Fuera de Combate a la vez.")
            return True
        ganador = self._rival(caidos[0])
        self.status = "finished"
        self.winner = ganador.id if ganador else None
        if ganador:
            self.points_gained = 100 * ganador.en_pie
            update_player_score(ganador.name, self.points_gained, "puntos_batalla", "victorias_batalla")
            self.coins_gained = grant_coin_reward(self.coin_rewards, ganador, COIN_REWARD_OPTIONS_BATALLA)
            self.log.append(
                f"¡{ganador.name} ganó la batalla! Gana {self.points_gained} puntos "
                f"y {self.coins_gained} monedas."
            )
        return True

    def state_for(self, viewer_id: str) -> dict:
        cur = self.current_player() if self.players and self.status == "playing" else None
        rival = next((p for p in self.players if p.id != viewer_id), None)

        # Mientras se está eligiendo piedra/papel/tijera no se revela qué
        # eligió cada uno (solo si YA eligió, para que "esperando a tu
        # rival" tenga sentido) — recién se revela el valor real en
        # rps_resultado, una vez que los dos ya tiraron.
        rps = None
        if self.status == "rps":
            rps = {
                "ya_elegiste": viewer_id in self.rps_choices,
                "rival_eligio": bool(rival and rival.id in self.rps_choices),
            }
        rps_resultado = None
        if self.rps_resultado:
            choices = self.rps_resultado["choices"]
            rps_resultado = {
                "empate": self.rps_resultado["empate"],
                "tu_eleccion": choices.get(viewer_id),
                "rival_eleccion": next((v for pid, v in choices.items() if pid != viewer_id), None),
                "ganaste": (not self.rps_resultado["empate"]) and self.rps_resultado.get("ganador") == viewer_id,
            }
        rematch = None
        if self.status == "finished":
            rematch = {
                "vos": self.rematch_requested.get(viewer_id, False),
                "rival": bool(rival and self.rematch_requested.get(rival.id, False)),
            }

        return {
            "type": "state",
            "game": "batalla",
            "status": self.status,
            "room": self.code,
            "mode": self.mode,
            "current_turn": cur.id if cur else None,
            "current_turn_name": cur.name if cur else None,
            "players": [p.to_dict(viewer_id) for p in self.players],
            "cartas_info": BATALLA_CARTAS,
            "log": self.log[-16:],
            "winner": self.winner,
            "points_gained": self.points_gained,
            "coins_gained": self.coins_gained,
            "deck_remaining": len(self.mazo),
            "chat": self.chat_messages[-50:],
            "your_id": viewer_id,
            "min_players": 2,
            "max_players": 2,
            "can_start": False,
            "is_spectator": False,
            "spectator_count": len(self.spectators),
            "matchmaking_deadline_ms": int(self.matchmaking_deadline * 1000) if self.matchmaking_deadline else None,
            "rps": rps,
            "rps_resultado": rps_resultado,
            "rematch": rematch,
            "last_action": self.last_action,
        }

    def spectator_state_for(self) -> dict:
        base = self.state_for("__espectador__")
        base["your_id"] = None
        base["is_spectator"] = True
        return base


def _capas_activas(p: dict) -> int:
    return int(p["campo_dron"]) + int(p["dron"]) + int(p["campo_fuerza"]) + int(p["escudo"])


def _capas_terrestres(p: dict) -> int:
    return int(p["campo_fuerza"]) + int(p["escudo"])


def _mejores(indices: List[int], personajes: List[dict], clave) -> List[int]:
    """De una lista de índices, devuelve los que empatan en el mínimo valor
    de `clave` (para elegir entre varios objetivos igual de buenos y recién
    ahí sortear al azar entre ellos)."""
    if not indices:
        return []
    mejor = min(clave(personajes[i]) for i in indices)
    return [i for i in indices if clave(personajes[i]) == mejor]


def decidir_jugada_bot(room: BatallaRoom, bot: BatallaPlayer):
    """Heurística fija (sin búsqueda en profundidad, pero sin tirar cartas
    al azar): primero busca rematar a alguien de un solo golpe, después
    desgasta las capas de defensa del rival (con Misil priorizando a quien
    solo le queden capas aéreas, que la Bomba no puede tocar), después se
    cura, después se protege a sí mismo (Escudo → Campo de Fuerza → Dron →
    Campo del Dron), y solo bombardea si el intercambio le conviene.
    Devuelve (carta, objetivo) o None si conviene descartar toda la mano y
    robar de nuevo."""
    rival = room._rival(bot)
    mano = bot.mano
    if rival is None:
        return None

    # 1) Rematar: un solo golpe que deje a alguien Fuera de Combate.
    if "bomba" in mano:
        rematables = [i for i, p in enumerate(rival.personajes)
                      if p["estado"] == "pie" and _capas_terrestres(p) == 0]
        if rematables:
            return "bomba", {"slot": random.choice(rematables)}
    if "misil" in mano:
        rematables = [i for i, p in enumerate(rival.personajes)
                      if p["estado"] == "pie" and _capas_activas(p) == 0]
        if rematables:
            return "misil", {"slot": random.choice(rematables)}

    # 2) Curarse si tengo caídos.
    if "hada" in mano:
        caidos = [i for i, p in enumerate(bot.personajes) if p["estado"] == "caido"]
        if caidos:
            return "hada", {"slot": random.choice(caidos)}

    # 3) Desgastar con Bomba al que le queden menos capas terrestres.
    if "bomba" in mano:
        objetivos = [i for i, p in enumerate(rival.personajes) if p["estado"] == "pie"]
        mejores = _mejores(objetivos, rival.personajes, _capas_terrestres)
        if mejores:
            return "bomba", {"slot": random.choice(mejores)}

    # 4) Desgastar con Misil, priorizando a quien solo lo protejan capas
    #    aéreas (Dron/Campo del Dron), ya que la Bomba no las puede tocar.
    if "misil" in mano:
        solo_aereo = [i for i, p in enumerate(rival.personajes)
                      if p["estado"] == "pie" and _capas_terrestres(p) == 0 and _capas_activas(p) > 0]
        if solo_aereo:
            return "misil", {"slot": random.choice(solo_aereo)}
        objetivos = [i for i, p in enumerate(rival.personajes) if p["estado"] == "pie"]
        mejores = _mejores(objetivos, rival.personajes, _capas_activas)
        if mejores:
            return "misil", {"slot": random.choice(mejores)}

    # 5) Protegerse a mí mismo: Escudo -> Campo de Fuerza -> Dron -> Campo del Dron.
    if "escudo" in mano:
        desprotegidos = [i for i, p in enumerate(bot.personajes) if not p["escudo"]]
        if desprotegidos:
            en_pie = [i for i in desprotegidos if bot.personajes[i]["estado"] == "pie"]
            return "escudo", {"slot": random.choice(en_pie or desprotegidos)}

    if "campo_fuerza" in mano:
        candidatos = [i for i, p in enumerate(bot.personajes) if p["escudo"] and not p["campo_fuerza"]]
        if candidatos:
            return "campo_fuerza", {"slot": random.choice(candidatos)}

    if "dron" in mano:
        desprotegidos = [i for i, p in enumerate(bot.personajes) if not p["dron"]]
        if desprotegidos:
            en_pie = [i for i in desprotegidos if bot.personajes[i]["estado"] == "pie"]
            return "dron", {"slot": random.choice(en_pie or desprotegidos)}

    if "campo_dron" in mano:
        candidatos = [i for i, p in enumerate(bot.personajes) if p["dron"] and not p["campo_dron"]]
        if candidatos:
            return "campo_dron", {"slot": random.choice(candidatos)}

    # 6) Bombardeo solo si me deja Fuera de Combate a más rivales que propios.
    if "bombardeo" in mano:
        rival_en_riesgo = sum(1 for p in rival.personajes if p["estado"] == "pie" and _capas_activas(p) == 0)
        propios_en_riesgo = sum(1 for p in bot.personajes if p["estado"] == "pie" and _capas_activas(p) == 0)
        if rival_en_riesgo > propios_en_riesgo:
            return "bombardeo", {}
        # El Bombardeo es la única carta que siempre es legal jugar (no
        # necesita objetivo). Si llegamos hasta acá con Bombardeo en mano,
        # es que ninguna otra carta tenía objetivo válido — y como la sala
        # ya no permite descartar teniendo una jugada legal disponible, hay
        # que jugarlo igual aunque el intercambio no sea el ideal, para no
        # trabar el turno del bot.
        return "bombardeo", {}

    return None


def _lanzar_bot_rps_si_hace_falta(room: BatallaRoom) -> None:
    """Si la sala está en piedra/papel/tijera y hay un bot que todavía no
    tiró, le programa la tirada (con una demora corta para que no se
    sienta instantáneo)."""
    if room.status != "rps":
        return
    bot = next((p for p in room.players if p.is_bot), None)
    if bot is not None and bot.id not in room.rps_choices:
        asyncio.create_task(_bot_elegir_rps(room.code))


async def _bot_elegir_rps(room_code: str):
    await asyncio.sleep(0.7 + random.random() * 0.9)
    room = rooms.get(room_code)
    if not isinstance(room, BatallaRoom) or room.status != "rps":
        return
    bot = next((p for p in room.players if p.is_bot), None)
    if bot is None or bot.id in room.rps_choices:
        return
    ok, _ = room.elegir_rps(bot.id, random.choice(["piedra", "papel", "tijera"]))
    if not ok:
        return
    try:
        await broadcast(room)
    except Exception:
        pass
    if room.rps_resultado and not room.rps_resultado.get("empate"):
        asyncio.create_task(_watch_rps_resolucion(room.code))
    else:
        _lanzar_bot_rps_si_hace_falta(room)


async def _watch_rps_resolucion(room_code: str):
    """Tras un piedra/papel/tijera resuelto (no empate), deja un momento
    para que los dos vean la revelación de qué tiró cada uno antes de que
    arranque la partida de verdad."""
    await asyncio.sleep(2.2)
    room = rooms.get(room_code)
    if not isinstance(room, BatallaRoom) or room.status != "rps" or not room.rps_resultado:
        return
    if room.rps_resultado.get("empate"):
        return
    ganador_id = room.rps_resultado.get("ganador")
    room.start(primero=ganador_id)
    try:
        await broadcast(room)
    except Exception:
        pass
    if room.status == "playing" and room.current_player().is_bot:
        asyncio.create_task(_bot_jugar_batalla(room.code))


async def _bot_jugar_batalla(room_code: str):
    """Hace jugar al bot su turno, con una pequeña demora para que no se
    sienta instantáneo/robótico (parte de que el rival no note que es un
    bot)."""
    await asyncio.sleep(1.3 + random.random() * 1.2)
    room = rooms.get(room_code)
    if not isinstance(room, BatallaRoom) or room.status != "playing":
        return
    cur = room.current_player()
    if not cur.is_bot:
        return
    jugada = decidir_jugada_bot(room, cur)
    ok = False
    if jugada is None:
        ok, _ = room.descartar_y_robar(cur.id, list(cur.mano))
    else:
        carta, objetivo = jugada
        ok, _ = room.jugar_carta(cur.id, carta, objetivo)
        if not ok:
            # Salvaguarda: si por algún motivo la heurística sugirió una
            # jugada inválida, no se traba el turno — descarta y sigue.
            ok, _ = room.descartar_y_robar(cur.id, list(cur.mano))
    if not ok:
        # Última red de seguridad: ni jugar ni descartar funcionaron (no
        # debería pasar nunca, pero nunca hay que dejar una partida trabada
        # en el turno del bot). Se fuerza a pasar el turno igual.
        room.turn_index = 1 - room.turn_index
    try:
        await broadcast(room)
    except Exception:
        pass
    if room.status == "playing" and room.current_player().is_bot:
        asyncio.create_task(_bot_jugar_batalla(room.code))


async def _watch_batalla_matchmake(room_code: str):
    """Cuenta regresiva de 10s de Partida Rápida: si nadie más entró a la
    sala, se completa con un bot y arranca la partida sola."""
    await asyncio.sleep(BATALLA_MATCHMAKE_TIMEOUT)
    room = rooms.get(room_code)
    if not isinstance(room, BatallaRoom):
        return
    if room.status != "waiting" or len(room.players) != 1:
        return
    room.agregar_bot()
    room.iniciar_rps()
    try:
        await broadcast(room)
    except Exception:
        pass
    _lanzar_bot_rps_si_hace_falta(room)


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
async def matchmake(game: str = "codigos"):
    """game="codigos" (Estratega, por defecto) o "batalla" (Batalla de
    Avatares) — cada uno busca/crea salas MM- de SU propio juego nada más,
    nunca mezcla jugadores de un juego con el otro."""
    max_jugadores = 2 if game == "batalla" else MAX_PLAYERS

    # 1. Buscar una sala existente del mismo juego que esté "waiting" y
    # tenga espacio.
    for room_id, room in rooms.items():
        if room_id.startswith(MATCHMAKE_PREFIX) and getattr(room, "game", "codigos") == game:
            if room.status == "waiting" and len(room.players) < max_jugadores:
                return {"room": room_id, "game": game}

    # 2. Si no hay, crear nueva con un código aleatorio.
    # Antes era un contador secuencial ("001", "002"...): cualquiera podía
    # adivinar el código de una sala ajena con solo probar números seguidos.
    new_room_id = MATCHMAKE_PREFIX + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    while new_room_id in rooms:
        new_room_id = MATCHMAKE_PREFIX + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

    rooms[new_room_id] = BatallaRoom(new_room_id) if game == "batalla" else Room(new_room_id)
    return {"room": new_room_id, "game": game}


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
            "game": getattr(room, "game", "codigos"),
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
async def ws_endpoint(websocket: WebSocket, room_code: str, player_name: str, token: Optional[str] = None):
    await websocket.accept()
    clean_name = player_name.strip()

    if supabase is not None:
        # El navegador no puede mandar headers personalizados al abrir un
        # WebSocket, así que el token viaja como query param (?token=...).
        # Sin esta verificación, cualquiera podía conectarse con el nombre
        # de otra persona (visible en el ranking/chat) y jugar partidas "a
        # nombre de" ella, robándole o regalándole puntos y monedas reales.
        verified_username = verify_supabase_token(token)
        if verified_username is None or verified_username.strip().lower() != clean_name.lower():
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Sesión inválida o el nombre no coincide con tu cuenta: vuelve a iniciar sesión.",
            }))
            await websocket.close()
            return

    # Las salas son un espacio de códigos único entre los dos juegos: si
    # este código ya existe pero es de Batalla de Avatares, no se juega acá
    # -se manda a reconectar al endpoint correcto, sin importar qué juego
    # tenía elegido esta persona en el menú.
    existente = rooms.get(room_code)
    if isinstance(existente, BatallaRoom):
        await websocket.send_text(json.dumps({"type": "redirect_game", "game": "batalla", "room": room_code}))
        await websocket.close()
        return

    room_mode = "solo" if room_code.upper().startswith("SOLO-") else "multi"
    room = rooms.setdefault(room_code, Room(room_code, mode=room_mode))

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
        avatar, rango, _tablero = obtener_avatar_y_rango(clean_name)
        player = Player(pid, clean_name, websocket, avatar=avatar, rango=rango)
        room.players.append(player)
        room.log.append(f"{clean_name} se unió a la sala.")
        await broadcast(room)

    _add_online(clean_name)
    await _broadcast_lobby()

    async def _cleanup_desconexion():
        """Limpieza compartida al perder la conexión, sea por un cierre
        normal (WebSocketDisconnect) o por cualquier error inesperado. Antes
        solo se ejecutaba para WebSocketDisconnect: un mensaje malformado
        (JSON inválido, campos con el tipo equivocado, etc.) tiraba una
        excepción distinta que no se capturaba, dejando al jugador con
        "connected: True" para siempre — un jugador fantasma que ocupa su
        cupo en la sala sin que nadie pueda reemplazarlo ni que la sala se
        libere sola."""
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

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                mtype = msg.get("type")
            except Exception as e:
                print(f"⚠️  Mensaje ilegible de {player.name} en sala {room.code}: {e}")
                continue

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
                    if room.reveal_tile() is not None:
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
        await _cleanup_desconexion()
    except Exception as e:
        # Cualquier otro error (payload con forma inesperada, KeyError,
        # TypeError, etc.) ya no debe dejar al jugador "conectado" para
        # siempre: se limpia igual que un WebSocketDisconnect normal.
        print(f"❌ Error inesperado en la conexión de {player.name} en sala {room.code}: {e}")
        try:
            await _cleanup_desconexion()
        except Exception:
            pass


@app.websocket("/ws/batalla/{room_code}/{player_name}")
async def ws_batalla_endpoint(websocket: WebSocket, room_code: str, player_name: str, token: Optional[str] = None):
    """Jugador de Batalla de Avatares. Mismo patrón de verificación de
    token y de reconexión que ws_endpoint (Estratega de Códigos), pero con
    su propia gramática de mensajes (jugar_carta / descartar_robar) — ver
    BatallaRoom más arriba."""
    await websocket.accept()
    clean_name = player_name.strip()

    if supabase is not None:
        verified_username = verify_supabase_token(token)
        if verified_username is None or verified_username.strip().lower() != clean_name.lower():
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Sesión inválida o el nombre no coincide con tu cuenta: vuelve a iniciar sesión.",
            }))
            await websocket.close()
            return

    existente = rooms.get(room_code)
    if existente is not None and not isinstance(existente, BatallaRoom):
        await websocket.send_text(json.dumps({"type": "redirect_game", "game": "codigos", "room": room_code}))
        await websocket.close()
        return

    room: BatallaRoom = rooms.setdefault(room_code, BatallaRoom(room_code))

    player = next((p for p in room.players if p.name.strip().lower() == clean_name.lower()), None)

    if player:
        player.ws = websocket
        player.connected = True
        room.log.append(f"{player.name} se reconectó a la partida.")
        await broadcast(room)
    else:
        if room.status == "playing":
            await websocket.send_text(json.dumps(
                {"type": "redirect_spectator", "room": room_code,
                 "message": "La partida ya comenzó: entrarás como espectador."}
            ))
            await websocket.close()
            return
        if room.status != "waiting" or len(room.players) >= 2:
            await websocket.send_text(json.dumps(
                {"type": "error", "message": "La sala ya está llena."}
            ))
            await websocket.close()
            return

        pid = f"{clean_name}-{random.randint(1000, 9999)}"
        avatar, rango, tablero = obtener_avatar_y_rango(clean_name, "puntos_batalla", "victorias_batalla")
        player = BatallaPlayer(pid, clean_name, websocket, avatar=avatar, rango=rango, tablero=tablero)
        room.players.append(player)
        room.log.append(f"{clean_name} se unió a la sala.")

        # Solo las salas de Partida Rápida (MM-) arrancan la cuenta
        # regresiva de emparejamiento con bot; una sala privada espera a
        # un humano indefinidamente, igual que en Estratega de Códigos.
        if room_code.upper().startswith(MATCHMAKE_PREFIX) and len(room.players) == 1:
            room.matchmaking_deadline = time.time() + BATALLA_MATCHMAKE_TIMEOUT
            asyncio.create_task(_watch_batalla_matchmake(room_code))
        elif len(room.players) == 2:
            room.iniciar_rps()

        await broadcast(room)

    _add_online(clean_name)
    await _broadcast_lobby()

    async def _cleanup_batalla():
        player.connected = False
        try:
            await broadcast(room)
        except Exception:
            pass
        _remove_online(player.name)
        try:
            await _broadcast_lobby()
        except Exception:
            pass
        if not any(p.connected for p in room.players if not p.is_bot):
            rooms.pop(room.code, None)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                mtype = msg.get("type")
            except Exception as e:
                print(f"⚠️  Mensaje ilegible de {player.name} en sala batalla {room.code}: {e}")
                continue

            if mtype == "ping":
                continue

            elif mtype == "elegir_rps" and room.status == "rps":
                ok, motivo = room.elegir_rps(player.id, msg.get("opcion"))
                if ok:
                    await broadcast(room)
                    if room.rps_resultado and not room.rps_resultado.get("empate"):
                        asyncio.create_task(_watch_rps_resolucion(room.code))
                    else:
                        _lanzar_bot_rps_si_hace_falta(room)
                else:
                    await websocket.send_text(json.dumps({"type": "error", "message": motivo}))

            elif mtype == "revancha" and room.status == "finished":
                ok, motivo = room.pedir_revancha(player.id)
                if ok:
                    await broadcast(room)
                    _lanzar_bot_rps_si_hace_falta(room)
                else:
                    await websocket.send_text(json.dumps({"type": "error", "message": motivo}))

            elif mtype == "jugar_carta" and room.status == "playing":
                if room.current_player().id == player.id:
                    ok, motivo = room.jugar_carta(player.id, msg.get("carta"), msg.get("objetivo") or {})
                    if ok:
                        await broadcast(room)
                        if room.status == "playing" and room.current_player().is_bot:
                            asyncio.create_task(_bot_jugar_batalla(room.code))
                    else:
                        await websocket.send_text(json.dumps({"type": "error", "message": motivo}))

            elif mtype == "descartar_robar" and room.status == "playing":
                if room.current_player().id == player.id:
                    ok, motivo = room.descartar_y_robar(player.id, msg.get("cartas") or [])
                    if ok:
                        await broadcast(room)
                        if room.status == "playing" and room.current_player().is_bot:
                            asyncio.create_task(_bot_jugar_batalla(room.code))
                    else:
                        await websocket.send_text(json.dumps({"type": "error", "message": motivo}))

            elif mtype == "chat":
                text = str(msg.get("text", ""))
                if text.strip():
                    room.add_chat_message(player, text)
                    await broadcast(room)

            elif mtype == "leave":
                room.leave_game(player)
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
                if not any(p.connected for p in room.players if not p.is_bot):
                    rooms.pop(room.code, None)
                try:
                    await websocket.close()
                except Exception:
                    pass
                return

    except WebSocketDisconnect:
        await _cleanup_batalla()
    except Exception as e:
        print(f"❌ Error inesperado en la conexión de {player.name} en sala batalla {room.code}: {e}")
        try:
            await _cleanup_batalla()
        except Exception:
            pass


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
