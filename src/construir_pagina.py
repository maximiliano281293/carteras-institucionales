#!/usr/bin/env python3
"""Inyecta datos/pagina.json en web/plantilla.html y escribe docs/index.html.

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
