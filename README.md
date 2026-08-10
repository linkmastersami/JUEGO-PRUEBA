# Got Five! — Prototipo web (2 jugadores)

## Qué incluye
- `main.py`: servidor (Python + FastAPI + WebSockets) con toda la lógica del juego.
- `frontend/index.html`: interfaz web de una sola página, pensada para celular.
- `requirements.txt`: dependencias.

## Cómo probarlo en tu computadora
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
Abre `http://localhost:8000` en **dos pestañas del navegador** (o desde tu celular usando la IP de tu compu en la misma red, ej: `http://192.168.1.5:8000`).

1. En ambas pestañas escribe el **mismo código de sala** (ej: `ABCD`) y un nombre distinto.
2. En cuanto el segundo jugador entra, la partida arranca sola: a cada jugador se le asignan 5 números secretos (1–99), ocultos para él mismo pero visibles para el otro.
3. En tu turno, presiona **"Robar ficha"**: el servidor saca una ficha del mazo y la coloca automáticamente en la posición correcta de tu propio atril (esto simula al oponente colocándola, ya que el servidor sabe tus números).
4. Cuando creas saber tus 5 números, llena el formulario **GOT FIVE!** de menor a mayor y confirma. Si aciertas, ganas al instante; si fallas, quedas eliminado de la ronda.

## Qué simplifiqué para el prototipo (y se puede ajustar)
- **Rango de números**: uso 1–99 en vez de fichas de colores físicas; fácil de cambiar.
- **"Dar la pista a un oponente"**: en 2 jugadores no hay elección posible (solo hay un oponente), así que el servidor coloca la ficha automáticamente en tu atril al robarla. Con 3–4 jugadores habría que agregar un paso para elegir a quién dársela.
- **Sin cuentas ni salas persistentes**: todo vive en memoria del servidor mientras corre; al reiniciarlo se pierden las partidas. Para producción real conviene una base de datos (Redis es buena opción para salas efímeras).
- **Reconexión**: si alguien cierra la pestaña, el otro se entera en el log, pero no hay reintento automático de reconexión todavía.

## Para desplegarlo de verdad (que funcione entre dos celulares en internet)
Backends con soporte de WebSockets y plan gratuito: **Render**, **Railway** o **Fly.io**. Subes esta misma carpeta, defines `uvicorn main:app --host 0.0.0.0 --port $PORT` como comando de arranque, y listo: compartes el link con quien quieras jugar.

## Siguientes pasos sugeridos
- Agregar soporte para 3–4 jugadores (elegir a quién le das la pista).
- Animaciones al colocar fichas.
- Persistencia de salas (Redis) para que sobrevivan reinicios del servidor.
- Modo "revancha" al terminar la partida.
