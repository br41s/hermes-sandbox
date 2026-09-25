# Flujo técnico BigLobster Shorts — checklist v2

Revisión del checklist v1 (el del grokbot) punto por punto. Todo sigue siendo
**gratis**. Donde algo cambia, se marca **🔧 CAMBIADO** o **✅ NUEVO** y se dice por
qué. Hermes ejecuta este mismo flujo automáticamente (`shorts/STUDIO.md`); este
documento sirve también para quien lo haga a mano mientras tanto.

Floor de calidad = Claude v5. Google Flow (Martín/Lucía) sigue siendo opcional.

---

## 0. Inputs y reglas duras

- Fuente: 🔧 **los feeds RSS**, no el HTML del índice:
  `https://biglobster.top/feed.xml` (EN) y `https://biglobster.top/es/feed.xml` (ES).
  Vienen ordenados por fecha, con URL y título, y no cambian cuando se rediseña el
  blog. El gemelo EN/ES comparte el mismo slug.
- Ledger: 🔧 **JSON gestionado por la herramienta**, no un `LEDGER.md` editado a mano.
  Un agente que edita su propio estado en prosa es cómo un short se publica dos veces.
- Targets: FB Page · IG Reel + Story · YouTube Shorts · X (a mano, ver §10).
- Formato: 1080×1920, 30 fps, 55–65 s; Story ≤59,9 s.
- VO: EN `en-US-ChristopherNeural` · ES `es-ES-AlvaroNeural` · rate +0 %.
- Reglas duras (igual): no inventar claims · cover titulado obligatorio · B-roll no
  repetido · no freeze antes de corte.
- ✅ NUEVO: **cada cifra del short tiene que aparecer en el artículo** — se comprueba
  leyendo el artículo, no fiándose del guion.

## 1. Selección del post

1. Leer el feed del idioma.
2. Saltar los que ya tienen short (ledger).
3. Coger el más reciente sin short.
4. 🔧 La paleta y el motivo no pueden repetir los de los **dos últimos shorts** (incluye
   el gemelo del otro idioma). Antes era una intención; ahora se valida.

## 2. Ingesta del artículo

- Fetch del post y extracto del **cuerpo** (`<article class="article-body">`), sin
  menú ni footer: un "999 %" del footer no puede contar como fuente.
- ✅ NUEVO: lista de cifras del artículo → son las únicas utilizables.

## 3. Paquete creativo (antes `PACKAGE.md`)

🔧 **Un JSON con esquema**, no un Markdown libre. El mismo documento que valida el
guion es el que consume el render, así no hay copia a mano entre pasos.

- Hook ≤3 s, sin saludo · 4–6 beats · CTA → URL del post.
- Tipos de beat: `hook` · `point` · `stat` (cifra que cuenta hacia arriba) · `list` ·
  `quote` · `avatar` · `cta`.
- On-screen ≤8 palabras; `*palabra*` la resalta en color de acento.
- B-roll por beat: 1–4 palabras **en inglés** y filmables.
- ✅ NUEVO: se rechaza automáticamente: <70 o >200 palabras, on-screen largo, CTA a otro
  dominio, X >280, título YouTube >100, paleta repetida, cifra sin fuente.

## 4. Voz (edge-tts)

| v1 | v2 | Por qué |
|---|---|---|
| `--write-subtitles` .vtt por cue | 🔧 eventos `WordBoundary` de la API de Python | edge-tts 7.x escribe **frases** en el .vtt por defecto: el karaoke caía a un resaltado por frase sin avisar |
| `ffmpeg -f concat … -c copy vo.wav` desde MP3 | 🔧 decodificar cada cue a WAV 48 kHz y concatenar **re-codificando** | `-c copy` de MP3 a un contenedor WAV no es fiable, y la lista de concat **no insertaba** los silencios de 200–400 ms que decía el paso |
| silencios "~200–400 ms" | 🔧 cada beat se rellena (`apad`) hasta un número **entero de frames** | audio y vídeo quedan alineados por construcción; sin deriva acumulada |

Pulido de VO: igual (`highpass 90 · acompressor · treble`).

## 5. B-roll + música

- 🔧 **Pexels primero** (API gratuita, clips **verticales** nativos) y Mixkit de
  respaldo. Mixkit es casi todo horizontal: un 720p recortado a 9:16 son 405 px de
  ancho estirados a 1080.
- 🔧 Cada clip se corta **a la duración exacta de su beat** (con bucle solo si es más
  corto), no a un bucle fijo de 15 s: el bucle fijo se veía reiniciar dentro de la escena.
- Clips que comparten búsqueda siguen donde lo dejó el anterior, no repiten imagen.
- Música: Mixkit por *mood*, evitando las últimas 12 usadas.
- ATTRIBUTION: queda en `manifest.json` (ID, fuente, URL) automáticamente.

## 6. Remotion

- 🔧 **Una sola plantilla con props**, no un proyecto nuevo + `npm i` por short. Menos
  tiempo, menos tokens y calidad constante. `shorts/studio/remotion/`.
- Capas: fondo degradado + motivo animado + B-roll (opacidad 0,42, zoom lento,
  fundido) + escena + barra de progreso por beat + karaoke.
- Márgenes seguros: arriba 230 px, abajo 290 px; karaoke por encima del texto de IG.
- Render **muted** con `--color-space=bt709`. ✅ NUEVO: sin ese flag Remotion
  entrega `yuvj420p` (rango completo), que algunas plataformas muestran lavado.
