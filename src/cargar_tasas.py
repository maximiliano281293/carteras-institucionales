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
- Swaps de TIIE de Fondeo: boletín de MexDer (crudos/mexder_swaps_*.parquet) y,
  como historia inicial, crudos/historia_tiief_pg.parquet. Si una fecha tiene
  ambos, gana MexDer. Con la curva par completa (1F1..390F1, interpolando en
  plazo si sólo hay nodos) se hace bootstrapping a factores de descuento
  (cupones cada 28 días, act/360) y se publica par (TIIEF) y cupón cero (TIIEF_CERO).
- Nunca imputar: los huecos se quedan como null. Las subastas de Cetes y Bonos M
  son semanales o menos frecuentes; la página usa "la última subasta vigente" y
  dice de qué fecha es, pero aquí no se rellena nada.
"""
from __future__ import annotations

import datetime as dt, json, sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
CRUDOS, DATOS = RAIZ / "crudos", RAIZ / "datos"

CURVAS = {
    "TIIEF":  {"nombre": "Swaps TIIE de Fondeo", "detalle": "IRS TIIEF · tasa par · precio de liquidación MexDer",
               "base": 28, "frecuencia": "diaria", "vigencia_dias": 7},
    "TIIEF_CERO": {"nombre": "TIIE de Fondeo · cupón cero", "detalle": "Bootstrapping de la curva par · capitalizable 28 días",
               "base": 28, "frecuencia": "diaria", "vigencia_dias": 7},
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
# nodos que se publican de la curva swap (n periodos de 28 días)
NODOS_SWAP = {"1M": 1, "3M": 3, "6M": 6, "9M": 9, "1A": 13, "2A": 26, "3A": 39, "4A": 52, "5A": 65,
              "7A": 91, "10A": 130, "15A": 195, "20A": 260, "30A": 390}
TAU = 28 / 360


def bootstrap(par_pct: np.ndarray) -> np.ndarray:
    """Par (en %, n = 1..N periodos de 28 días) -> tasa cupón cero capitalizable cada 28 días (en %)."""
    s = par_pct / 100
    df, acum = np.empty(len(s)), 0.0
    for i, si in enumerate(s):
        df[i] = (1 - si * TAU * acum) / (1 + si * TAU)
        acum += df[i]
    n = np.arange(1, len(s) + 1)
    return (df ** (-1 / n) - 1) / TAU * 100


def filas_tiief() -> pd.DataFrame:
    """Curva swap TIIE de Fondeo por fecha: MexDer gana sobre la historia de Postgres."""
    partes = [pd.read_parquet(p).assign(fuente="mexder") for p in sorted(CRUDOS.glob("mexder_swaps_*.parquet"))]
    hist = CRUDOS / "historia_tiief_pg.parquet"
    if hist.exists():
        partes.insert(0, pd.read_parquet(hist).assign(fuente="postgres"))
    if not partes:
        return pd.DataFrame(columns=["fecha", "curva", "nodo", "plazo_dias", "tasa", "fuente", "serie"])
    raw = pd.concat(partes, ignore_index=True)
    raw["prio"] = (raw.fuente == "mexder").astype(int)
    filas = []
    for fecha, g in raw.groupby("fecha"):
        g = g[g.prio == g.prio.max()].sort_values("n")
        fuente = g.fuente.iloc[0]
        nmax = int(g.n.max())
        if nmax < 13:          # sin al menos 1 año no hay curva que publicar
            continue
        n = np.arange(1, nmax + 1)
        par = np.interp(n, g.n.values, g.tasa.values)   # interpola en plazo sólo si faltan contratos
        cero = bootstrap(par)
        observados = set(g.n.values)
        for et, k in NODOS_SWAP.items():
            if k > nmax:
                continue
            if k not in observados:   # sólo se publican plazos observados; la interpolación es para el bootstrapping
                continue
            filas.append((fecha, "TIIEF", et, k * 28, round(float(par[k - 1]), 4), fuente, f"{k}F1"))
            filas.append((fecha, "TIIEF_CERO", et, k * 28, round(float(cero[k - 1]), 4), fuente, f"{k}F1"))
    return pd.DataFrame(filas, columns=["fecha", "curva", "nodo", "plazo_dias", "tasa", "fuente", "serie"])


def main():
    archivos = sorted(CRUDOS.glob("tasas_*.parquet"))
    swaps = filas_tiief()
    if not archivos and swaps.empty:
        sys.exit("No hay crudos de tasas. Corre primero src/cosechar_tasas.py y/o src/cosechar_mexder.py")
    DATOS.mkdir(exist_ok=True)
    con = duckdb.connect(str(DATOS / "carteras.duckdb"))
    con.execute("DROP TABLE IF EXISTS tasas")
    con.register("swaps", swaps)
    publicas = ""
    if archivos:
        lista = ", ".join(f"'{p.as_posix()}'" for p in archivos)
        publicas = f"""
        SELECT fecha::DATE, curva, nodo, plazo_dias::INT, tasa::DOUBLE, fuente, serie
        FROM (
            SELECT *, row_number() OVER (PARTITION BY fecha, curva, nodo, fuente
                                         ORDER BY filename DESC) AS rn
            FROM read_parquet([{lista}], filename = true)
        ) WHERE rn = 1
        UNION ALL"""
    con.execute(f"""
        CREATE TABLE tasas AS
        SELECT * FROM ({publicas}
            SELECT fecha::DATE, curva, nodo, plazo_dias::INT, tasa::DOUBLE, fuente, serie FROM swaps
        ) t(fecha, curva, nodo, plazo_dias, tasa, fuente, serie)
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
