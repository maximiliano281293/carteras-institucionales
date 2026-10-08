#!/usr/bin/env python3
"""
Cosecha tasas de referencia PÚBLICAS para la sección "Tasas · FI" y guarda un
snapshot crudo con fecha en crudos/tasas_AAAAMMDD.parquet.

Fuentes (todas gratuitas)
-------------------------
- Banxico SIE (API REST, token gratis en
  https://www.banxico.org.mx/SieAPIRest/service/v1/token) -> variable de entorno
  BANXICO_TOKEN (en GitHub: Settings -> Secrets -> Actions).
    * TIIE de Fondeo a un día y TIIE 28 / 91 / 182 (diarias)
    * Cetes 28 / 91 / 182 / 364 / 728 y Bonos M 3 / 5 / 7 / 10 / 20 / 30 años:
      tasa de la SUBASTA primaria (semanal o menos frecuente; no hay dato diario)
- NY Fed Markets API (sin token): SOFR overnight.
- U.S. Treasury (CSV público): curva par diaria 1M - 30A.

Lo que NO hay gratis: las curvas swap completas (IRS TIIE de Fondeo, SOFR OIS).
Eso viene de proveedores de precios y no se publica aquí.

Reglas
------
- Crudo primero: el snapshot guarda exactamente lo que devolvió cada fuente
  (fecha, serie, valor) más el título que la fuente da a la serie.
- Nunca imputar: si una serie no tiene dato un día ("N/E"), no hay fila.
- Los ids de Banxico se VERIFICAN contra el título que devuelve la API; si un
  id no corresponde a lo esperado, la corrida falla en lugar de cargar basura.

Uso:
    python src/cosechar_tasas.py                     # últimos 45 días
    python src/cosechar_tasas.py --historia-completa # todo lo disponible
    python src/cosechar_tasas.py --desde 2020-01-01
"""
from __future__ import annotations

import argparse, io, os, sys, time, unicodedata
import datetime as dt
from pathlib import Path

import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parents[1]
CRUDOS = RAIZ / "crudos"
UA = {"User-Agent": "carteras-institucionales/1.0 (+github)"}

# id -> (curva, nodo, plazo_dias, palabras que DEBEN aparecer en el título de Banxico)
BANXICO = {
    "SF331451": ("TIIE",   "1D",  1,     ["fondeo"]),
    "SF43783":  ("TIIE",   "1M",  28,    ["28"]),
    "SF43878":  ("TIIE",   "3M",  91,    ["91"]),
    "SF111916": ("TIIE",   "6M",  182,   ["182"]),
    "SF43936":  ("MX_GUB", "1M",  28,    ["cetes", "28"]),
    "SF43939":  ("MX_GUB", "3M",  91,    ["cetes", "91"]),
    "SF43942":  ("MX_GUB", "6M",  182,   ["cetes", "182"]),
    "SF43945":  ("MX_GUB", "1A",  364,   ["cetes", "364"]),
    "SF349785": ("MX_GUB", "2A",  728,   ["cetes"]),
    "SF43883":  ("MX_GUB", "3A",  1092,  ["bono", "3"]),
    "SF43886":  ("MX_GUB", "5A",  1820,  ["bono", "5"]),
    "SF44946":  ("MX_GUB", "7A",  2548,  ["bono", "7"]),
    "SF44071":  ("MX_GUB", "10A", 3640,  ["bono", "10"]),
    "SF45384":  ("MX_GUB", "20A", 7280,  ["bono", "20"]),
    "SF60696":  ("MX_GUB", "30A", 10920, ["bono", "30"]),
}

UST = {"1 Mo": ("1M", 30), "2 Mo": ("2M", 61), "3 Mo": ("3M", 91), "4 Mo": ("4M", 122),
       "6 Mo": ("6M", 182), "1 Yr": ("1A", 365), "2 Yr": ("2A", 730), "3 Yr": ("3A", 1095),
       "5 Yr": ("5A", 1825), "7 Yr": ("7A", 2555), "10 Yr": ("10A", 3650),
       "20 Yr": ("20A", 7300), "30 Yr": ("30A", 10950)}

INICIO = {"banxico": dt.date(2006, 1, 2), "nyfed": dt.date(2018, 4, 2), "treasury": dt.date(2000, 1, 3)}
COLS = ["fecha", "curva", "nodo", "plazo_dias", "tasa", "fuente", "serie", "titulo"]


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def tramos(desde: dt.date, hasta: dt.date, anios: int = 1):
    ini = desde
    while ini <= hasta:
        fin = min(dt.date(ini.year + anios, 1, 1) - dt.timedelta(days=1), hasta)
        yield ini, fin
        ini = fin + dt.timedelta(days=1)


