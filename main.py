"""
Got Five! - Prototipo web fiel al reglamento oficial, para 2-4 jugadores en línea.

Cómo correrlo:
    pip install -r requirements.txt
    uvicorn main:app --reload
Luego abre http://localhost:8000 en varias pestañas/dispositivos,
usa el mismo código de sala y nombres diferentes.
"""

import json
import random
import sqlite3
import hashlib
from pydantic import BaseModel
from fastapi import HTTPException
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope


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
# Base de datos y Autenticación
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect('got_five.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            puntos INTEGER DEFAULT 0,
            victorias INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db() 

def hash_password(password: str):
    return hashlib.sha256(password.encode()).hexdigest()

class UsuarioAuth(BaseModel):
    username: str
    password: str    

RANGOS = [
    "Adivino de Feria", "Curioso Empedernido", "Observador Casual", 
    "Estudiante de Probabilidades", "Detective Aficionado", "Analista de Patrones", 
    "Perfilador de Códigos", "Calculador Frío", "Estratega Silencioso", 
    "Zorro Ártico", "Mente de Neón", "Maestro de la Navaja", 
    "Cerebro de Cristal", "Oráculo de Bolsillo", "El Predicador"
]

def obtener_rango(puntos: int, victorias: int):
    if victorias == 0:
        return "Novato en Desactivación"
    
    indice = (puntos - 1) // 60000 if puntos > 0 else 0
    indice = max(0, min(14, indice))
    return RANGOS[indice]

def update_player_score(username: str, points_change: int) -> int:
    conn = sqlite3.connect('got_five.db')
    cursor = conn.cursor()
    cursor.execute("SELECT puntos, victorias FROM usuarios WHERE username = ?", (username,))
    row = cursor.fetchone()
    
    if row:
        current_puntos, current_victorias = row
        new_puntos = max(0, current_puntos + points_change)
        new_victorias = current_victorias + (1 if points_change > 0 else 0)
        
        cursor.execute(
            "UPDATE usuarios SET puntos = ?, victorias = ? WHERE username = ?",
            (new_puntos, new_victorias, username)
        )
        conn.commit()
        conn.close()
        return new_puntos
    conn.close()
    return 0

@app.post("/registro")
async def registrar_usuario(user: UsuarioAuth):
    conn = sqlite3.connect('got_five.db')
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO usuarios (username, password_hash) VALUES (?, ?)",
            (user.username, hash_password(user.password))
        )
        conn.commit()
        return {"mensaje": "¡Registro exitoso! Prepárate para la explosión."}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Este nombre de usuario ya está en uso.")
    finally:
        conn.close()

@app.post("/login")
async def iniciar_sesion(user: UsuarioAuth):
    conn = sqlite3.connect('got_five.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT puntos, victorias FROM usuarios WHERE username = ? AND password_hash = ?",
        (user.username, hash_password(user.password))
    )
    usuario = cursor.fetchone()
    conn.close()

    if usuario:
        puntos, victorias = usuario
        rango = obtener_rango(puntos, victorias)
        return {
            "mensaje": "Login exitoso",
            "username": user.username,
            "puntos": puntos,
            "victorias": victorias,
            "rango": rango
        }
    else:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos. ¿Intentaste adivinar?")


# ---------------------------------------------------------------------------
# Configuración del Juego y Salas WebSocket
# ---------------------------------------------------------------------------
rooms: Dict[str, "Room"] = {}
room_counter = 1  # Contador global para emparejamiento automático

