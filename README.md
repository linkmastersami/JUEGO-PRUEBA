# Estratega de Códigos — juego web 2-4 jugadores

Juego de deducción tipo "desactivar la bomba": cada jugador tiene un código
secreto de 5 números (ocultos incluso para sí mismo) y va revelando fichas
del mazo para conseguir pistas sobre su propio código, hasta animarse a
gritar "¡Desactivar!" y adivinarlo antes que se acabe el mazo.

## Qué incluye
- `main.py`: servidor (Python + FastAPI + WebSockets) con toda la lógica del
  juego, autenticación (verificación de sesión de Supabase), tienda de
  avatares, ranking y chat.
- `frontend/index.html`: interfaz web de una sola página, pensada para
  celular (login, lobby, partida, tienda, ranking, modo espectador).
- `requirements.txt`: dependencias de Python.

## Modos de juego
- **Multijugador (2-4)**: por turnos, cada jugador revela una ficha del mazo
  y la usa como pista sobre su propio código (categorizarla entre sus
  fichas, o comparar sus puntitos con una de sus posiciones). El primero en
  acertar su código gana puntos según cuánto quede del mazo; si falla,
  queda eliminado.
- **Solitario**: una sola persona contra el mazo, mismas reglas, puntaje a
  la mitad.
- **Tutorial guiado**: un guion fijo paso a paso, sin puntos ni monedas, para
  aprender la mecánica.
- **Espectador**: cualquiera puede entrar de solo lectura a ver una partida
  en curso (no ve el código de nadie, solo color/puntitos como cualquier
  rival) y usar el chat de la sala.
- **Monster Crazy**: minijuego aparte (`frontend/monster-crazy.html`, corre
  dentro de un `<iframe>`), sin salas ni rivales — deslizar el dedo rápido
  sobre los puntos débiles del monstruo antes de que se acabe el tiempo, en
  fácil (cualquier orden) o difícil (solo en orden numérico). No suma a un
  puntaje acumulado: cada dificultad tiene su propio récord personal, y las
  monedas se pagan según la puntuación de esa partida (1 moneda cada 2000
  puntos en fácil, cada 1000 en difícil).

También hay chat de sala, chat de lobby global, contador de "en línea",
reconexión automática con reintentos si se corta la conexión, y una cuenta
regresiva de gracia (20s) si a alguien le toca jugar y está desconectado.

## Cuentas, tienda y ranking (requiere Supabase)
El login es usuario+contraseña vía **Supabase Auth** (el frontend fabrica un
email interno tipo `usuario@gotfive.local` para no pedir email real). El
backend usa la **service role key** de Supabase solo para leer/escribir
puntos, monedas y avatares en la tabla `profiles`, y para **verificar la
sesión** de quien llama a los endpoints sensibles (comprar/cambiar avatar,
pedir avatar nuevo, y al conectarse al WebSocket de una partida): el
`username` nunca se toma de lo que manda el cliente, sale del token de
Supabase ya verificado.

Ganar una partida da puntos (rango) y una recompensa fija de monedas
(caja fuerte). Las monedas se gastan en la tienda de avatares — el catálogo
sale solo de los archivos en `frontend/gif/`, donde los primeros 3 dígitos
del nombre son el precio (`15001.gif` → 150 monedas). El primer avatar de
150 es gratis. Hay un ranking global por puntos.

Si `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` no están configuradas, el
servidor sigue funcionando para jugar partidas (sin verificar identidad),
pero no guarda puntos/monedas/avatares ni sirve el ranking.

## Cómo probarlo en tu computadora
```bash
pip install -r requirements.txt
set SUPABASE_URL=https://tu-proyecto.supabase.co
set SUPABASE_SERVICE_KEY=tu-service-role-key
uvicorn main:app --reload
```
(en PowerShell usa `$env:SUPABASE_URL = "..."` en vez de `set`)

Abre `http://localhost:8000` en dos pestañas/dispositivos, regístrate (o
inicia sesión) con un usuario distinto en cada una, y desde el lobby elige
"Partida rápida" (matchmaking automático) o escribe el mismo código de sala
privada en ambas para jugar juntos.

> Nota: el frontend trae su propia `SUPABASE_URL`/`SUPABASE_ANON_KEY`
> (públicas, es normal exponerlas) apuntando a un proyecto de Supabase real
> — para usar tu propio proyecto, reemplázalas en `frontend/index.html` y
> asegúrate de tener una tabla `profiles` (columnas: `username`, `puntos`,
> `victorias`, `monedas`, `avatar_actual`, `avatares_comprados`,
> `puntos_batalla`, `victorias_batalla`, `monster_record_facil`,
> `monster_record_dificil`) y `solicitudes` (columnas: `username`,
> `categoria` — `"avatar"`/`"tablero"`/`"cancion"` —, `fecha`, `texto`).

## Para desplegarlo de verdad (que funcione entre celulares en internet)
Backends con soporte de WebSockets y plan gratuito: **Render**, **Railway** o
**Fly.io**. Subes esta misma carpeta, defines `uvicorn main:app --host
0.0.0.0 --port $PORT` como comando de arranque, configuras las variables de
entorno `SUPABASE_URL` y `SUPABASE_SERVICE_KEY`, y compartes el link.

## Qué simplifica el prototipo (y se puede ajustar)
- **Sin salas persistentes**: las partidas viven en memoria del servidor
  mientras corre; al reiniciarlo se pierden (los puntos/monedas ya guardados
  en Supabase no se pierden, solo la partida en curso). Para producción real
  a mayor escala conviene mover el estado de salas a Redis.
- **Verificación de token por request**: cada acción sensible llama a la API
  de Supabase Auth para validar el token (no hay caché de sesión en el
  servidor), lo cual es correcto pero agrega una llamada de red por acción.

## Siguientes pasos sugeridos
- Recuperación de contraseña (hoy, si alguien la olvida, no hay forma de
  recuperar la cuenta porque el email es inventado).
- Animaciones adicionales al colocar fichas.
- Modo "revancha" al terminar la partida.
