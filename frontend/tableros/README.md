# Tableros (fondos comprables para el tablero de batalla)

Esta carpeta va a guardar las imágenes de los diseños de tablero que se van
a poder comprar en la tienda (funcionalidad todavía no armada — por ahora
esto es solo el lugar donde van los archivos).

## Convención de nombres

Numeración de 3 dígitos, en formato PNG:

```
001.png
002.png
003.png
...
```

El número de archivo es el identificador del tablero (así como los avatares
usan su propio archivo en `frontend/gif/`).

## Cómo se va a usar (cuando esté conectado)

- Cada jugador va a poder tener **como mucho un tablero elegido** a la vez
  (igual que el avatar).
- La imagen se estira/recorta (`background-size: cover`) para adaptarse al
  área de juego de ese jugador — no importa la proporción exacta con la que
  la subas, el CSS ya la ajusta.
- Si un jugador **no tiene ningún tablero seleccionado**, el área de juego
  se queda con el fondo actual (el degradado oscuro de siempre) — nunca se
  rompe el diseño por falta de imagen.
- El mismo tablero también se va a ver como fondo de la ficha del jugador
  en los lugares donde esa ficha aparece (pantalla de cambio de avatar,
  etc.).
- En la tienda va a haber 2 pestañas: una con el carrusel de avatares (como
  ahora) y otra con el carrusel de tableros.

Subí las imágenes acá con esa numeración y avisame — la parte de compra/
selección en la tienda y Supabase la conectamos en un paso aparte.
