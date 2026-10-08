#!/usr/bin/env python3
"""
Junta los snapshots crudos de tasas (crudos/tasas_*.parquet), deja la versión
más reciente de cada dato y exporta:

    datos/carteras.duckdb   tabla `tasas` (la misma base de las Afores y los fondos)
    datos/tasas.parquet     tabla larga: fecha, curva, nodo, plazo_dias, tasa, fuente, serie
    datos/pagina_fi.json    lo que consume la sección "Tasas · FI"

Reglas
------
- Crudo primero: todo sale de crudos/; este paso es reproducible.
- Si una fuente revisa una cifra, gana el snapshot más reciente (el archivo con
  fecha mayor). El historial de git guarda la versión anterior.
- Nunca imputar: los huecos se quedan como null. Las subastas de Cetes y Bonos M
  son semanales o menos frecuentes; la página usa "la última subasta vigente" y
  dice de qué fecha es, pero aquí no se rellena nada.
"""
from __future__ import annotations

import datetime as dt, json, sys
from pathlib import Path

import duckdb

RAIZ = Path(__file__).resolve().parents[1]
CRUDOS, DATOS = RAIZ / "crudos", RAIZ / "datos"

CURVAS = {
    "MX_GUB": {"nombre": "Gubernamental MX", "detalle": "Cetes y Bonos M · tasa de subasta primaria",
               "base": 28, "frecuencia": "subasta", "vigencia_dias": 50},
    "TIIE":   {"nombre": "TIIE", "detalle": "TIIE de Fondeo (1D) y TIIE 28 · 91 · 182",
               "base": 28, "frecuencia": "diaria", "vigencia_dias": 7},
    "UST":    {"nombre": "Treasuries", "detalle": "U.S. Treasury · curva par diaria",
               "base": 365, "frecuencia": "diaria", "vigencia_dias": 7},
    "SOFR":   {"nombre": "SOFR", "detalle": "Secured Overnight Financing Rate · NY Fed",
               "base": 365, "frecuencia": "diaria", "vigencia_dias": 7},
}
DESDE_PAGINA = dt.date(2006, 1, 1)


def main():
    archivos = sorted(CRUDOS.glob("tasas_*.parquet"))
    if not archivos:
        sys.exit("No hay crudos/tasas_*.parquet. Corre primero src/cosechar_tasas.py")
    DATOS.mkdir(exist_ok=True)
    con = duckdb.connect(str(DATOS / "carteras.duckdb"))
    con.execute("DROP TABLE IF EXISTS tasas")
    lista = ", ".join(f"'{p.as_posix()}'" for p in archivos)
    con.execute(f"""
        CREATE TABLE tasas AS
        SELECT fecha::DATE AS fecha, curva, nodo, plazo_dias::INT AS plazo_dias, tasa::DOUBLE AS tasa,
               fuente, serie
        FROM (
            SELECT *, row_number() OVER (PARTITION BY fecha, curva, nodo, fuente
                                         ORDER BY filename DESC) AS rn
            FROM read_parquet([{lista}], filename = true)
        ) WHERE rn = 1
        ORDER BY curva, plazo_dias, fecha
    """)
    con.execute(f"COPY tasas TO '{(DATOS / 'tasas.parquet').as_posix()}' (FORMAT parquet)")

    resumen = con.execute("""SELECT curva, count(*), min(fecha), max(fecha) FROM tasas GROUP BY 1 ORDER BY 1""").fetchall()
    for c, n, a, b in resumen:
        print(f"  {c:<7} {n:>8} filas  {a} → {b}")

    pagina = {"generado": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "curvas": {}}
    for curva, meta in CURVAS.items():
        nodos = con.execute("""SELECT nodo, plazo_dias, any_value(serie) FROM tasas WHERE curva = ?
                               GROUP BY 1, 2 ORDER BY 2""", [curva]).fetchall()
        if not nodos:
            continue
        piv = con.execute("""SELECT fecha, nodo, tasa FROM tasas WHERE curva = ? AND fecha >= ?
                             ORDER BY fecha""", [curva, DESDE_PAGINA]).fetchall()
        fechas = sorted({f for f, _, _ in piv})
        idx = {f: i for i, f in enumerate(fechas)}
        cols = {n: [None] * len(fechas) for n, _, _ in nodos}
        for f, n, t in piv:
            cols[n][idx[f]] = round(t, 4)
        pagina["curvas"][curva] = {
            **meta,
            "nodos": [{"et": n, "dias": d, "serie": s} for n, d, s in nodos],
            "fechas": [f.isoformat() for f in fechas],
            "v": [cols[n] for n, _, _ in nodos],
        }
    (DATOS / "pagina_fi.json").write_text(json.dumps(pagina, separators=(",", ":"), ensure_ascii=False), "utf-8")
    kb = (DATOS / "pagina_fi.json").stat().st_size / 1024
    con.close()
    print(f"datos/carteras.duckdb (tabla tasas) · datos/tasas.parquet · datos/pagina_fi.json ({kb:.0f} KB)")


if __name__ == "__main__":
    main()
