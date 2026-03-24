# SVG -> PDF para colorear por numeros (A4)

Este proyecto genera PDF(s) A4 listos para imprimir a partir de un SVG vectorial puro o de una carpeta con multiples SVG.

Salida del PDF:

1. Dibujo principal en fondo blanco con contornos tenues.
2. Referencia interna por zonas (cada zona muestra un simbolo de un caracter para su color original).
3. Leyenda inferior con todos los colores detectados, incluyendo negro puro, mostrando simbolo + cuadrado de color.

## Requisitos

- Python 3.10+
- Dependencias en `requirements.txt`

## Estructura recomendada

- `svg_to_paint_by_numbers_pdf.py`: script principal.
- `fonts/Montserrat-Regular.ttf`: fuente usada para numeracion.
- `inputs/`: carpeta recomendada para lotes de SVG.
- `patterns/`: patrones SVG para modo mystery/ofuscado.
- `output/`: PDFs generados.

## Instalacion

```bash
python -m pip install -r requirements.txt
```

La fuente usada para los numeros es `Montserrat` y se carga desde:

- `fonts/Montserrat-Regular.ttf`

## Uso

```bash
python svg_to_paint_by_numbers_pdf.py <archivo.svg>
python svg_to_paint_by_numbers_pdf.py <archivo.svg> -o <salida.pdf>
python svg_to_paint_by_numbers_pdf.py <archivo.svg> --mystery-pattern patterns/pattern.svg
python svg_to_paint_by_numbers_pdf.py <carpeta_con_svgs>
```

Ejemplo con este repositorio:

```bash
python svg_to_paint_by_numbers_pdf.py numbers.svg
python svg_to_paint_by_numbers_pdf.py numbers.svg --mystery-pattern patterns/pattern.svg
python svg_to_paint_by_numbers_pdf.py inputs
```

Si no indicas `-o`, el PDF se genera en `output/<nombre>_paint_by_numbers.pdf`.

En modo carpeta, el script crea automaticamente una salida con timestamp dentro del input:

- `pdf-output-YYYYMMDD-HHMMSS/`

Cada SVG produce un PDF con el mismo nombre base dentro de esa carpeta.

Durante la ejecucion, el CLI muestra un checklist de etapas con `[x]`, `[>]` y `[ ]`, junto con tiempo transcurrido y ETA aproximado.

En `render-labels`, ademas muestra progreso en vivo por zonas y un contador aproximado de `checks` geometricos para que se vea cuanto falta cuando el trabajo interno es grande.

En modo carpeta, tambien muestra progreso batch por archivo (`completados/total`), archivo actual y ETA global restante del lote.

## Opciones CLI principales

- `--include-strokes`: incluye trazos sin relleno como zonas numerables (usa buffer geometrico por `stroke-width`).
- `--show-hex`: muestra tambien el HEX en la leyenda.
- `--font-path`: ruta al TTF de Montserrat (por defecto `fonts/Montserrat-Regular.ttf`).
- `-o/--output`: salida PDF explicita (solo modo archivo individual).
- `--representation-grey OUTLINE NUMBER`: override de grises para contorno y numeros (0..1). Ejemplo: `--representation-grey 0.68 0.72`.
- `--mystery-pattern`: aplica un SVG patron para fragmentar geometricamente todas las zonas del dibujo.
- `--mystery-fit`: ajusta el patron al `viewBox` del dibujo (`contain`, `cover`, `stretch`).
- `--mystery-min-fragment-area`: area minima para conservar un fragmento generado por el patron.
- `--mystery-min-fragment-ratio`: proporcion minima respecto al area original para conservar un fragmento.
- `--mystery-max-fragments-per-zone`: limite de fragmentos por zona; si se supera, esa zona no se divide.
- `--mystery-boundary-grey`: tono gris de las divisiones internas del patron.
- `--mystery-boundary-width`: grosor de las divisiones internas del patron.
- `--min-font-size`: tamano minimo de numero (pt). El minimo efectivo siempre es `2`.
- `--max-font-size`: tamano maximo de numero (pt). El maximo efectivo siempre es `6`.
- `--line-width`: grosor de linea del dibujo principal (pt).
- `--max-segment-step`: paso de muestreo para curvas/arcos en geometria interna.
- `--min-area`: area minima de zona para etiquetado (default `0`, incluye todas).

