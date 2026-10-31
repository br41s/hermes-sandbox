---
aprobado: no            # el dueño lo cambia a "si" tras revisar este fichero entero
clientes-redactados: no # "si" solo cuando no queda ningún nombre ni dominio de cliente
---

<!--
Plantilla de un fichero de datos propios para el Content Gap Hunter de BigLobster.
Se copia a /opt/data/profiles/biglobster/gaphunter/first-party/<tema>.md y lo
rellena y aprueba el dueño. El Hunter nunca crea, edita ni aprueba estos ficheros
(ver "5. DATOS PROPIOS" en biglobster-gap-hunter.prompt). Este fichero de plantilla
está en el repo; los rellenos viven en el volumen, fuera de git.
-->

# <Hallazgo en una frase, con lo que cambió>

## Hechos

Una línea por hecho, con la cifra exacta y de dónde sale. Mínimo 6 aquí: el Hunter
necesita ≥12 en total con fuentes externas de contexto, y este fichero cuenta como una.

- <hecho con la cifra exacta> | <cómo se midió: Langfuse, libro de registro, CI, validador> | <periodo: fechas> | <n>
- <p. ej. "16 de 82 infografías publicadas tenían texto fuera de su caja"> | validador de infografías sobre el sitio publicado | <fecha> | n=82

## Qué hicimos distinto después

Lo que cambió en el proceso por este hallazgo, en tres frases como máximo. Es la
parte que el lector puede copiar.

## Límites

A quién NO aplica y qué no mide. Un hallazgo sin límites no es un hallazgo.

## No publicar

Todo lo que no debe salir aunque el Hunter lo vea: nombres de clientes, dominios,
claves, rutas internas, cifras de facturación.
