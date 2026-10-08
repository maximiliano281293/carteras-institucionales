#!/usr/bin/env python3
"""
Importación ÚNICA de historia de la curva swap de TIIE de Fondeo desde tu base
Postgres, para tener historia antes de que empiece la cosecha diaria del
boletín de MexDer (que no tiene archivo histórico).

Corre en tu laptop (la base no es accesible desde GitHub):

    pip install psycopg2-binary
    export PG_URL="postgresql://usuario:password@localhost:5432/apps_db"
    python src/importar_historia_pg.py --consulta "
        SELECT fecha, plazo_dias, tasa
        FROM   mi_tabla_de_curvas
        WHERE  curva = 'TIIE_FONDEO_IRS'"

La consulta debe devolver exactamente: fecha, plazo_dias, tasa (tasa par del swap).
Si la tasa viene en decimal (0.0725) agrega --decimal.

Resultado: crudos/historia_tiief_pg.parquet, mismo formato que el boletín de
MexDer. Cuando hay boletín de MexDer para una fecha, gana el de MexDer.

OJO: el repo y el sitio son públicos. Si estos datos vienen de un proveedor de
precios (Valmer, PiP, Bloomberg…), revisa que la licencia permita publicarlos
antes de hacer commit de este archivo.
"""
from __future__ import annotations

import argparse, os, sys
from pathlib import Path

import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--consulta", required=True, help="SQL que devuelve fecha, plazo_dias, tasa")
    ap.add_argument("--decimal", action="store_true", help="la tasa viene en decimal, no en %")
    ap.add_argument("--hasta", help="última fecha a importar (AAAA-MM-DD); después manda MexDer")
    a = ap.parse_args()

    url = os.environ.get("PG_URL")
    if not url:
        sys.exit("Falta la variable PG_URL (cadena de conexión a Postgres).")
    from sqlalchemy import create_engine, text
    with create_engine(url).connect() as con:
        df = pd.read_sql(text(a.consulta), con)
    df.columns = [c.lower() for c in df.columns]
    falta = {"fecha", "plazo_dias", "tasa"} - set(df.columns)
    if falta:
        sys.exit(f"La consulta debe devolver fecha, plazo_dias, tasa. Faltan: {falta}")
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["plazo_dias"] = df["plazo_dias"].astype(int)
    df["tasa"] = df["tasa"].astype(float) * (100 if a.decimal else 1)
    if a.hasta:
        df = df[df.fecha <= pd.Timestamp(a.hasta).date()]
    # validaciones básicas: tasas en rango razonable y suficientes plazos por fecha
    fuera = df[(df.tasa < 0) | (df.tasa > 40)]
    if len(fuera):
        sys.exit(f"{len(fuera)} tasas fuera de 0-40%: ¿unidades? (usa --decimal si vienen en decimal)")
    por_fecha = df.groupby("fecha").size()
    pocas = por_fecha[por_fecha < 5]
    if len(pocas):
        print(f"Aviso: {len(pocas)} fechas con menos de 5 plazos; se descartan.")
        df = df[~df.fecha.isin(pocas.index)]
    df["n"] = (df.plazo_dias / 28).round().astype(int).clip(lower=1)
    df = (df.sort_values(["fecha", "plazo_dias"]).drop_duplicates(["fecha", "n"])
            [["fecha", "n", "plazo_dias", "tasa"]].assign(fuente="postgres"))
    salida = RAIZ / "crudos" / "historia_tiief_pg.parquet"
    salida.parent.mkdir(exist_ok=True)
    df.to_parquet(salida, index=False)
    print(f"{salida.relative_to(RAIZ)}  {df.fecha.nunique()} fechas · {df.fecha.min()} → {df.fecha.max()} · "
          f"{df.groupby('fecha').size().median():.0f} plazos por fecha (mediana)")


if __name__ == "__main__":
    main()
