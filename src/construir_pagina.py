#!/usr/bin/env python3
"""Inyecta datos/pagina.json en web/plantilla.html y escribe docs/index.html.
Si existe datos/pagina_fi.json, también construye la sección de tasas en docs/fi/index.html.

La pagina queda autocontenida (los datos van dentro del HTML), asi funciona
igual en Cloudflare Pages, GitHub Pages o como artifact, sin depender de que
un fetch a un archivo aparte este permitido.
"""
from pathlib import Path
import sys

RAIZ = Path(__file__).resolve().parents[1]
datos = RAIZ / "datos" / "pagina.json"
fondos = RAIZ / "datos" / "pagina_fondos.json"
if not datos.exists():
    sys.exit("Falta datos/pagina.json. Corre primero src/cargar.py")
if not fondos.exists():
    sys.exit("Falta datos/pagina_fondos.json. Corre primero src/cargar_r7.py")

salida = RAIZ / "docs"
salida.mkdir(exist_ok=True)
html = (RAIZ / "web" / "plantilla.html").read_text("utf-8").replace(
    "__DATOS__", datos.read_text("utf-8")).replace(
    "__DATOS_FONDOS__", fondos.read_text("utf-8"))
(salida / "index.html").write_text(html, "utf-8")
print(f"docs/index.html  ({len(html)/1024:.0f} KB)")

# Sección "Tasas · FI" (opcional: sólo si ya se cargaron tasas)
fi = RAIZ / "datos" / "pagina_fi.json"
if fi.exists():
    (salida / "fi").mkdir(exist_ok=True)
    html_fi = (RAIZ / "web" / "fi.html").read_text("utf-8").replace("__DATOS_FI__", fi.read_text("utf-8"))
    (salida / "fi" / "index.html").write_text(html_fi, "utf-8")
    print(f"docs/fi/index.html  ({len(html_fi)/1024:.0f} KB)")
    # Risk premium: sólo se embeben los bonos de 10A (públicos); EPS y precios los carga el usuario en su navegador
    import json as _json
    pf = _json.loads(fi.read_text("utf-8"))
    def _serie(curva, nodo):
        c = pf["curvas"].get(curva)
        if not c or nodo not in [n["et"] for n in c["nodos"]]:
            return None
        j = [n["et"] for n in c["nodos"]].index(nodo)
        pares = [(f, v) for f, v in zip(c["fechas"], c["v"][j]) if v is not None]
        return {"f": [p[0] for p in pares], "v": [p[1] for p in pares]}
    bonos = {"generado": pf["generado"], "UST10": _serie("UST", "10A"), "MBONO10": _serie("MX_GUB", "10A")}
    (salida / "erp").mkdir(exist_ok=True)
    html_erp = (RAIZ / "web" / "erp.html").read_text("utf-8").replace(
        "__DATOS_ERP_BONOS__", _json.dumps(bonos, separators=(",", ":")))
    (salida / "erp" / "index.html").write_text(html_erp, "utf-8")
    print(f"docs/erp/index.html  ({len(html_erp)/1024:.0f} KB)")
else:
    print("datos/pagina_fi.json no existe: se omite la sección de tasas (corre src/cosechar_tasas.py y src/cargar_tasas.py)")
