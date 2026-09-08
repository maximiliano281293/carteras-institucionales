#!/usr/bin/env python3
"""
Carga los Parquet crudos de CNBV R7 (salida de extraer_r7.py) a DuckDB, a nivel
posicion (holdings), calcula la composicion por clase de activo (allocations) y
exporta Parquet + el JSON que consume la seccion de fondos de la pagina.

    python src/cargar_r7.py                      # todos los crudos/r7_*.parquet nuevos
    python src/cargar_r7.py crudos/r7_2026_xxx.parquet

Mismas reglas que cargar.py (CONSAR): append-only, idempotente por sha256, nada se
imputa, crudo y normalizado juntos, toda fila lleva source.

Decisiones que este script hace cumplir (todas viven en tablas, no en el codigo):
  - Denominador (parametros.cnbv.denominador): Directo + Reporto + Garantias +
    Prestamo de valores. Los DERIVADOS quedan fuera: su "valor" es P&L firmado, no
    una tenencia. Se guardan igual en holdings y salen como clase 'Derivados' en
    allocations con peso sobre el mismo denominador, para mostrarse aparte.
  - Clase de activo: catalogos/asset_class_map_cnbv.csv. Regla por TV ('*') y
    excepciones por emisora (ETFs). Lo que cae en un default marcado
    'juicio_pendiente' se registra en calidad_log.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, sys
from pathlib import Path

import duckdb
import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
CRUDOS, DATOS, CATALOGOS = RAIZ / "crudos", RAIZ / "datos", RAIZ / "catalogos"
FUENTE = "CNBV"
DERIV = "Inversiones en instrumentos financieros derivados"

ORDEN_CLASES = ["Deuda Gubernamental", "Deuda Privada Nacional", "Deuda Internacional",
                "Renta Variable Nacional", "Renta Variable Internacional",
                "Estructurados y FIBRAS", "Mercancias", "Fondos de inversión",
                "Efectivo y equivalentes"]
FUERA_COMPOSICION = ["Derivados"]


# Tipos de valor en los que la SERIE es la fecha de vencimiento en formato AAMMDD.
# Verificado sobre el crudo 2026: 99.05% del valor de estos TV parsea a una fecha
# valida. En los CB corporativos (91, 94, 95, 93, CD...) la serie es el numero de
# emision, NO el vencimiento, y por eso no se intenta convertir.
TV_SERIE_ES_FECHA = {"M", "MS", "S", "BI", "LF", "LG", "LD", "IQ", "IM", "IS", "2U",
                     "MP", "MC", "SP", "SC", "D1SP", "D2", "D2SP", "D4SP", "D5SP",
                     "D7SP", "D8", "D8SP", "JE"}
SERIE_SIN_VALOR = {"", "N", "*", "-", "NA"}   # 'sin serie' en el reporte


def vencimiento(tv: str, serie) -> dt.date | None:
    """Fecha de vencimiento leida de la serie, o None. Nunca la inventa."""
    if tv not in TV_SERIE_ES_FECHA or not isinstance(serie, str):
        return None
    t = serie.strip()
    if len(t) != 6 or not t.isdigit():
        return None
    try:
        f = dt.date(2000 + int(t[:2]), int(t[2:4]), int(t[4:6]))
    except ValueError:
        return None
    return f if dt.date(2015, 1, 1) <= f <= dt.date(2099, 12, 31) else None


def nombre_completo(emisora, serie) -> str:
    """'BONOS' + '300228' -> 'BONOS 300228'. La serie ES la identidad del papel."""
    e = (emisora or "").strip()
    t = (serie or "").strip() if isinstance(serie, str) else ""
    return f"{e} {t}" if t and t.upper() not in SERIE_SIN_VALOR else e


def fin_de_mes(p: int) -> dt.date:
    a, m = divmod(int(p), 100)
    return dt.date(a, 12, 31) if m == 12 else dt.date(a, m + 1, 1) - dt.timedelta(days=1)


def cargar_catalogo(con) -> pd.DataFrame:
    ruta = CATALOGOS / "asset_class_map_cnbv.csv"
    cat = pd.read_csv(ruta, dtype=str).fillna({"subclase": "", "notes": ""})
    cat["emisora_pattern"] = cat["emisora_pattern"].fillna("*")
    dup = cat.duplicated(["tv_pattern", "emisora_pattern"])
    if dup.any():
        sys.exit(f"Catalogo con reglas duplicadas: {cat[dup][['tv_pattern','emisora_pattern']].values.tolist()}")
    con.execute("DELETE FROM asset_class_map WHERE source=?", [FUENTE])
    con.executemany("""INSERT INTO asset_class_map
        (source,tv_pattern,emisora_pattern,asset_class,subclase,mapped_by,notes) VALUES (?,?,?,?,?,?,?)""",
        cat[["source", "tv_pattern", "emisora_pattern", "asset_class", "subclase", "mapped_by", "notes"]].values.tolist())
    return cat


def clasificar(df: pd.DataFrame, cat: pd.DataFrame) -> pd.DataFrame:
    """Une la clase de activo: primero por (TV, emisora), luego por (TV, '*')."""
    por_emisora = cat[cat.emisora_pattern != "*"].rename(columns={"tv_pattern": "cve_tipo_valor", "emisora_pattern": "dat_emisora"})
    por_tv = cat[cat.emisora_pattern == "*"].rename(columns={"tv_pattern": "cve_tipo_valor"})
    out = df.merge(por_emisora[["cve_tipo_valor", "dat_emisora", "asset_class", "subclase", "mapped_by"]],
                   on=["cve_tipo_valor", "dat_emisora"], how="left")
    falt = out.asset_class.isna()
    tv = out.loc[falt, ["cve_tipo_valor"]].merge(por_tv[["cve_tipo_valor", "asset_class", "subclase", "mapped_by"]],
                                                on="cve_tipo_valor", how="left")
    out.loc[falt, ["asset_class", "subclase", "mapped_by"]] = tv[["asset_class", "subclase", "mapped_by"]].values
    return out


def cargar_uno(con, ruta: Path, publicado: dt.date | None) -> int:
    bytes_ = ruta.read_bytes()
    sha = hashlib.sha256(bytes_).hexdigest()
    if con.execute("SELECT count(*) FROM ingest_log WHERE source=? AND sha256=? AND status='ok'", [FUENTE, sha]).fetchone()[0]:
        print(f"{ruta.name} ya estaba cargado (sha256 {sha[:12]}). Nada que hacer.")
        return 0

    df = pd.read_parquet(ruta)
    df["cve_periodo"] = df["cve_periodo"].astype(int)
    df["as_of_date"] = df["cve_periodo"].map(fin_de_mes)
    df["entity_id"] = "CNBV:" + df["cve_pizarra"].astype(str)
    df["instrument_id"] = ("CNBV:" + df["cve_tipo_valor"].astype(str) + ":" + df["dat_emisora"].astype(str)
                           + ":" + df["dat_serie"].fillna("").astype(str))
    cat = cargar_catalogo(con)
    df = clasificar(df, cat)
    sin_clase = df.asset_class.isna()
    if sin_clase.any():
        sys.exit(f"TV sin regla en el catalogo: {sorted(df[sin_clase].cve_tipo_valor.unique())}. Agregalos a asset_class_map_cnbv.csv.")

    ingest_id = con.execute("""INSERT INTO ingest_log
        (source,archivo,ruta_crudo,sha256,bytes,publication_date,status)
        VALUES (?,?,?,?,?,?,'en_proceso') RETURNING ingest_id""",
        [FUENTE, ruta.name, str(ruta.relative_to(RAIZ)), sha, len(bytes_), publicado]).fetchone()[0]

    # ---- snapshots (una version por entidad y mes; la anterior queda superseded) ----
    ent = df[["entity_id", "as_of_date"]].drop_duplicates()
    con.execute("CREATE OR REPLACE TEMP TABLE nuevos(entity_id VARCHAR, as_of_date DATE)")
    con.executemany("INSERT INTO nuevos VALUES (?,?)", ent.values.tolist())
    con.execute("""UPDATE snapshots s SET superseded_at=now() WHERE s.source=? AND s.superseded_at IS NULL
        AND EXISTS (SELECT 1 FROM nuevos n WHERE n.entity_id=s.entity_id AND n.as_of_date=s.as_of_date)""", [FUENTE])
    con.execute("""INSERT INTO snapshots (source,entity_id,as_of_date,publication_date,ingest_id,version)
        SELECT ?, n.entity_id, n.as_of_date, ?, ?,
               1 + coalesce((SELECT max(version) FROM snapshots s WHERE s.entity_id=n.entity_id AND s.as_of_date=n.as_of_date),0)
        FROM nuevos n""", [FUENTE, publicado, ingest_id])

    # ---- holdings ----
    con.register("stage", df[["entity_id", "as_of_date", "instrument_id", "dl_tipo_inversion",
                              "dat_cantidad_titulos_operados", "dat_valor_razonable_unitario",
                              "dat_valor_razonable_total", "RowNumber", "asset_class", "subclase", "mapped_by",
                              "cve_tipo_valor", "dl_tipo_valor", "dat_emisora", "dat_serie",
                              "cve_pizarra", "dl_administradora", "cve_tipo_fondo"]])
    con.execute("""INSERT INTO holdings (snapshot_id,source,entity_id,as_of_date,publication_date,instrument_id,
            tipo_inversion,titulos_raw,valor_unitario_raw,valor_total_raw,moneda_raw,unidad_valor_raw,valor_total_mxn,fila_origen)
        SELECT s.snapshot_id, ?, t.entity_id, t.as_of_date, ?, t.instrument_id, t.dl_tipo_inversion,
               t.dat_cantidad_titulos_operados, t.dat_valor_razonable_unitario, t.dat_valor_razonable_total,
               NULL, 'MXN', t.dat_valor_razonable_total, t.RowNumber
        FROM stage t JOIN snapshots s ON s.entity_id=t.entity_id AND s.as_of_date=t.as_of_date
                                      AND s.ingest_id=? AND s.superseded_at IS NULL""", [FUENTE, publicado, ingest_id])

    # ---- dimensiones ----
    con.execute("""INSERT INTO instruments (instrument_id,source,tv,tipo_valor_desc,emisora,serie,primera_vez,ultima_vez)
        SELECT instrument_id, ?, any_value(cve_tipo_valor), any_value(dl_tipo_valor), any_value(dat_emisora),
               any_value(dat_serie), min(as_of_date), max(as_of_date)
        FROM stage WHERE instrument_id NOT IN (SELECT instrument_id FROM instruments) GROUP BY 1""", [FUENTE])
    con.execute("""UPDATE instruments i SET ultima_vez = greatest(i.ultima_vez, s.mx)
        FROM (SELECT instrument_id, max(as_of_date) mx FROM stage GROUP BY 1) s WHERE i.instrument_id=s.instrument_id""")
    con.execute("""INSERT INTO entities (entity_id,source,clave_nativa,nombre,entity_type,subtipo,operadora,vigencia_desde,vigencia_hasta)
        SELECT entity_id, ?, any_value(cve_pizarra), any_value(cve_pizarra), 'vehiculo', 'fondo_inversion_mx',
               any_value(dl_administradora), min(as_of_date), max(as_of_date)
        FROM stage WHERE entity_id NOT IN (SELECT entity_id FROM entities) GROUP BY 1""", [FUENTE])
    con.execute("""UPDATE entities e SET vigencia_hasta = greatest(e.vigencia_hasta, s.mx), vigencia_desde = least(e.vigencia_desde, s.mn)
        FROM (SELECT entity_id, max(as_of_date) mx, min(as_of_date) mn FROM stage GROUP BY 1) s WHERE e.entity_id=s.entity_id""")
    con.execute("""INSERT OR IGNORE INTO entity_taxonomy (entity_id,taxonomy_version,estrategia,estrategia_declarada_fuente,valid_from)
        SELECT entity_id, 'cnbv_tipo_fondo', any_value(cve_tipo_fondo), any_value(cve_tipo_fondo), min(as_of_date)
        FROM stage GROUP BY 1""")

    # ---- allocations (calculadas) ----
    con.execute("""INSERT INTO allocations (snapshot_id,source,entity_id,siefore,afore,as_of_date,publication_date,asset_class,peso,peso_raw,origen_peso)
        WITH base AS (
            SELECT t.entity_id, t.as_of_date, t.asset_class, t.dat_valor_razonable_total v,
                   t.dl_tipo_inversion <> ? AS en_denominador
            FROM stage t),
        den AS (SELECT entity_id, as_of_date, sum(v) d FROM base WHERE en_denominador GROUP BY 1,2),
        num AS (SELECT entity_id, as_of_date, asset_class, sum(v) n FROM base GROUP BY 1,2,3)
        SELECT s.snapshot_id, ?, num.entity_id, NULL, NULL, num.as_of_date, ?, num.asset_class,
               CASE WHEN den.d > 0 THEN 100.0*num.n/den.d END, CAST(num.n AS VARCHAR), 'calculado'
        FROM num JOIN den USING (entity_id, as_of_date)
        JOIN snapshots s ON s.entity_id=num.entity_id AND s.as_of_date=num.as_of_date AND s.ingest_id=? AND s.superseded_at IS NULL""",
        [DERIV, FUENTE, publicado, ingest_id])

    # ---- calidad ----
    cal = []
    nd = df[df.dl_tipo_inversion != DERIV]
    err = (nd.dat_cantidad_titulos_operados * nd.dat_valor_razonable_unitario - nd.dat_valor_razonable_total).abs()
    tol = (0.005 * nd.dat_valor_razonable_total.abs()).clip(lower=100)   # 0.5% o 100 pesos, lo que sea mayor
    for _, r in nd[err > tol].iterrows():
        cal.append((ingest_id, FUENTE, "titulos_x_unitario", "alerta", r.entity_id, r.as_of_date,
                    f"{r.cve_tipo_valor} {r.dat_emisora} {r.dat_serie}: {r.dat_cantidad_titulos_operados}x{r.dat_valor_razonable_unitario} vs {r.dat_valor_razonable_total}", None))
    for _, r in nd[nd.dat_valor_razonable_total < 0].iterrows():
        cal.append((ingest_id, FUENTE, "valor_negativo_no_derivado", "alerta", r.entity_id, r.as_of_date,
                    f"{r.dl_tipo_inversion}: {r.cve_tipo_valor} {r.dat_emisora}", r.dat_valor_razonable_total))
    pend = df[df.mapped_by == "juicio_pendiente"]
    for (tv, em, ac), g in pend.groupby(["cve_tipo_valor", "dat_emisora", "asset_class"]):
        cal.append((ingest_id, FUENTE, "clase_por_default", "info", None, None,
                    f"TV {tv} emisora {em} -> {ac} (regla pendiente de revision)", g.dat_valor_razonable_total.sum()))
    for _, r in df[df.dl_tipo_valor.isna()].drop_duplicates("cve_tipo_valor").iterrows():
        cal.append((ingest_id, FUENTE, "tv_sin_descripcion", "info", None, None, f"TV {r.cve_tipo_valor}", None))
    k = ["as_of_date", "entity_id", "cve_tipo_valor", "dat_emisora", "dat_serie"]
    directo = set(map(tuple, df[df.dl_tipo_inversion == "Inversión en Directo"][k].values))
    for tipo, g in df[~df.dl_tipo_inversion.isin(["Inversión en Directo", DERIV])].groupby("dl_tipo_inversion"):
        n = sum(tuple(r) in directo for r in g[k].values)
        cal.append((ingest_id, FUENTE, "solapamiento_clave_con_directo", "info", None, None,
                    f"{tipo}: {n} de {len(g)} posiciones coinciden en (fondo,TV,emisora,serie) con una de directo. Importes distintos = posiciones distintas, no doble conteo.", n))
    if cal:
        con.executemany("INSERT INTO calidad_log (ingest_id,source,regla,severidad,entity_id,as_of_date,detalle,valor) VALUES (?,?,?,?,?,?,?,?)", cal)

    con.execute("UPDATE ingest_log SET status='ok', filas_leidas=?, filas_cargadas=? WHERE ingest_id=?", [len(df), len(df), ingest_id])
    print(f"\n{ruta.name}  (sha256 {sha[:12]})")
    print(f"  holdings: {len(df):,}  periodos: {df.cve_periodo.min()}-{df.cve_periodo.max()}  fondos: {df.cve_pizarra.nunique()}  operadoras: {df.dl_administradora.nunique()}")
    print(f"  calidad : " + ", ".join(f"{r}={n}" for r, n in pd.Series([c[2] for c in cal]).value_counts().items()))
    return len(df)


def exportar(con):
    con.execute("""INSERT OR REPLACE INTO sources VALUES
      ('CNBV','CNBV Portafolio de Informacion, R03 J-0311 Cartera de inversion (fondos de inversion, "R7")','mensual',60,
       'holdings','% del valor de la cartera reportada excluyendo derivados (Directo + Reporto + Garantias + Prestamo de valores)',
       'Sujeto a reenvios de las entidades. No incluye pasivos ni activo neto; no comparable con el denominador de CONSAR.')""")
    con.execute("""INSERT OR REPLACE INTO parametros VALUES
      ('cnbv.denominador','directo+reporto+garantias+prestamo',
       'Todo lo reportado en R7 excepto la valuacion de derivados (P&L firmado, no una tenencia). Prestamo de valores incluido: el fondo sigue siendo dueño economico del papel; sus importes no coinciden con los del directo, asi que no duplica.',
       ?, 'hallazgos-r7-universo-completo.md, 2026-09-08')""", [dt.date.today()])
    con.execute(f"COPY (SELECT * FROM holdings WHERE source='CNBV') TO '{DATOS/'holdings_cnbv.parquet'}' (FORMAT PARQUET)")
    con.execute(f"""COPY (SELECT a.*, e.operadora, e.nombre AS fondo FROM allocations a JOIN entities e USING (entity_id)
                     WHERE a.source='CNBV') TO '{DATOS/'allocations_cnbv.parquet'}' (FORMAT PARQUET)""")

    # -------- validaciones --------
    print("\n-- composicion agregada del sistema, ultimo mes (%, ponderado por valor) --")
    print(con.execute("""WITH h AS (SELECT * FROM holdings WHERE source='CNBV' AND as_of_date=(SELECT max(as_of_date) FROM holdings WHERE source='CNBV')),
        c AS (SELECT h.*, coalesce(m2.asset_class, m1.asset_class) ac FROM h
              JOIN instruments i USING (instrument_id)
              LEFT JOIN asset_class_map m1 ON m1.source='CNBV' AND m1.tv_pattern=i.tv AND m1.emisora_pattern='*'
              LEFT JOIN asset_class_map m2 ON m2.source='CNBV' AND m2.tv_pattern=i.tv AND m2.emisora_pattern=i.emisora)
        SELECT ac, round(100*sum(valor_total_mxn)/(SELECT sum(valor_total_mxn) FROM c WHERE tipo_inversion<>?),2) pct
        FROM c GROUP BY 1 ORDER BY 2 DESC""", [DERIV]).df().to_string(index=False))
    print("\n-- fondos por operadora y mes --")
    print(con.execute("""SELECT e.operadora, count(DISTINCT a.entity_id) fondos, min(a.as_of_date) desde, max(a.as_of_date) hasta
        FROM allocations a JOIN entities e USING(entity_id) WHERE a.source='CNBV' GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    print("\n-- log de calidad CNBV --")
    print(con.execute("SELECT regla, severidad, count(*) n, round(sum(valor)/1e6) mdp FROM calidad_log WHERE source='CNBV' GROUP BY 1,2 ORDER BY 1").df().to_string(index=False))

    # -------- JSON para la pagina --------
    fechas = [r[0].isoformat() for r in con.execute("SELECT DISTINCT as_of_date FROM allocations WHERE source='CNBV' ORDER BY 1").fetchall()]
    idx = {f: i for i, f in enumerate(fechas)}
    ents = con.execute("""SELECT e.entity_id, e.nombre, e.operadora, t.estrategia_declarada_fuente
        FROM entities e LEFT JOIN entity_taxonomy t ON t.entity_id=e.entity_id AND t.taxonomy_version='cnbv_tipo_fondo'
        WHERE e.source='CNBV' ORDER BY e.operadora, e.nombre""").fetchall()
    clases = ORDEN_CLASES + FUERA_COMPOSICION
    fondos = {}
    for eid, nombre, op, tipo in ents:
        fondos[nombre] = {"operadora": op, "tipo": tipo, "series": {c: [None]*len(fechas) for c in clases}}
    nom = {eid: nombre for eid, nombre, _, _ in ents}
    for eid, f, c, p in con.execute("SELECT entity_id, as_of_date, asset_class, peso FROM allocations WHERE source='CNBV'").fetchall():
        if c in clases:
            fondos[nom[eid]]["series"][c][idx[f.isoformat()]] = round(p, 3) if p is not None else None
    # Un fondo con snapshot en un mes y sin posiciones en una clase tiene 0 en esa
    # clase por construccion (el denominador existe). None queda solo para los meses
    # en que el fondo no reporto.
    for f in fondos.values():
        ser = f["series"]
        for i in range(len(fechas)):
            if any(ser[c][i] is not None for c in clases):
                for c in clases:
                    if ser[c][i] is None:
                        ser[c][i] = 0.0
    operadoras = sorted({e[2] for e in ents})

    # -------- detalle del ultimo mes de cada fondo, por instrumento --------
    # Para el "universo" de cada fondo en la pagina: posiciones del ultimo mes en que
    # reporto, agregadas por (tipo de valor, emisora, tipo de inversion). Compacto:
    # [idx_tv, emisora, idx_tipo_inv, valor_mxn, idx_clase]. Los derivados van con
    # su valor firmado y clase 'Derivados'; el total excluye derivados (denominador).
    det_rows = con.execute("""
        WITH ult AS (SELECT entity_id, max(as_of_date) f FROM holdings WHERE source='CNBV' GROUP BY 1),
        h AS (SELECT h.entity_id, h.as_of_date, h.tipo_inversion, h.valor_total_mxn,
                     i.tv, i.tipo_valor_desc, i.emisora, i.serie
              FROM holdings h JOIN ult ON ult.entity_id=h.entity_id AND ult.f=h.as_of_date
              JOIN instruments i USING (instrument_id) WHERE h.source='CNBV'),
        c AS (SELECT h.*, coalesce(m2.asset_class, m1.asset_class) ac FROM h
              LEFT JOIN asset_class_map m1 ON m1.source='CNBV' AND m1.tv_pattern=h.tv AND m1.emisora_pattern='*'
              LEFT JOIN asset_class_map m2 ON m2.source='CNBV' AND m2.tv_pattern=h.tv AND m2.emisora_pattern=h.emisora)
        SELECT entity_id, as_of_date, tv, any_value(tipo_valor_desc), emisora, serie, tipo_inversion, ac,
               sum(valor_total_mxn)
        FROM c GROUP BY entity_id, as_of_date, tv, emisora, serie, tipo_inversion, ac
        ORDER BY entity_id, 9 DESC""").fetchall()
    tvs, tv_idx, tipos, tipo_idx = [], {}, [], {}
    clases_det = ORDEN_CLASES + FUERA_COMPOSICION
    detalle = {}
    for eid, f, tv, desc, em, se, ti, ac, v in det_rows:
        if tv not in tv_idx:
            tv_idx[tv] = len(tvs); tvs.append([tv, desc or ""])
        if ti not in tipo_idx:
            tipo_idx[ti] = len(tipos); tipos.append(ti)
        d = detalle.setdefault(nom[eid], {"mes": f.isoformat(), "total": 0.0, "pos": [],
                                          "_pz": 0.0, "_pzv": 0.0})
        if ti != DERIV:
            d["total"] += v
        ven = vencimiento(tv, se)
        if ven is not None and ti != DERIV and v > 0:
            d["_pz"] += v * ((ven - f).days / 365.25)
            d["_pzv"] += v
        d["pos"].append([tv_idx[tv], nombre_completo(em, se), tipo_idx[ti], round(v),
                         clases_det.index(ac) if ac in clases_det else -1])
    for d in detalle.values():
        d["total"] = round(d["total"])
        # Plazo promedio al vencimiento, ponderado por valor. Solo sobre el papel
        # cuya serie es una fecha; se declara que fraccion de la cartera cubre.
        if d["_pzv"] > 0 and d["total"] > 0:
            d["plazo"] = round(d["_pz"] / d["_pzv"], 2)
            d["plazo_cob"] = round(100 * d["_pzv"] / d["total"], 1)
        d.pop("_pz"); d.pop("_pzv")
    import json
    (DATOS / "pagina_fondos.json").write_text(json.dumps({
        "fuente": "CNBV · Portafolio de Información · R03 J-0311 Cartera de inversión (fondos de inversión)",
        "denominador": "% de la cartera reportada, excluyendo la valuación de derivados (Directo + Reporto + Garantías + Préstamo de valores)",
        "actualizado": dt.date.today().isoformat(),
        "reglas_catalogo": con.execute("SELECT count(*) FROM asset_class_map WHERE source='CNBV'").fetchone()[0],
        "fechas": fechas, "clases": ORDEN_CLASES, "fuera_composicion": FUERA_COMPOSICION,
        "operadoras": operadoras, "fondos": fondos,
        "tv": tvs, "tipos_inversion": tipos, "detalle": detalle}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"\nParquet y pagina_fondos.json escritos en datos/  ({len(fondos)} fondos, {len(fechas)} meses)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("crudos", nargs="*", help="Parquet(s) de extraer_r7.py; por defecto todos los crudos/r7_*.parquet")
    ap.add_argument("--publicado", type=dt.date.fromisoformat, default=None, help="Fecha de publicacion del .xlsm en el portal (AAAA-MM-DD)")
    a = ap.parse_args()
    rutas = [Path(p) for p in a.crudos] or sorted(CRUDOS.glob("r7_*.parquet"))
    if not rutas:
        sys.exit("No hay crudos/r7_*.parquet. Corre primero src/extraer_r7.py sobre el .xlsm.")
    DATOS.mkdir(exist_ok=True)
    con = duckdb.connect(str(DATOS / "carteras.duckdb"))
    con.execute((RAIZ / "src" / "esquema.sql").read_text("utf-8"))
    for r in rutas:
        cargar_uno(con, r.resolve(), a.publicado)
    exportar(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
