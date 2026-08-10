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
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

app = FastAPI()

rooms: Dict[str, "Room"] = {}

MIN_PLAYERS = 2
MAX_PLAYERS = 4

# ---------------------------------------------------------------------------
# Fichas: 60 en total, 5 colores x 12 columnas. El color depende del resto
# módulo 5 y los puntitos (1-3) se repiten en ciclos de 3 según la columna,
# igual que en el tablero físico real.
# ---------------------------------------------------------------------------
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
    """Ficha colocada como pista, visible para todos (incluido su dueño)."""

    def __init__(self, kind: str, tile: dict, notch: Optional[int] = None,
                 slot: Optional[int] = None, same: Optional[bool] = None):
        self.kind = kind  # 'categorize' | 'compare'
        self.tile = tile
        self.notch = notch  # 0..5, posición entre las 5 fichas secretas
        self.slot = slot  # 0..4, con qué ficha secreta se comparó
        self.same = same  # resultado sí/no de la comparación

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
        self.secret: List[dict] = []  # 5 fichas, ordenadas ascendente
        self.clues: List[ClueTile] = []
        self.eliminated = False
        self.connected = True

    def notch_for(self, number: int) -> int:
        """Cuántas de mis fichas secretas son menores que `number`."""
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
            "connected": self.connected,
        }


class Room:
    def __init__(self, code: str):
        self.code = code
        self.players: List[Player] = []
        self.status = "waiting"  # waiting | playing | finished
        self.phase = "reveal"  # 'reveal' | 'clue' (solo aplica en status=playing)
        self.color_piles: Dict[str, List[int]] = {c: [] for c in COLORS}
        self.faceup: List[dict] = []
        self.turn_index = 0
        self.winner: Optional[str] = None
        self.log: List[str] = []

    def get_player(self, pid: str) -> Optional[Player]:
        return next((p for p in self.players if p.id == pid), None)

    # -- Setup -------------------------------------------------------------
    def start(self):
        by_color: Dict[str, List[int]] = {c: [] for c in COLORS}
        for n in range(1, 61):
            by_color[tile_info(n)["color"]].append(n)
        for c in COLORS:
            random.shuffle(by_color[c])

        # Ignorar 5 fichas del mazo (quemar)
        for _ in range(5):
            for c in COLORS:
                if by_color[c]: by_color[c].pop()

        # A cada jugador: 1 ficha de cada color
        for p in self.players:
            secret = [tile_info(by_color[c].pop()) for c in COLORS]
            secret.sort(key=lambda t: t["number"])
            p.secret = secret
            p.clues = []
            p.eliminated = False

        # Ya NO se agregan fichas a self.faceup aquí
        self.faceup = [] 

        self.color_piles = by_color
        self.status = "playing"
        self.phase = "reveal"
        self.turn_index = 0
        self.winner = None
        self.log.append("La partida ha comenzado. ¡Suerte!")

    def current_player(self) -> Player:
        return self.players[self.turn_index]

    def colors_available(self) -> Dict[str, bool]:
        return {c: len(self.color_piles[c]) > 0 for c in COLORS}

    # -- Turno: 1) Revelar ficha --------------------------------------------
    def reveal_tile(self, color: str) -> Optional[dict]:
        pile = self.color_piles.get(color, [])
        if not pile:
            return None
        n = pile.pop()
        tile = tile_info(n)
        self.faceup.append(tile)
        cur = self.current_player()
        self.log.append(f"{cur.name} reveló el {n} y lo puso junto a la reserva.")
        if any(self.color_piles[c] for c in COLORS):
            self.phase = "clue"
        else:
            # Sin fichas para revelar en el futuro no cambia el flujo actual.
            self.phase = "clue"
        return tile

    # -- Turno: 2) Pedir una pista -------------------------------------------
    def apply_categorize(self, faceup_index: int) -> bool:
        if not (0 <= faceup_index < len(self.faceup)):
            return False
        cur = self.current_player()
        tile = self.faceup.pop(faceup_index)
        notch = cur.notch_for(tile["number"])
        cur.clues.append(ClueTile("categorize", tile, notch=notch))
        self.log.append(f"{cur.name} pidió CATEGORIZAR el {tile['number']} en su atril.")
        self._end_clue_step()
        return True

    def apply_compare(self, faceup_index: int, slot: int) -> bool:
        if not (0 <= faceup_index < len(self.faceup)):
            return False
        cur = self.current_player()
        if not (0 <= slot < len(cur.secret)):
            return False
        tile = self.faceup.pop(faceup_index)
        same = tile["dots"] == cur.secret[slot]["dots"]
        cur.clues.append(ClueTile("compare", tile, slot=slot, same=same))
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
            if not self.players[self.turn_index].eliminated:
                break

    # -- GOT FIVE! -----------------------------------------------------------
    def guess(self, pid: str, numbers: List[int]) -> None:
        cur = self.get_player(pid)
        if cur is None or cur.eliminated or self.status != "playing":
            return
        correct = [t["number"] for t in cur.secret]
        if numbers == correct:
            self.status = "finished"
            self.winner = pid
            self.log.append(f"¡{cur.name} gritó GOT FIVE! y acertó sus 5 números! ¡Gana la partida!")
            return

        cur.eliminated = True
        self.log.append(f"{cur.name} gritó GOT FIVE! pero falló y queda eliminado.")
        active = [p for p in self.players if not p.eliminated]
        if len(active) == 1:
            self.status = "finished"
            self.winner = active[0].id
            self.log.append(f"{active[0].name} es el último en pie y gana la partida.")
        elif self.current_player().id == pid:
            self.advance_turn()
            self.phase = "reveal"

    # -- Serialización ---------------------------------------------------------
    def state_for(self, viewer_id: str) -> dict:
        cur = self.current_player() if self.players and self.status == "playing" else None
        return {
            "type": "state",
            "status": self.status,
            "phase": self.phase,
            "room": self.code,
            "faceup": self.faceup,
            "colors_available": self.colors_available(),
            "color_hex": COLOR_HEX,
            "current_turn": cur.id if cur else None,
            "current_turn_name": cur.name if cur else None,
            "players": [p.to_dict(viewer_id) for p in self.players],
            "log": self.log[-16:],
            "winner": self.winner,
            "your_id": viewer_id,
            "min_players": MIN_PLAYERS,
            "max_players": MAX_PLAYERS,
            "can_start": self.status == "waiting" and len(self.players) >= MIN_PLAYERS,
        }