MIN_PLAYERS = 2
MAX_PLAYERS = 4

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
    def __init__(self, pid: str, name: str, ws: WebSocket):
        self.id = pid
        self.name = name
        self.ws = ws
        self.secret: List[dict] = []  
        self.clues: List[ClueTile] = []
        self.eliminated = False  
        self.resolved = False  
        self.connected = True

    def notch_for(self, number: int) -> int:
        return sum(1 for t in self.secret if t["number"] < number)

    def to_dict(self, viewer_id: str) -> dict:
        is_owner = viewer_id == self.id
        if is_owner:
            secret_view = [{"hidden": True, "color": t["color"]} for t in self.secret]
        else:
            secret_view = [{"hidden": False, **t} for t in self.secret]
        return {
            "id": self.id,
            "name": self.name,
            "is_you": is_owner,
            "secret": secret_view,
            "clues": [c.to_dict() for c in self.clues],
            "eliminated": self.eliminated,
            "resolved": self.resolved,
            "connected": self.connected,
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

    def active_players(self) -> List[Player]:
        return [p for p in self.players if not p.eliminated and not p.resolved]

    def deck_remaining(self) -> int:
        return sum(len(pile) for pile in self.color_piles.values())

    def _record_event(self, event_type: str, player: Player, points: int, multiplier: Optional[int] = None):
        self.event_seq += 1
        self.last_event = {
            "seq": self.event_seq,
            "type": event_type, 
            "player_id": player.id,
            "player_name": player.name,
            "points": points,
            "multiplier": multiplier,
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

    def _guess_solo(self, cur: "Player", numbers: List[int], correct: List[int]) -> None:
        """Modo solitario: 1 jugador contra el mazo. Gana la mitad de las
        fichas que quedan en el mazo (redondeado hacia abajo) al descifrar
        su propio código. Si falla, la partida termina sin puntos."""
        if numbers == correct:
            cur.resolved = True
            earned_points = (self.deck_remaining() // 2) * 1000
            update_player_score(cur.name, earned_points)
            self.status = "finished"
            self.winner = cur.id
            self.log.append(
                f"¡{cur.name} descifró su código en solitario! Gana {earned_points} puntos "
                f"(mitad del mazo restante, redondeado hacia abajo)."
            )
            self._record_event("correct", cur, earned_points, multiplier=1)
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
            self.log.append(
                f"¡{cur.name} gritó GOT FIVE! y acertó. Gana {earned_points} puntos (×{multiplier})."
            )
            self._record_event("correct", cur, earned_points, multiplier=multiplier)

            if not self._finish_if_one_active_remains("correct") and was_current:
                self.advance_turn()
                self.phase = "reveal"
            return

        cur.eliminated = True
        lost_points = self.deck_remaining() * 1000
        update_player_score(cur.name, -lost_points)
        self.log.append(
            f"{cur.name} gritó GOT FIVE! pero falló y pierde {lost_points} puntos. Queda eliminado."
        )
        self._record_event("exploded", cur, lost_points)

        if not self._finish_if_one_active_remains("exploded") and was_current:
            self.advance_turn()
            self.phase = "reveal"

    def state_for(self, viewer_id: str) -> dict:
        cur = self.current_player() if self.players and self.status == "playing" else None
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
            "your_id": viewer_id,
            "min_players": MIN_PLAYERS,
            "max_players": MAX_PLAYERS,
            "can_start": self.status == "waiting" and (
                self.mode == "solo" or len(self.players) >= MIN_PLAYERS
            ),
        }


async def broadcast(room: Room):
    for p in room.players:
        if not p.connected:
            continue
        try:
            await p.ws.send_text(json.dumps(room.state_for(p.id)))
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
        room.log.append(f"{player.name} se reconectó a la partida.")
        await broadcast(room)
    else:
        if room.status != "waiting" or len(room.players) >= MAX_PLAYERS:
            await websocket.send_text(json.dumps(
                {"type": "error", "message": "La sala ya está llena o la partida ya empezó."}
            ))
            await websocket.close()
            return

        pid = f"{clean_name}-{random.randint(1000, 9999)}"
        player = Player(pid, clean_name, websocket)
        room.players.append(player)
        room.log.append(f"{clean_name} se unió a la sala.")
        await broadcast(room)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "start" and room.status == "waiting":
                if room.mode == "solo" or len(room.players) >= MIN_PLAYERS:
                    room.start()
                    await broadcast(room)

            elif mtype == "reveal" and room.status == "playing" and room.phase == "reveal":
                if room.current_player().id == player.id:
                    color = msg.get("color")
                    if room.reveal_tile(color) is not None:
                        await broadcast(room)

            elif mtype == "clue" and room.status == "playing" and room.phase == "clue":
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
                room.guess(player.id, numbers)
                await broadcast(room)

    except WebSocketDisconnect:
        player.connected = False
        room.log.append(f"{player.name} se desconectó temporalmente.")
        try:
            await broadcast(room)
        except Exception:
            pass
        
        # Limpieza de memoria (cierre de sala) si todos se desconectaron de esta sala
        if not any(p.connected for p in room.players):
            if room.code in rooms:
                del rooms[room.code]


app.mount("/", UTF8StaticFiles(directory="frontend", html=True), name="frontend")
