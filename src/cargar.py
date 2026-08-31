#!/usr/bin/env python3
"""
Carga el snapshot crudo mas reciente de CONSAR a DuckDB, valida y exporta
Parquet + el JSON que consume la pagina.

Reglas que este script hace cumplir
-----------------------------------
1. Append-only     - cada corrida crea snapshots nuevos; los anteriores quedan
                     marcados superseded_at, nunca se borran.
2. Crudo primero   - el archivo de crudos/ es la fuente; el parseo es reproducible.
3. Idempotencia    - si el sha256 ya esta en ingest_log con status 'ok', no reprocesa.
4. Nunca imputar   - 'N/A' y el centinela -969696 entran como NULL + calidad_log.
5. Crudo y normalizado - se guarda peso_raw junto a peso.
6. as_of_date y publication_date son distintas y ambas se guardan.
7. Toda fila lleva source.

Hallazgos de la fuente que el codigo respeta
--------------------------------------------
- La fila TOTAL de cada cuadro se identifica POR POSICION (la primera), no por
  nombre: su etiqueta cambia entre cuadros ("Estructurados" vs "Estructurados y
  FIBRAS", "Mercancias" vs "Mercancias" con acento).
- "XXI Banorte" y "XXI-Banorte" son la MISMA afore escrita de dos formas segun
  el cuadro. Sin normalizar darian once afores donde hay diez.
- CONSAR calcula sus porcentajes sobre ACTIVOS NETOS. Las ocho clases no suman
  100% (mediana ~97.3%) y en 2020-2021 algunos meses pasan de 100%. No son una
  particion. No se normaliza nada para forzar el cierre.
"""
from __future__ import annotations
import datetime as dt, hashlib, json, sys, unicodedata
from pathlib import Path

import duckdb

RAIZ = Path(__file__).resolve().parents[1]
CRUDOS, DATOS = RAIZ / "crudos", RAIZ / "datos"

