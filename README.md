# carteras-mx

Dataset longitudinal de composición de carteras de inversionistas institucionales
de México y Estados Unidos, y la plataforma que se construye sobre él.

Cubre **CONSAR / Siefores Generacionales** (afores) y **CNBV R03 J-0311 "R7"**
(fondos de inversión). SEC 13F entra después, sobre el mismo esquema.

## El botón

**Actions → "Actualizar datos de CONSAR" → Run workflow.**

Eso cosecha SISET, recarga DuckDB, corre las validaciones, reconstruye la página
y hace commit del resultado. También se dispara solo los días 15 y 22 de cada
mes, porque CONSAR publica a mitad de mes.

Cada corrida deja un commit. **El historial de git es el log de versiones**: nada
se sobrescribe, y se puede ver el diff exacto de qué cambió entre una descarga y
otra — que importa porque las fuentes revisan cifras hacia atrás.

## Correrlo a mano

```bash
pip install -r requirements.txt
# Afores (CONSAR)
python src/cosechar_consar.py     # -> crudos/consar_AAAAMMDD.json   (~15 min)
python src/cargar.py              # -> datos/carteras.duckdb + allocations.parquet + pagina.json
# Fondos de inversión (CNBV) — un .xlsm por año, bajado del portal a crudos/
python src/extraer_r7.py crudos/052_1G_R7_2026.xlsm   # -> crudos/r7_2026_<sha>.parquet (14 s)
python src/cargar_r7.py --publicado 2026-08-17        # -> holdings_cnbv.parquet + allocations_cnbv.parquet + pagina_fondos.json
# Página (las dos secciones)
python src/construir_pagina.py    # -> docs/index.html
#
```

`cosechar_consar.py` guarda avance parcial en cada consulta: si se cae la red,
la siguiente corrida retoma donde iba.

## Estructura

```
src/esquema.sql           esquema DuckDB: append-only, snapshots versionados,
                          dimensiones, ingest_log y log de calidad
src/cosechar_consar.py    baja las series de SISET por el endpoint directo
src/cargar.py             parsea, carga, valida y exporta (CONSAR)
src/extraer_r7.py         saca el universo completo del .xlsm de CNBV sin abrir Excel
src/cargar_r7.py          holdings -> allocations por clase, con el catálogo y el denominador
src/construir_pagina.py   inyecta los datos de ambas fuentes en la plantilla
catalogos/asset_class_map_cnbv.csv   tipo de valor (+ emisora para ETFs) -> clase de activo
catalogos/pendientes_etf_revision.csv ETFs que aún caen en la clase por default
web/plantilla.html        la página (HTML autocontenido, sin dependencias)
crudos/                   snapshots crudos con fecha. Nunca se sobrescriben.
datos/                    DuckDB, Parquet y el JSON de la página
docs/                     la página construida (sirve tal cual en GitHub Pages)
```

Para publicar: **Settings → Pages → Deploy from a branch → `main` / `/docs`**.

## Cómo se bajan los datos, y por qué así

El botón "Exportar" del portal de CONSAR **está roto del lado de ellos** para
Excel y CSV: su servidor intenta generar el archivo con Excel vía COM y falla con
`Retrieving the COM class factory for component with CLSID
{00024500-0000-0000-C000-000000000046} failed ... 80040154`.

El formato IQY sí funciona, y ese archivo destapa el endpoint que el portal usa
por debajo — sin postbacks, sin `__VIEWSTATE`, sin Excel:

```
POST /gobmx/aplicativo/siset/ExportaSeriesHistoricas.aspx?t=IQY_HTML
cd=<id>&nl=Detalle&monthIni=1&yearIni=2019&monthFin=7&yearFin=2026&seriesSeleccionadas=<id>|<id>|...
```

Estructura de SISET, ya mapeada:

```
cd base (clase de activo) -> ddl_pivote -> cd por siefore -> filas = afores
```