async def broadcast(room: Room):
    for p in room.players:
        if not p.connected:
            continue
        try:
            await p.ws.send_text(json.dumps(room.state_for(p.id)))
        except Exception:
            pass


@app.websocket("/ws/{room_code}/{player_name}")
async def ws_endpoint(websocket: WebSocket, room_code: str, player_name: str):
    await websocket.accept()
    room = rooms.setdefault(room_code, Room(room_code))

    if room.status != "waiting" or len(room.players) >= MAX_PLAYERS:
        await websocket.send_text(json.dumps(
            {"type": "error", "message": "La sala ya está llena o la partida ya empezó."}
        ))
        await websocket.close()
        return

    pid = f"{player_name}-{random.randint(1000, 9999)}"
    player = Player(pid, player_name, websocket)
    room.players.append(player)
    room.log.append(f"{player_name} se unió a la sala.")

    await broadcast(room)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "start" and room.status == "waiting":
                if len(room.players) >= MIN_PLAYERS:
                    room.start()
                    await broadcast(room)

            elif mtype == "reveal" and room.status == "playing" and room.phase == "reveal":
                if room.current_player().id == pid:
                    color = msg.get("color")
                    if room.reveal_tile(color) is not None:
                        await broadcast(room)

            elif mtype == "clue" and room.status == "playing" and room.phase == "clue":
                if room.current_player().id == pid:
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
                room.guess(pid, numbers)
                await broadcast(room)

    except WebSocketDisconnect:
        player.connected = False
        room.log.append(f"{player.name} se desconectó.")
        try:
            await broadcast(room)
        except Exception:
            pass


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")