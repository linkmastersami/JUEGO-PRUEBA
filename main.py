"""
Got Five! - Prototipo web para 2 jugadores en línea.

Cómo correrlo:
    pip install -r requirements.txt
    uvicorn main:app --reload
Luego abre http://localhost:8000 en dos pestañas/dispositivos distintos,
usa el mismo código de sala y nombres diferentes.
"""

import json
import random
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

app = FastAPI()

rooms: Dict[str, "Room"] = {}


class Entry:
    """Una casilla del atril: puede ser un número secreto (oculto para su
    dueño) o una ficha pista (visible para todos) intercalada entre ellos."""

    def __init__(self, kind: str, value: int, secret_index: Optional[int] = None):
        self.kind = kind  # 'secret' | 'clue'
        self.value = value
        self.secret_index = secret_index

    def to_dict(self, viewer_id: str, board_owner_id: str):
        if self.kind == "clue":
            return {"kind": "clue", "value": self.value}
        # Es un número secreto: el dueño no lo ve, los demás sí.
        if viewer_id == board_owner_id:
            return {"kind": "secret", "value": None, "index": self.secret_index}
        return {"kind": "secret", "value": self.value, "index": self.secret_index}


class Player:
    def __init__(self, pid: str, name: str, ws: WebSocket):
        self.id = pid
        self.name = name
        self.ws = ws
        self.secret: List[int] = []
        self.board: List[Entry] = []
        self.connected = True

    def rebuild_board_secrets(self):
        self.board = [
            Entry("secret", v, secret_index=i) for i, v in enumerate(self.secret)
        ]

    def add_clue(self, value: int):
        self.board.append(Entry("clue", value))
        self.board.sort(key=lambda e: e.value)


class Room:
    def __init__(self, code: str):
        self.code = code
        self.players: List[Player] = []
        self.draw_pile: List[int] = []
        self.turn_index = 0
        self.status = "waiting"  # waiting | playing | finished
        self.winner: Optional[str] = None
        self.log: List[str] = []
        self.eliminated: set = set()

    def get_player(self, pid: str) -> Optional[Player]:
        return next((p for p in self.players if p.id == pid), None)

    def start(self):
        pool = random.sample(range(1, 100), 10 + 40)
        secrets_pool, self.draw_pile = pool[:10], pool[10:]
        for i, p in enumerate(self.players):
            p.secret = sorted(secrets_pool[i * 5:(i + 1) * 5])
            p.rebuild_board_secrets()
        self.status = "playing"
        self.turn_index = 0
        self.log.append("La partida ha comenzado. ¡Suerte!")

    def current_player(self) -> Player:
        return self.players[self.turn_index]

    def draw_for_current(self):
        if not self.draw_pile:
            return
        tile = self.draw_pile.pop(0)
        cur = self.current_player()
        cur.add_clue(tile)
        self.log.append(f"{cur.name} robó el {tile} y lo colocaron en su atril.")

    def advance_turn(self):
        n = len(self.players)
        for _ in range(n):
            self.turn_index = (self.turn_index + 1) % n
            if self.players[self.turn_index].id not in self.eliminated:
                break

    def state_for(self, viewer_id: str) -> dict:
        players_view = []
        for p in self.players:
            board = [e.to_dict(viewer_id, p.id) for e in p.board]
            players_view.append({
                "id": p.id,
                "name": p.name,
                "is_you": p.id == viewer_id,
                "board": board,
                "eliminated": p.id in self.eliminated,
                "connected": p.connected,
            })
        cur = self.current_player() if self.players and self.status == "playing" else None
        return {
            "type": "state",
            "status": self.status,
            "room": self.code,
            "draw_pile_count": len(self.draw_pile),
            "current_turn": cur.id if cur else None,
            "current_turn_name": cur.name if cur else None,
            "players": players_view,
            "log": self.log[-14:],
            "winner": self.winner,
            "your_id": viewer_id,
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

    if room.status != "waiting" or len(room.players) >= 2:
        if not (room.status == "waiting" and len(room.players) < 2):
            await websocket.send_text(json.dumps(
                {"type": "error", "message": "La sala ya está llena o la partida ya empezó."}
            ))
            await websocket.close()
            return

    pid = f"{player_name}-{random.randint(1000, 9999)}"
    player = Player(pid, player_name, websocket)
    room.players.append(player)
    room.log.append(f"{player_name} se unió a la sala.")

    if len(room.players) == 2 and room.status == "waiting":
        room.start()

    await broadcast(room)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "draw" and room.status == "playing":
                if room.current_player().id == pid:
                    room.draw_for_current()
                    room.advance_turn()
                    await broadcast(room)

            elif mtype == "guess" and room.status == "playing":
                guess = msg.get("numbers", [])
                cur = room.get_player(pid)
                if guess == cur.secret:
                    room.status = "finished"
                    room.winner = pid
                    room.log.append(f"¡{cur.name} gritó GOT FIVE! y acertó! ¡Gana la partida!")
                else:
                    room.eliminated.add(pid)
                    room.log.append(f"{cur.name} gritó GOT FIVE! pero falló y queda eliminado.")
                    active = [p for p in room.players if p.id not in room.eliminated]
                    if len(active) == 1:
                        room.status = "finished"
                        room.winner = active[0].id
                        room.log.append(f"{active[0].name} gana la partida.")
                    elif room.current_player().id == pid:
                        room.advance_turn()
                await broadcast(room)

    except WebSocketDisconnect:
        player.connected = False
        room.log.append(f"{player.name} se desconectó.")
        try:
            await broadcast(room)
        except Exception:
            pass


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
