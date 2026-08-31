#!/usr/bin/env python3
"""
Cosecha las series historicas de CONSAR/SISET (Siefores Generacionales) con
desglose por afore, y guarda un snapshot crudo con fecha en crudos/.

Por que existe este script
--------------------------
El boton "Exportar" del portal esta ROTO del lado de CONSAR para Excel y CSV:
su servidor intenta generar el archivo con Excel via COM y falla con
"Retrieving the COM class factory for component with CLSID
{00024500-0000-0000-C000-000000000046} failed ... 80040154".

El formato IQY (Excel Web Query) si funciona, y ese archivo revela el endpoint
que el portal usa por debajo -- sin postbacks, sin __VIEWSTATE, sin Excel:

    POST /gobmx/aplicativo/siset/ExportaSeriesHistoricas.aspx?t=IQY_HTML
    cd=<id>&nl=Detalle&monthIni=1&yearIni=2019&monthFin=7&yearFin=2026
    &seriesSeleccionadas=<id>|<id>|...

Devuelve una tabla HTML con la serie mensual completa. `nl=Detalle` da el
desglose por administradora.

Estructura de SISET, ya mapeada:
    cd base (clase de activo) -> ddl_pivote -> cd por siefore -> filas = afores

Uso:
    python src/cosechar_consar.py                 # rango por defecto
    python src/cosechar_consar.py --fin 8/2026    # hasta agosto de 2026
"""
from __future__ import annotations
import argparse, json, re, sys, time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

RAIZ = Path(__file__).resolve().parents[1]
CRUDOS = RAIZ / "crudos"

BASE    = "https://www.consar.gob.mx/gobmx/aplicativo/siset"
SERIES  = BASE + "/Series.aspx?cd={cd}&cdAlt=True"
EXPORTA = BASE + "/ExportaSeriesHistoricas.aspx?t=IQY_HTML"

# Cuadros base, uno por clase de activo (Siefores Generacionales).
CLASES = {
    259: "Renta Variable Nacional",
    271: "Renta Variable Internacional",
    283: "Deuda Privada Nacional",
    295: "Mercancias",
    307: "Estructurados y FIBRAS",
    319: "Deuda Internacional",
    331: "Deuda Gubernamental",
    343: "Otros Activos",
}

# Las primeras 11 filas de cada cuadro son el total de la clase + las 10 afores.
FILAS_POR_CUADRO = 11

ses = requests.Session()
ses.headers["User-Agent"] = "Mozilla/5.0 (carteras-mx; cosecha de datos publicos)"


def sopa(url: str) -> BeautifulSoup:
    r = ses.get(url, timeout=120)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def siefores_de(cd_base: int) -> list[tuple[str, str]]:
    """[(cd, nombre)] de las siefores dentro de un cuadro base."""
    sel = sopa(SERIES.format(cd=cd_base)).find(
        "select", id="ctl00_ContentPlaceHolder1_ddl_pivote")
    if not sel:
        return []
    return [(o.get("value"), o.get_text(strip=True))
            for o in sel.find_all("option")
            if o.get_text(strip=True).startswith("Siefore")]


def ids_de(cd: str) -> list[str]:
    s = sopa(SERIES.format(cd=cd))
    return [c.get("value") for c in s.find_all("input", id="chkSerie")][:FILAS_POR_CUADRO]


def exportar(cd: str, ids: list[str], rango: dict) -> str:
    datos = dict(cd=cd, nl="Detalle", seriesSeleccionadas="|".join(ids), **rango)
    r = ses.post(EXPORTA, data=datos, timeout=180)
    r.raise_for_status()
    return r.text


def tabla(html: str):
    """-> (periodos, [(etiqueta, [valores])]). No convierte numeros: eso es del cargador."""
    t = BeautifulSoup(html, "html.parser").find("table")
    if not t:
        return None, []
    periodos, filas = None, []
    for tr in t.find_all("tr"):
        celdas = [td.get_text(" ", strip=True).replace("\xa0", " ").strip()
                  for td in tr.find_all(["td", "th"])]
        nz = [c for c in celdas if c]
        if not nz:
            continue
        if nz[0].startswith("Descripci"):
            periodos = nz[1:]
        elif periodos and len(nz) > 3 and re.match(r"^-?[\d.,]+$|^N/A$", nz[1]):
            filas.append((nz[0], nz[1:]))
    return periodos, filas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ini", default="1/2019", help="mes/anio inicial (default 1/2019)")
    ap.add_argument("--fin", default="", help="mes/anio final; vacio = mes anterior al actual")
    ap.add_argument("--pausa", type=float, default=1.0)
    a = ap.parse_args()

    mi, ai = (int(x) for x in a.ini.split("/"))
    if a.fin:
        mf, af = (int(x) for x in a.fin.split("/"))
    else:
        hoy = date.today()
        mf, af = (12, hoy.year - 1) if hoy.month == 1 else (hoy.month - 1, hoy.year)
    rango = dict(monthIni=mi, yearIni=ai, monthFin=mf, yearFin=af)
    print(f"rango: {mi}/{ai} -> {mf}/{af}\n")

    CRUDOS.mkdir(exist_ok=True)
    parcial = CRUDOS / "consar.parcial.json"
    hechos = json.loads(parcial.read_text("utf-8")) if parcial.exists() else {}
    if hechos:
        print(f"retomando: ya habia {len(hechos.get('bloques', {}))} bloques guardados\n")
    bloques = hechos.get("bloques", {})
    periodos = hechos.get("periodos")

    trabajos = [(nom, cd, sf) for cb, nom in CLASES.items() for cd, sf in siefores_de(cb)]
    print(f"{len(trabajos)} consultas por hacer\n")

    errores = []
    for i, (clase, cd, siefore) in enumerate(trabajos, 1):
        clave = f"{clase}||{siefore}"
        etq = f"[{i:>2}/{len(trabajos)}] {clase} / {siefore}"
        if clave in bloques:
            print(etq, "(ya estaba)")
            continue
        try:
            ids = ids_de(cd)
            if not ids:
                errores.append(f"{etq}: sin ids de serie"); print(etq, "SIN IDS"); continue
            per, filas = tabla(exportar(cd, ids, rango))
            if not filas:
                errores.append(f"{etq}: respuesta vacia"); print(etq, "VACIO"); continue
            periodos = periodos or per
            bloques[clave] = filas
            print(etq, f"ok ({len(filas)} filas)")
        except Exception as e:
            errores.append(f"{etq}: {e}"); print(etq, "ERROR", e)
        # guardado incremental: si se cae la red, no se pierde lo cosechado
        parcial.write_text(json.dumps({"periodos": periodos, "bloques": bloques},
                                      ensure_ascii=False), "utf-8")
        time.sleep(a.pausa)

    if not bloques:
        print("\nno se cosecho nada", file=sys.stderr)
        return 1

    # Snapshot crudo con fecha. Append-only: nunca se sobrescribe uno anterior.
    destino = CRUDOS / f"consar_{date.today():%Y%m%d}.json"
    destino.write_text(json.dumps(
        {"fuente": "CONSAR/SISET Inversiones de las Siefores Generacionales",
         "cosechado_en": date.today().isoformat(), "rango": rango,
         "periodos": periodos, "bloques": bloques},
        ensure_ascii=False), "utf-8")
    parcial.unlink(missing_ok=True)
    print(f"\n{len(bloques)} bloques x {len(periodos or [])} periodos -> {destino.name}")
    if errores:
        print("\nerrores:")
        for e in errores:
            print(" -", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
