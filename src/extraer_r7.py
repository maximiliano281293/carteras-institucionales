#!/usr/bin/env python3
"""
Extrae el universo COMPLETO del R7 de CNBV (R03 J-0311 Cartera de inversión,
fondos de inversion) directamente del archivo .xlsm, sin abrir Excel.

Hallazgo que hace esto posible (verificado 2026-09-08 sobre 052_1G_R7_2026.xlsm):
  - La hoja MINFO es una TABLA DINAMICA. Lo que se ve (una operadora, un periodo)
    es solo el filtro con que se guardo el archivo.
  - La cache de esa tabla dinamica (xl/pivotCache/pivotCacheRecords*.xml) trae
    TODOS los registros del año: 105,518 filas, 6 periodos, 29 operadoras,
    650 fondos en el archivo de 2026.
  - "Quitar filtros y refrescar" en Excel NO va a ningun servidor: el VBA lee la
    cadena de conexion de \\sector5\DGAIN\MINFO\dgaex.txt, una ruta interna de
    CNBV que desde fuera no existe. La cache ES el dato.

Uso:
    python src/extraer_r7.py crudos/052_1G_R7_2026.xlsm
    -> crudos/r7_2026_<sha12>.parquet  (crudo tal cual, una fila por registro)

No transforma nada: los valores salen como estan en la cache. La normalizacion
(tipos, denominador, clases de activo) es trabajo de cargar_r7.py.
"""
from __future__ import annotations
import hashlib, re, sys, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
M = "{%s}" % NS["m"]


def _valor(el):
    """Un item de la cache -> valor Python. <m/> es 'missing' (NULL)."""
    t = el.tag[len(M):]
    if t == "m":
        return None
    v = el.get("v")
    if t == "n":
        return float(v)
    if t == "b":
        return v in ("1", "true")
    return v  # s, d, e: texto tal cual


def cache_mas_grande(z: zipfile.ZipFile) -> tuple[str, str]:
    """Devuelve (definicion, registros) de la pivot cache con mas registros."""
    mejor, mejor_n = None, -1
    for nombre in z.namelist():
        m = re.fullmatch(r"xl/pivotCache/pivotCacheDefinition(\d+)\.xml", nombre)
        if not m:
            continue
        raiz = ET.fromstring(z.read(nombre))
        n = int(raiz.get("recordCount", "0"))
        if n > mejor_n:
            mejor, mejor_n = m.group(1), n
    if mejor is None:
        sys.exit("El archivo no tiene tablas dinamicas con cache.")
    return (f"xl/pivotCache/pivotCacheDefinition{mejor}.xml",
            f"xl/pivotCache/pivotCacheRecords{mejor}.xml")


def leer_pivot_cache(ruta: Path) -> pd.DataFrame:
    with zipfile.ZipFile(ruta) as z:
        p_def, p_rec = cache_mas_grande(z)
        raiz = ET.fromstring(z.read(p_def))
        campos, compartidos = [], []
        for cf in raiz.find("m:cacheFields", NS):
            campos.append(cf.get("name"))
            si = cf.find("m:sharedItems", NS)
            compartidos.append([_valor(e) for e in si] if si is not None else [])
        esperados = int(raiz.get("recordCount", "0"))

        filas = []
        # iterparse: el archivo de registros pesa ~25 MB por año; no cargarlo entero.
        with z.open(p_rec) as fh:
            for _, el in ET.iterparse(fh):
                if el.tag != M + "r":
                    continue
                fila = []
                for k, item in enumerate(el):
                    if item.tag == M + "x":            # indice a sharedItems
                        fila.append(compartidos[k][int(item.get("v"))])
                    else:                               # valor inline
                        fila.append(_valor(item))
                filas.append(fila)
                el.clear()
    df = pd.DataFrame(filas, columns=campos)
    if len(df) != esperados:
        print(f"AVISO: la definicion declara {esperados} registros y se leyeron {len(df)}",
              file=sys.stderr)
    return df


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    ruta = Path(argv[1])
    sha = hashlib.sha256(ruta.read_bytes()).hexdigest()
    df = leer_pivot_cache(ruta)

    # Excel guarda algunas claves con apostrofo inicial para forzar texto ('91, '25).
    # Se quita SOLO el apostrofo de Excel; el resto del valor queda intacto.
    for c in ("dat_serie", "cve_tipo_valor"):
        if c in df:
            df[c] = df[c].map(lambda v: v[1:] if isinstance(v, str) and v.startswith("'") else v)

    anio = re.search(r"R7_(\d{4})", ruta.name)
    etiqueta = anio.group(1) if anio else "sin_anio"
    salida = ruta.with_name(f"r7_{etiqueta}_{sha[:12]}.parquet")
    df.attrs = {"archivo_origen": ruta.name, "sha256_origen": sha}
    df.to_parquet(salida, index=False)

    print(f"archivo   : {ruta.name}  (sha256 {sha[:12]})")
    print(f"registros : {len(df):,}")
    print(f"periodos  : {sorted(df['cve_periodo'].unique())}")
    print(f"operadoras: {df['dl_administradora'].nunique()} · fondos: {df['cve_pizarra'].nunique()}")
    print(f"tipos de inversion:")
    for k, n in df["dl_tipo_inversion"].value_counts().items():
        print(f"    {n:>7,}  {k}")
    print(f"-> {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