def _get(url, **kw):
    for intento in range(4):
        try:
            r = requests.get(url, timeout=90, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            if intento == 3:
                raise
            espera = 5 * (intento + 1)
            print(f"   reintento en {espera}s ({e})")
            time.sleep(espera)


# ---------------------------------------------------------------- Banxico
def banxico(token: str, desde: dt.date, hasta: dt.date) -> pd.DataFrame:
    filas, verificados = [], set()
    for d, h in tramos(desde, hasta):
        url = (f"https://www.banxico.org.mx/SieAPIRest/service/v1/series/{','.join(BANXICO)}"
               f"/datos/{d:%Y-%m-%d}/{h:%Y-%m-%d}")
        js = _get(url, headers={**UA, "Bmx-Token": token, "Accept": "application/json"}).json()
        n = 0
        for s in js["bmx"]["series"]:
            sid, titulo = s["idSerie"], s.get("titulo", "")
            curva, nodo, plazo, claves = BANXICO[sid]
            if sid not in verificados:
                t = _sin_acentos(titulo)
                falta = [c for c in claves if c not in t]
                if falta:
                    sys.exit(f"Banxico {sid}: el título «{titulo}» no contiene {falta}. "
                             f"Revisa el id en el catálogo antes de cargar.")
                verificados.add(sid)
            for x in s.get("datos") or []:
                if x["dato"] in ("N/E", "", None):
                    continue
                filas.append((dt.datetime.strptime(x["fecha"], "%d/%m/%Y").date(), curva, nodo, plazo,
                              float(x["dato"].replace(",", "")), "banxico", sid, titulo))
                n += 1
        print(f"  banxico  {d} → {h}  {n:>6}")
        time.sleep(0.4)
    return pd.DataFrame(filas, columns=COLS)


# ---------------------------------------------------------------- NY Fed
def nyfed(desde: dt.date, hasta: dt.date) -> pd.DataFrame:
    filas = []
    for d, h in tramos(desde, hasta):
        url = ("https://markets.newyorkfed.org/api/rates/secured/sofr/search.json"
               f"?startDate={d:%Y-%m-%d}&endDate={h:%Y-%m-%d}")
        rr = _get(url, headers=UA).json().get("refRates", [])
        for x in rr:
            filas.append((dt.date.fromisoformat(x["effectiveDate"][:10]), "SOFR", "1D", 1,
                          float(x["percentRate"]), "nyfed", "SOFR", "Secured Overnight Financing Rate"))
        print(f"  nyfed    {d} → {h}  {len(rr):>6}")
    return pd.DataFrame(filas, columns=COLS)


# ---------------------------------------------------------------- Treasury
def treasury(desde: dt.date, hasta: dt.date) -> pd.DataFrame:
    filas = []
    for anio in range(desde.year, hasta.year + 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{anio}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={anio}&page&_format=csv")
        raw = pd.read_csv(io.StringIO(_get(url, headers=UA).text))
        raw["Date"] = pd.to_datetime(raw["Date"]).dt.date
        raw = raw[(raw["Date"] >= desde) & (raw["Date"] <= hasta)]
        n = 0
        for _, r in raw.iterrows():
            for col, (nodo, plazo) in UST.items():
                if col in raw.columns and pd.notna(r[col]):
                    filas.append((r["Date"], "UST", nodo, plazo, float(r[col]), "treasury", col,
                                  "Daily Treasury Par Yield Curve Rates"))
                    n += 1
        print(f"  treasury {anio}  {n:>6}")
    return pd.DataFrame(filas, columns=COLS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", type=dt.date.fromisoformat)
    ap.add_argument("--hasta", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--historia-completa", action="store_true")
    ap.add_argument("--solo", choices=["banxico", "nyfed", "treasury"])
    a = ap.parse_args()

    def rango(fuente):
        if a.historia_completa:
            return INICIO[fuente], a.hasta
        return (a.desde or a.hasta - dt.timedelta(days=45)), a.hasta

    partes, fallas = [], []
    token = os.environ.get("BANXICO_TOKEN", "").strip()
    trabajos = {"banxico": lambda d, h: banxico(token, d, h), "nyfed": nyfed, "treasury": treasury}
    for nombre, f in trabajos.items():
        if a.solo and nombre != a.solo:
            continue
        if nombre == "banxico" and not token:
            print("banxico: falta BANXICO_TOKEN; se omite")
            fallas.append(nombre)
            continue
        d, h = rango(nombre)
        print(f"{nombre}: {d} → {h}")
        try:
            partes.append(f(d, h))
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"{nombre}: ERROR {e}")
            fallas.append(nombre)

    df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=COLS)
    if df.empty:
        sys.exit("No se descargó nada.")
    CRUDOS.mkdir(exist_ok=True)
    salida = CRUDOS / f"tasas_{dt.date.today():%Y%m%d}.parquet"
    if salida.exists():  # varias corridas el mismo día: se conserva todo
        df = pd.concat([pd.read_parquet(salida), df]).drop_duplicates(["fecha", "curva", "nodo", "fuente"], keep="last")
    df.to_parquet(salida, index=False)
    print(f"{salida.relative_to(RAIZ)}  {len(df)} filas · {df.fecha.min()} → {df.fecha.max()}")
    if fallas:
        print(f"Fuentes con problema: {', '.join(fallas)}")


if __name__ == "__main__":
    main()