MESES = {"Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6,
         "Jul": 7, "Ago": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dic": 12}
CENTINELA = -969696.0
FUENTE = "CONSAR"

ORDEN_CLASES = ["Deuda Gubernamental", "Deuda Privada Nacional", "Deuda Internacional",
                "Renta Variable Nacional", "Renta Variable Internacional",
                "Estructurados y FIBRAS", "Mercancias", "Otros Activos"]


def fin_de_mes(p: str) -> dt.date:
    mes_txt, anio_txt = p.split()
    m, a = MESES[mes_txt[:3].capitalize()], 2000 + int(anio_txt)
    return dt.date(a, 12, 31) if m == 12 else dt.date(a, m + 1, 1) - dt.timedelta(days=1)


def normaliza_afore(s: str) -> str:
    """'XXI-Banorte' y 'XXI Banorte' son la misma entidad."""
    base = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    base = base.replace("-", " ").replace("  ", " ").strip().lower()
    return {"xxi banorte": "XXI-Banorte", "pensionissste": "PensionISSSTE",
            "sura": "SURA"}.get(base, s.replace("-", "-").strip())


def numero(v):
    """float o None. Nunca inventa un valor."""
    s = str(v).strip().replace(",", "")
    if s in ("", "N/A", "n/a", "-", "ND", "N.D."):
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return None if x == CENTINELA else x


def crudo_mas_reciente() -> Path:
    cands = sorted(CRUDOS.glob("consar_*.json"))
    if not cands:
        sys.exit("No hay snapshots en crudos/. Corre primero src/cosechar_consar.py")
    return cands[-1]


def main() -> int:
    DATOS.mkdir(exist_ok=True)
    ruta = crudo_mas_reciente()
    bytes_ = ruta.read_bytes()
    sha = hashlib.sha256(bytes_).hexdigest()
    crudo = json.loads(bytes_)
    periodos = crudo["periodos"]
    fechas = [fin_de_mes(p) for p in periodos]
    pub = dt.date.fromisoformat(crudo.get("cosechado_en", dt.date.today().isoformat()))

    con = duckdb.connect(str(DATOS / "carteras.duckdb"))
    con.execute((RAIZ / "src" / "esquema.sql").read_text("utf-8"))

    ya = con.execute("SELECT count(*) FROM ingest_log WHERE source=? AND sha256=? AND status='ok'",
                     [FUENTE, sha]).fetchone()[0]
    if ya:
        print(f"{ruta.name} ya estaba cargado (sha256 {sha[:12]}). Nada que hacer.")
        return 0

    ingest_id = con.execute("""INSERT INTO ingest_log
        (source,archivo,ruta_crudo,sha256,bytes,publication_date,status)
        VALUES (?,?,?,?,?,?,'en_proceso') RETURNING ingest_id""",
        [FUENTE, ruta.name, str(ruta.relative_to(RAIZ)), sha, len(bytes_), pub]).fetchone()[0]

    filas, calidad, siefores = [], [], set()
    for clave, rows in crudo["bloques"].items():
        clase, siefore = clave.split("||")
        siefores.add(siefore)
        for i, (etq, vals) in enumerate(rows):
            if len(vals) != len(fechas):
                calidad.append((ingest_id, FUENTE, "largo_inesperado", "error", siefore, None,
                                f"{clase}/{etq}: {len(vals)} valores vs {len(fechas)} periodos", None))
                continue
            es_total = (i == 0)                      # la primera fila es el total de la clase
            afore = None if es_total else normaliza_afore(etq)
            entity = f"CONSAR:{siefore}" + ("" if es_total else f":{afore}")
            for f, v in zip(fechas, vals):
                x = numero(v)
                if x is None:
                    calidad.append((ingest_id, FUENTE, "valor_ausente", "info", entity, f,
                                    f"{clase}: '{v}'", None))
                filas.append((entity, siefore, afore, f, pub, clase, x, str(v)))

    # Snapshots: una version por (entity, as_of_date); la anterior queda superseded.
    entidades = sorted({(r[0], r[3]) for r in filas})
    con.execute("""CREATE OR REPLACE TEMP TABLE nuevos(entity_id VARCHAR, as_of_date DATE)""")
    con.executemany("INSERT INTO nuevos VALUES (?,?)", entidades)
    con.execute("""UPDATE snapshots s SET superseded_at=now()
        WHERE s.source=? AND s.superseded_at IS NULL
          AND EXISTS (SELECT 1 FROM nuevos n
                      WHERE n.entity_id=s.entity_id AND n.as_of_date=s.as_of_date)""", [FUENTE])
    con.execute("""INSERT INTO snapshots (source,entity_id,as_of_date,publication_date,ingest_id,version)
        SELECT ?, n.entity_id, n.as_of_date, ?, ?,
               1 + coalesce((SELECT max(version) FROM snapshots s
                             WHERE s.entity_id=n.entity_id AND s.as_of_date=n.as_of_date),0)
        FROM nuevos n""", [FUENTE, pub, ingest_id])

    con.execute("CREATE OR REPLACE TEMP TABLE stage(entity_id VARCHAR, siefore VARCHAR, afore VARCHAR,"
                "as_of_date DATE, publication_date DATE, asset_class VARCHAR, peso DOUBLE, peso_raw VARCHAR)")
    con.executemany("INSERT INTO stage VALUES (?,?,?,?,?,?,?,?)", filas)
    con.execute("""INSERT INTO allocations
        (snapshot_id,source,entity_id,siefore,afore,as_of_date,publication_date,asset_class,peso,peso_raw,origen_peso)
        SELECT s.snapshot_id, ?, t.entity_id, t.siefore, t.afore, t.as_of_date, t.publication_date,
               t.asset_class, t.peso, t.peso_raw, 'reportado'
        FROM stage t JOIN snapshots s
          ON s.entity_id=t.entity_id AND s.as_of_date=t.as_of_date
         AND s.ingest_id=? AND s.superseded_at IS NULL""", [FUENTE, ingest_id])

    if calidad:
        con.executemany("""INSERT INTO calidad_log
            (ingest_id,source,regla,severidad,entity_id,as_of_date,detalle,valor)
            VALUES (?,?,?,?,?,?,?,?)""", calidad)

    con.execute("""INSERT INTO entities (entity_id,source,clave_nativa,nombre,entity_type,subtipo,operadora)
        SELECT DISTINCT entity_id, ?, coalesce(afore,siefore),
               siefore || coalesce(' · '||afore,''), 'vehiculo','siefore', afore
        FROM allocations WHERE source=?
          AND entity_id NOT IN (SELECT entity_id FROM entities)""", [FUENTE, FUENTE])

    con.execute("""INSERT OR REPLACE INTO sources VALUES
      ('CONSAR','CONSAR/SISET Inversiones de las Siefores Generacionales','mensual',45,
       'allocations','% a valor de mercado sobre ACTIVOS NETOS (criterio de CONSAR)',
       'Cifras preliminares. Las 8 clases no suman 100% ni son disjuntas.')""")
    con.execute("""INSERT OR REPLACE INTO parametros VALUES
      ('consar.denominador','activos_netos',
       'CONSAR reporta cada clase como % a valor de mercado sobre activos netos. NO es comparable con el denominador de R7 (Directo+Reporto+Garantias sobre la cartera de inversion).',
       ?, 'Nota al pie del cuadro de SISET, verificada 2026-08-28')""", [dt.date.today()])

    n = con.execute("SELECT count(*) FROM allocations WHERE source=?", [FUENTE]).fetchone()[0]
    con.execute("UPDATE ingest_log SET status='ok', filas_leidas=?, filas_cargadas=? WHERE ingest_id=?",
                [len(filas), len(filas), ingest_id])

    # ---------------- validaciones ----------------
    print(f"\narchivo   : {ruta.name}  (sha256 {sha[:12]})")
    print(f"cargadas  : {len(filas)} filas · total en la base: {n}")
    print(f"periodos  : {len(fechas)}  {fechas[0]} -> {fechas[-1]}")
    print(f"siefores  : {len(siefores)} · afores: "
          + str(con.execute("SELECT count(DISTINCT afore) FROM allocations WHERE afore IS NOT NULL").fetchone()[0]))
    print(f"ausentes  : {sum(1 for c in calidad if c[2]=='valor_ausente')} valores NULL (no imputados)")

    print("\n-- suma de las 8 clases por (siefore, afore, mes) --")
    print(con.execute("""SELECT round(min(s),2) minimo, round(median(s),2) mediana,
      round(max(s),2) maximo, count(*) casos FROM
      (SELECT siefore,afore,as_of_date,sum(peso) s FROM allocations
       WHERE afore IS NOT NULL AND peso IS NOT NULL GROUP BY 1,2,3 HAVING count(*)=8)"""
    ).df().to_string(index=False))

    print("\n-- vigencia observada por siefore --")
    print(con.execute("""SELECT siefore, min(as_of_date) desde, max(as_of_date) hasta
      FROM allocations WHERE peso IS NOT NULL GROUP BY 1 ORDER BY 2,1""").df().to_string(index=False))

    print("\n-- huecos: meses sin dato entre el primero y el ultimo de cada siefore --")
    print(con.execute("""WITH r AS (SELECT siefore,min(as_of_date) a,max(as_of_date) b
        FROM allocations WHERE peso IS NOT NULL GROUP BY 1),
      obs AS (SELECT siefore,count(DISTINCT as_of_date) n FROM allocations
              WHERE peso IS NOT NULL GROUP BY 1)
      SELECT r.siefore, obs.n meses_con_dato,
             datediff('month', r.a, r.b)+1 meses_esperados,
             datediff('month', r.a, r.b)+1-obs.n huecos
      FROM r JOIN obs USING(siefore) WHERE datediff('month',r.a,r.b)+1-obs.n <> 0"""
    ).df().to_string(index=False) or "  (ninguno)")

    print("\n-- log de calidad --")
    print(con.execute("SELECT regla,severidad,count(*) n FROM calidad_log GROUP BY 1,2"
                      ).df().to_string(index=False))

    con.execute(f"COPY (SELECT * FROM allocations) TO '{DATOS/'allocations.parquet'}' (FORMAT PARQUET)")

    # ---------------- JSON para la pagina ----------------
    fechas_s = [f.isoformat() for f in fechas]
    idx = {f: i for i, f in enumerate(fechas_s)}
    sf = sorted(siefores, key=lambda s: (s != "Siefore Pensiones", s))
    af = [r[0] for r in con.execute(
        "SELECT DISTINCT afore FROM allocations WHERE afore IS NOT NULL ORDER BY 1").fetchall()]
    series = {s: {a: {c: [None]*len(fechas) for c in ORDEN_CLASES} for a in af} for s in sf}
    for s, a, c, f, p in con.execute("""SELECT siefore,afore,asset_class,as_of_date,peso
        FROM allocations WHERE afore IS NOT NULL""").fetchall():
        if s in series and a in series[s] and c in series[s][a]:
            series[s][a][c][idx[f.isoformat()]] = p
    (DATOS / "pagina.json").write_text(json.dumps({
        "fuente": "CONSAR · SISET · Inversiones de las Siefores Generacionales",
        "denominador": "% a valor de mercado sobre activos netos (criterio de CONSAR)",
        "publicado": pub.isoformat(), "actualizado": dt.date.today().isoformat(),
        "fechas": fechas_s, "clases": ORDEN_CLASES, "siefores": sf, "afores": af,
        "series": series}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"\nParquet y pagina.json escritos en datos/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