## Como funciona

### 1) Extraccion de colores

- Recorre elementos vectoriales (`path`, `rect`, `circle`, `ellipse`, `polygon`, `polyline`, `line`).
- Resuelve estilos (`fill`, `stroke`, opacidades, estilos inline).
- Normaliza color exacto a formato `#RRGGBB`.
- Normaliza a `#000000` el color mas cercano al negro para asegurar una referencia negra pura.
- `#000000` siempre aparece en la leyenda.

### 2) Ordenacion cromatica

- Convierte cada color a HSV con `colorsys`.
- Ordena por `(hue, saturation, value)` para que la leyenda quede cromatica.
- Colores sin tono (grises) se ordenan al final de forma consistente.
- Asigna referencias de un solo caracter por color: `1..9`, luego `A..Z`.

### 3) Dibujo principal en blanco y negro

- No reutiliza rellenos de color originales en el arte final.
- Traza toda la geometria con un gris tenue (default contorno `0.68`) sobre fondo blanco.
- En modo mystery, puede fragmentar todas las zonas del dibujo usando un patron SVG superpuesto geometricamente.
- Dibuja tambien las divisiones internas del patron con un gris configurable para ofuscar la lectura de la silueta.
- Mantiene posiciones/proporciones del SVG dentro de una pagina A4.

### 4) Colocacion geometrica de numeros

- Convierte zonas rellenables a geometria poligonal (`shapely`).
- Busca punto interior robusto con `polylabel`.
- Valida geometricamente que la caja del texto quede contenida dentro de la zona.
- Ajusta tipografia dentro del rango `2-6 pt`.
- Dibuja numeracion en `Montserrat` con tono tenue (default numero `0.72`).
- Evita superposiciones entre numeros en la colocacion normal con verificacion global de colision.
- Si una posicion colisiona, prueba alternativas en bandas (dos lineas internas) y puntos de respaldo.
- Si no se puede contener dentro de la zona, aplica fallback obligatorio: coloca centrado a `2 pt`, aunque salga del area.
- Si una zona negra pura cae en fallback, no se dibuja su simbolo dentro del dibujo, pero su referencia sigue apareciendo en la leyenda.

### 5) Leyenda inferior

- Reserva una franja inferior en A4.
- Usa cuadrados de referencia mas grandes y con mayor separacion.
- Cada cuadrado contiene dentro su referencia (digito/letra) centrada.
- El texto dentro del cuadrado se pinta automaticamente en blanco o negro segun contraste.
- Mantiene opcion de HEX al lado cuando se usa `--show-hex`.

### 6) Exportacion PDF A4

- Genera el documento final con `reportlab`.
- Pagina unica A4 con margenes razonables, dibujo principal y leyenda inferior.

### 7) Modo mystery / ofuscado

- Carga un `pattern.svg` independiente y lo ajusta al `viewBox` del dibujo.
- Usa las fronteras del patron para dividir geometricamente las zonas coloreables.
- Cada fragmento conserva el color original de su zona, por lo que la leyenda no cambia de concepto.
- Si un fragmento resultante es demasiado pequeno o excesivo en cantidad, el sistema conserva la zona original.

## Manejo basico de errores

- Ruta SVG inexistente o extension incorrecta.
- Carpeta de entrada sin archivos `.svg`.
- Fuente Montserrat no encontrada o no registrable.
- SVG invalido o sin elementos vectoriales compatibles.
- SVG sin zonas numerables.
- Fallos inesperados durante parseo o exportacion.

## Limitaciones conocidas

- La logica esta orientada a SVG vectoriales puros ya preprocesados.
- No interpreta filtros, mascaras, composiciones complejas o raster embebido.
- Soporte de transformaciones muy avanzadas no esta contemplado en esta version.
- En zonas extremadamente pequenas, por diseno el numero puede quedar fuera del area para no perder etiquetado.
