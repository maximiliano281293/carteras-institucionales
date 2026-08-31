# carteras-mx

Dataset longitudinal de composición de carteras de inversionistas institucionales
de México y Estados Unidos, y la plataforma que se construye sobre él.

Ahora mismo cubre **CONSAR / Siefores Generacionales**. CNBV R7 y SEC 13F entran
después, sobre el mismo esquema.

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
python src/cosechar_consar.py     # -> crudos/consar_AAAAMMDD.json   (~15 min)
python src/cargar.py              # -> datos/carteras.duckdb + allocations.parquet + pagina.json
python src/construir_pagina.py    # -> docs/index.html
```

`cosechar_consar.py` guarda avance parcial en cada consulta: si se cae la red,
la siguiente corrida retoma donde iba.

## Estructura

```
src/esquema.sql           esquema DuckDB: append-only, snapshots versionados,
                          dimensiones, ingest_log y log de calidad
src/cosechar_consar.py    baja las series de SISET por el endpoint directo
src/cargar.py             parsea, carga, valida y exporta
src/construir_pagina.py   inyecta los datos en la plantilla
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

**"XXI Banorte" y "XXI-Banorte" son la misma afore.** Sin normalizar darían once
afores donde hay diez.

**Ruptura de taxonomía en abril de 2025.** Se desagregaron BONDES G y FONADIN, se
consolidaron los BPAS y apareció "Otros Gubernamental". Afecta el desglose por
instrumento; a nivel de clase de activo, que es lo que se grafica, no.

**Vigencias reales.** La Siefore Básica 95-99 empieza a operar el 26 de agosto de
2024 y la 55-59 deja de operar ese mismo día.

CONSAR marca sus cifras como preliminares.

## Pendiente de verificar

- **Si los runners de GitHub alcanzan `consar.gob.mx`.** Si no, la cosecha corre
  en local y el workflow solo carga y publica.
- **Si CNBV tiene un endpoint equivalente.** El `.xlsm` de R7 llama al stored
  procedure `dbo.sp_052_1G_R7_`, o sea que hay un backend detrás. El mismo truco
  del IQY podría destaparlo, y entonces R7 también se automatiza en vez de pasar
  por Excel a mano.