- 🔧 Se renderiza en **GitHub Actions** (gratis), no en la máquina del agente.

## 7. Mezcla + exports

- Ducking igual (`sidechaincompress`), cama a 0,35.
- 🔧 **loudnorm en dos pasadas** (medir → aplicar lineal). La pasada única trabaja en
  modo dinámico y "bombea" sobre la voz.
- 🔧 **Story**: `-t 58.9 -c copy` corta en el keyframe más cercano y puede acabar a
  mitad de palabra. Ahora: si el vídeo cabe entero, entero; si no, se corta en el
  **último cambio de beat** antes de 59,5 s, con fundido de 0,5 s, re-codificando.
- ✅ NUEVO: `captions.srt` para YouTube (accesibilidad + SEO), con la puntuación del guion.

## 8. Covers

- Portada IG y miniatura YouTube como **composiciones propias** de Remotion (no un
  frame capturado), con títulos distintos: portada = promesa del hook, miniatura =
  pregunta/claim. ≤7 palabras.
- ✅ NUEVO: se comprueba 1080×1920 y ≤2 MB (límite de miniaturas de YouTube).

## 9. QA — ✅ NUEVO (antes "a ojo")

Sobre el fichero final, no sobre los inputs. Un fallo **bloquea** la publicación:

| Comprobación | Umbral |
|---|---|
| Resolución / fps / códec | 1080×1920 · 30 · h264 yuv420p · AAC |
| Duración | 20–90 s (aviso fuera de 45–70) · coincide con la línea de tiempo ±0,3 s |
| Loudness | −14 LUFS ±1,5 (aviso si true peak > −0,5 dBFS) |
| Freeze (`freezedetect`) | nada congelado >2 s — la regla "no freeze antes de corte", medida |
| Negro (`blackdetect`) | nada >0,4 s |
| Silencio (`silencedetect`) | ningún hueco >1,6 s salvo la cola del CTA |
| Story | ≤59,9 s |
| Covers | existen, 1080×1920, ≤2 MB |

## 10. Publicación

| v1 | v2 | Coste |
|---|---|---|
| FB + IG vía Zernio MCP | 🔧 **Graph API de Meta directa** (subida resumable desde el fichero) | gratis (Zernio solo es gratis para 2 cuentas) |
| IG cover = cover titulado | igual: el cover se aloja como foto no publicada de la Page para obtener URL pública | gratis |
| YouTube por navegador (Studio) | 🔧 **YouTube Data API v3** + miniatura + SRT | gratis (≈4.100 de 10.000 unidades/día por 2 shorts) |
| X por navegador (computerUse) | 🔧 **a mano**, con el vídeo y el texto listos en Telegram | X ya no tiene API gratuita (pago por uso desde feb. 2026) |

- Se acaba "Verifica que eres tú": la API no pasa por ese control del navegador.
- ⚠️ YouTube: hasta superar la auditoría gratuita de Google, **toda subida por API
  queda privada**. La herramienta avisa con el estado real que aplicó YouTube.
- Ritmo humano: ya no aplica (no hay navegador); cada destino es una llamada.
- ✅ NUEVO: **modo sombra** (`SHORTS_PUBLISH_MODE` distinto de `live`): renderiza y te
  lo manda todo por Telegram para revisar; no publica nada.

## 11. Ledger + aviso

- El ledger guarda estado, IDs/URLs por red, footage y música usados, QA.
- Aviso en español por Telegram: enlaces publicados + el post de X con el vídeo adjunto.

## 12. Híbrido Google Flow (Martín / Lucía)

Flow **no tiene API** (el acceso programático a Veo es de pago), así que el clic en
Flow sigue siendo humano. Lo que cambia es todo lo de alrededor:

- ✅ NUEVO: **librería de avatares**. Cada clip se guarda con la frase exacta que dice
  (así los subtítulos coinciden con la boca). Se envía a Hermes por Telegram una vez;
  el Producer puede usar como mucho un beat de avatar por short, solo con clips de
  la librería.
- Patrón recomendado de clips reutilizables por idioma: 1 apertura ("Soy Martín, de
  BigLobster…"), 1 cierre ("Tienes la guía completa en el enlace"), 2–3 reacciones
  cortas. Así un lote de Flow sirve para semanas.
- Igual que antes: uno por short, `md5` único por clip (ahora sha256 en el nombre del
  asset), sin créditos → no se compra nada.
- ✅ NUEVO: YouTube recibe `containsSyntheticMedia: true` cuando hay avatar.

## Mapa de herramientas

| Pieza | v1 | v2 |
|---|---|---|
| Orquestación | grokbot (desktop) | Hermes cron: Producer + Publisher |
| Composición | Remotion por short | Remotion, plantilla única, en GitHub Actions |
| VO + timings | edge-tts CLI + .vtt | edge-tts API + WordBoundary |
| B-roll / música | Mixkit | Pexels + Mixkit / Mixkit |
| Mezcla / QA | ffmpeg / a ojo | ffmpeg / QA automático |
| FB + IG | Zernio MCP | Graph API |
| YouTube | navegador | Data API v3 |
| X | navegador | Telegram → a mano |
| Estado | LEDGER.md | ledger JSON de la herramienta |

## Orden mental (una línea)

Feed → artículo + cifras → paquete JSON (validado) → Actions: edge-tts WordBoundary →
Pexels/Mixkit → Remotion muted → duck + loudnorm 2 pasadas → Story en beat → covers →
QA → Hermes: YouTube API · Graph API FB/IG/Story → Telegram (X + resumen) → ledger