Cuadros base: `259` RV Nacional · `271` RV Internacional · `283` Deuda Privada
Nacional · `295` Mercancías · `307` Estructurados y FIBRAS · `319` Deuda
Internacional · `331` Deuda Gubernamental · `343` Otros Activos.

## Lo que hay que saber del dato antes de usarlo

**Las ocho clases no son una partición del activo neto.** CONSAR calcula cada una
como porcentaje a valor de mercado sobre los activos netos; sumadas dan una
mediana de ~97.3% y en 2020–2021 algunos meses pasan de 100% (máximo observado:
110.5%, Principal en marzo de 2021). Ni cierran ni son disjuntas. La página lo
muestra con la línea del 100% en vez de normalizar.

**El denominador de CONSAR no es el de CNBV R7.** CONSAR: activos netos. R7:
Directo + Reporto + Garantías sobre la cartera de inversión, porque R7 no trae
activo neto. Están declarados en la tabla `parametros` y no deben mezclarse en
una misma serie sin distinguirlos.

**La fila de total de cada cuadro se identifica por posición, no por nombre.** Su
etiqueta cambia entre cuadros ("Estructurados" vs "Estructurados y FIBRAS").

**"XXI Banorte" y "XXI-Banorte" son la misma afore, y "Citibanamex" y "Banamex"
también** (8 de los 88 cuadros conservan el nombre viejo; ninguno trae ambos a la
vez). Sin normalizar darían once afores donde hay diez.

**Ruptura de taxonomía en abril de 2025.** Se desagregaron BONDES G y FONADIN, se
consolidaron los BPAS y apareció "Otros Gubernamental". Afecta el desglose por
instrumento; a nivel de clase de activo, que es lo que se grafica, no.

**Vigencias reales.** La Siefore Básica 95-99 empieza a operar el 26 de agosto de
2024 y la 55-59 deja de operar ese mismo día.

CONSAR marca sus cifras como preliminares.

## Lo que hay que saber del R7 de CNBV

**El `.xlsm` ya trae todo el año; no hay que abrir Excel.** La hoja MINFO es una tabla
dinámica y lo que se ve es sólo el filtro guardado. La caché de esa tabla dinámica trae
los 105 mil registros del año (29 operadoras, 650 fondos). "Refrescar" leería una ruta
de red interna de CNBV (`\\sector5\...`): desde fuera nunca funcionó. `extraer_r7.py`
lee la caché directo del ZIP.

**Denominador:** Directo + Reporto + Garantías + Préstamo de valores; derivados fuera
(su valor es P&L firmado). Declarado en `parametros.cnbv.denominador`. **No comparable
con CONSAR**, que usa activos netos.

**Los ETFs del SIC (TV `1ISP`, `1I`) no se pueden clasificar por tipo de valor:** el
mismo TV mezcla S&P 500 con Treasuries a 0-3 meses. Se clasifican por emisora en el
catálogo; lo que no está en el catálogo cae provisionalmente en Renta Variable
Internacional y queda en `calidad_log` (`clase_por_default`). La lista para revisar
está en `catalogos/pendientes_etf_revision.csv`.

**Cinco tipos de inversión, no cuatro:** existe "Operación de préstamo de valores
actuando como prestamista" (0.03% del sistema). **Sí hay efectivo en pesos** (`CHM`).
**Fondos de fondos** (TV `51`, `52`) se muestran como clase propia, sin look-through.

## Pendiente de verificar

- **Si los runners de GitHub alcanzan `consar.gob.mx`.** Si no, la cosecha corre
  en local y el workflow solo carga y publica.
- **Si CNBV tiene un endpoint equivalente.** El `.xlsm` de R7 llama al stored
  procedure `dbo.sp_052_1G_R7_`, o sea que hay un backend detrás. El mismo truco
  del IQY podría destaparlo, y entonces R7 también se automatiza en vez de pasar
  por Excel a mano.
  **"Publicado en Cloudflare Workers."
