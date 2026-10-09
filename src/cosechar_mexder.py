#!/usr/bin/env python3
"""
Baja y lee el Boletín de Swaps de MexDer (swaps de TIIE de Fondeo) y guarda un
snapshot crudo en crudos/mexder_swaps_AAAAMMDD.parquet.

Qué trae el boletín
-------------------
Un contrato por plazo: `nF1` = swap de TIIE de Fondeo a n periodos de 28 días,
de 1F1 (28 días) a 390F1 (30 años). Para cada uno, el PRECIO DE LIQUIDACIÓN
(la tasa par que MexDer asigna ese día, se haya operado o no), el del día
anterior y el interés abierto. MexDer lo publica con 2 decimales.

MexDer sólo publica el boletín del día (el mismo link se sobrescribe), así que la
historia se junta corrida a corrida; no hay archivo histórico público.

Uso:
    python src/cosechar_mexder.py                    # baja el boletín del día
    python src/cosechar_mexder.py --pdf ruta.pdf ... # lee PDFs ya descargados
"""
from __future__ import annotations

import argparse, io, re, sys
import datetime as dt
from pathlib import Path

import pandas as pd
import requests
from pypdf import PdfReader

RAIZ = Path(__file__).resolve().parents[1]
CRUDOS = RAIZ / "crudos"
URL = ("http://www.mexder.com.mx/wb3/wb/MEX/MEX_Repositorio/_vtp/MEX/11bb_boletin_diario_2025/_rid/21/_mto/3/"
       "Boletin_de_Swaps_Basico.pdf?repfop=view&reptp=11bb_boletin_diario_2025&repfiddoc=8979&repinline=true")
MESES = {m: i for i, m in enumerate(["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
                                     "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"], 1)}
FILA = re.compile(r"^(\d+)F1 ([\d.]+|-+) ([\d.]+|-+) ([\d,]+|-+)\s*$", re.M)
FECHA = re.compile(r"(\d{1,2}) DE ([A-ZÁÉÍÓÚ]+) DE (\d{4})")


def leer(pdf_bytes: bytes) -> pd.DataFrame:
    txt = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    m = FECHA.search(txt)
    if not m or m.group(2) not in MESES:
        sys.exit("No encontré la fecha del boletín; ¿cambió el formato?")
    fecha = dt.date(int(m.group(3)), MESES[m.group(2)], int(m.group(1)))
    num = lambda s: None if s.startswith("-") else float(s.replace(",", ""))
    filas = [(fecha, int(n), int(n) * 28, num(s), num(a), num(oi)) for n, a, s, oi in FILA.findall(txt)]
    df = pd.DataFrame(filas, columns=["fecha", "n", "plazo_dias", "tasa", "tasa_anterior", "interes_abierto"])
    df = df.dropna(subset=["tasa"]).drop_duplicates("n").sort_values("n")
    if len(df) < 300 or df.n.min() != 1:
        sys.exit(f"Sólo leí {len(df)} contratos (esperaba ~390); ¿cambió el formato?")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", nargs="*", help="PDFs ya descargados en lugar de bajar el del día")
    a = ap.parse_args()
    fuentes = [Path(p).read_bytes() for p in a.pdf] if a.pdf else [
        requests.get(URL, timeout=90, headers={"User-Agent": "carteras-institucionales/1.0"}).content]
    CRUDOS.mkdir(exist_ok=True)
    for b in fuentes:
        if not b.startswith(b"%PDF"):
            sys.exit("MexDer no devolvió un PDF (¿sitio caído o bloqueado?).")
        df = leer(b)
        f = df.fecha.iloc[0]
        salida = CRUDOS / f"mexder_swaps_{f:%Y%m%d}.parquet"
        df.to_parquet(salida, index=False)
        print(f"{salida.relative_to(RAIZ)}  {len(df)} contratos · 1F1 {df.tasa.iloc[0]:.2f}% … "
              f"{df.n.iloc[-1]}F1 {df.tasa.iloc[-1]:.2f}%")


if __name__ == "__main__":
    main()
