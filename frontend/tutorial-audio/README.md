# Narración del tutorial guiado

Esta carpeta se sirve tal cual en `/tutorial-audio/<archivo>` (ver
`main.py`, que monta toda la carpeta `frontend/` como estático). Alcanza
con subir acá cada `.mp3` con el nombre exacto que ya está enganchado en
`frontend/index.html` (`TUTORIAL_ROUNDS[].audioReveal/audioClue/audioAfter`,
`TUTORIAL_AUDIO_SOLVE`, `TUTORIAL_AUDIO_DONE`) — no hace falta tocar nada
más de código, el tutorial ya intenta reproducir cada uno apenas se
muestra su texto correspondiente. Si un archivo todavía no existe, el
tutorial sigue funcionando en silencio para ese paso (falla en silencio,
igual que los efectos de sonido).

Cada paso tiene 3 momentos (excepto el cierre, que tiene 1):
1. **revelar**: el texto que aparece antes de tocar el color e revelar la ficha.
2. **pista**: el texto que aparece después de revelar, invitando a pedir la pista.
3. **explicacion**: el texto que aparece después de pedir la pista (la explicación + qué tachar).

Ver el mensaje del chat / la conversación donde se cerró el guion para el
texto exacto de cada archivo — acá solo la lista de nombres:

- paso1-revelar.mp3 / paso1-pista.mp3 / paso1-explicacion.mp3
- paso2-revelar.mp3 / paso2-pista.mp3 / paso2-explicacion.mp3
- paso3-revelar.mp3 / paso3-pista.mp3 / paso3-explicacion.mp3
- paso4-revelar.mp3 / paso4-pista.mp3 / paso4-explicacion.mp3
- paso5-revelar.mp3 / paso5-pista.mp3 / paso5-explicacion.mp3
- paso6-revelar.mp3 / paso6-pista.mp3 / paso6-explicacion.mp3
- paso7-revelar.mp3 / paso7-pista.mp3 / paso7-explicacion.mp3
- paso8-revelar.mp3 / paso8-pista.mp3 / paso8-explicacion.mp3
- paso9-resolver.mp3 (el resumen antes de DESACTIVAR)
- paso10-completado.mp3 (la felicitación final)
