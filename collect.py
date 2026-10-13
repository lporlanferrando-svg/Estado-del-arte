#!/usr/bin/env python3
"""
Recoge cuántos artículos hay por año para cada combinación de palabras clave
en varias plataformas, guarda un histórico (snapshots) y detecta artículos nuevos.

Uso:
    python collect.py            # ejecución real (usa las API keys del entorno)
    python collect.py --demo     # genera datos inventados para probar la web
    python collect.py --solo openalex ieee

Variables de entorno:
    OPENALEX_API_KEY   obligatoria para OpenAlex (gratis en openalex.org/settings/api)
    IEEE_API_KEY       obligatoria para IEEE Xplore (gratis en developer.ieee.org)
    S2_API_KEY         opcional para Semantic Scholar
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import os
import random
import sys
import time
from pathlib import Path

import requests
import yaml

RAIZ = Path(__file__).resolve().parent
CONFIG = RAIZ / "config.yaml"
DIR_DATOS = RAIZ / "data"
HISTORICO = DIR_DATOS / "historico.csv"
VISTOS = DIR_DATOS / "vistos.json"
NOVEDADES = DIR_DATOS / "novedades.json"
SALIDA = RAIZ / "docs" / "data" / "resultados.json"

SESION = requests.Session()
SESION.headers["User-Agent"] = "estado-del-arte-bot/1.0"


# ---------------------------------------------------------------- utilidades
def pedir(url: str, params: dict, headers: dict | None = None, intentos: int = 5) -> dict:
    """GET con reintentos ante límites de peticiones (429) o errores del servidor."""
    ultimo = None
    for i in range(intentos):
        r = SESION.get(url, params=params, headers=headers, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            ultimo = r
            time.sleep(2 ** i * 2)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{url} devolvió {ultimo.status_code} tras {intentos} intentos")


def normalizar_id(doi: str | None, alternativo: str | None) -> str:
    if doi:
        return doi.lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
    return (alternativo or "").strip()


# ---------------------------------------------------------------- consultas
def generar_consultas(cfg: dict) -> list[list[list[str]]]:
    """Devuelve una lista de combinaciones; cada combinación es una lista de grupos de sinónimos."""
    terminos = [t if isinstance(t, list) else [t] for t in cfg["palabras_clave"]]
    cmin = max(1, cfg["combinaciones"].get("tamano_min", 1))
    cmax = min(len(terminos), cfg["combinaciones"].get("tamano_max", len(terminos)))
    return [list(c) for r in range(cmin, cmax + 1) for c in itertools.combinations(terminos, r)]


def etiqueta(combo) -> str:
    return " + ".join(g[0] for g in combo)


def booleana(combo, y: str = "AND", o: str = "OR") -> str:
    partes = []
    for grupo in combo:
        frases = [f'"{t}"' for t in grupo]
        partes.append(frases[0] if len(frases) == 1 else "(" + f" {o} ".join(frases) + ")")
    return f" {y} ".join(partes)


# ---------------------------------------------------------------- plataformas
class OpenAlex:
    nombre = "OpenAlex"
    URL = "https://api.openalex.org/works"

    def __init__(self, clave: str):
        self.clave = clave

    def _params(self, combo, desde, hasta, **extra):
        # Busca en título + resumen. Ojo: dentro de 'filter' las comas separan filtros,
        # así que los términos no pueden llevar comas.
        p = {
            "filter": f"title_and_abstract.search:{booleana(combo)},publication_year:{desde}-{hasta}",
            "api_key": self.clave,
        }
        p.update(extra)
        return p

    def conteos(self, combo, desde, hasta) -> dict[int, int]:
        d = pedir(self.URL, self._params(combo, desde, hasta, group_by="publication_year"))
        return {int(g["key"]): int(g["count"]) for g in d.get("group_by", [])}

    def recientes(self, combo, desde, hasta, n):
        d = pedir(self.URL, self._params(
            combo, desde, hasta, sort="publication_date:desc", per_page=n,
            select="id,doi,title,publication_date,primary_location"))
        for w in d.get("results", []):
            fuente = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
            yield {
                "id": normalizar_id(w.get("doi"), w.get("id")),
                "titulo": w.get("title"),
                "fecha_pub": w.get("publication_date"),
                "fuente": fuente,
                "url": w.get("doi") or w.get("id"),
            }


class IEEE:
    nombre = "IEEE Xplore"
    URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"

    def __init__(self, clave: str):
        self.clave = clave

    def conteos(self, combo, desde, hasta) -> dict[int, int]:
        # La API no agrupa por año: una llamada por año (cuidado con el límite diario).
        res = {}
        for anio in range(desde, hasta + 1):
            d = pedir(self.URL, {"querytext": f"({booleana(combo)})", "start_year": anio,
                                 "end_year": anio, "max_records": 1, "apikey": self.clave})
            res[anio] = int(d.get("total_records", 0))
            time.sleep(0.3)
        return res

    def recientes(self, combo, desde, hasta, n):
        d = pedir(self.URL, {"querytext": f"({booleana(combo)})", "start_year": desde,
                             "end_year": hasta, "max_records": n, "sort_field": "publication_year",
                             "sort_order": "desc", "apikey": self.clave})
        for a in d.get("articles", []):
            yield {
                "id": normalizar_id(a.get("doi"), f"ieee:{a.get('article_number')}"),
                "titulo": a.get("title"),
                "fecha_pub": a.get("publication_date") or str(a.get("publication_year", "")),
                "fuente": a.get("publication_title"),
                "url": a.get("html_url") or (f"https://doi.org/{a['doi']}" if a.get("doi") else None),
            }


class SemanticScholar:
    nombre = "Semantic Scholar"
    URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"

    def __init__(self, clave: str | None):
        self.headers = {"x-api-key": clave} if clave else None
        self.pausa = 1.1 if clave else 3.0

    def _q(self, combo):
        return booleana(combo, y="+", o="|")

    def conteos(self, combo, desde, hasta) -> dict[int, int]:
        res = {}
        for anio in range(desde, hasta + 1):
            d = pedir(self.URL, {"query": self._q(combo), "year": str(anio), "fields": "title"},
                      headers=self.headers)
            res[anio] = int(d.get("total", 0))  # S2 indica que es un total aproximado
            time.sleep(self.pausa)
        return res

    def recientes(self, combo, desde, hasta, n):
        d = pedir(self.URL, {"query": self._q(combo), "year": f"{hasta - 1}-{hasta}",
                             "sort": "publicationDate:desc",
                             "fields": "title,externalIds,publicationDate,venue,url"},
                  headers=self.headers)
        time.sleep(self.pausa)
        for p in (d.get("data") or [])[:n]:
            doi = (p.get("externalIds") or {}).get("DOI")
            yield {
                "id": normalizar_id(doi, f"s2:{p.get('paperId')}"),
                "titulo": p.get("title"),
                "fecha_pub": p.get("publicationDate"),
                "fuente": p.get("venue") or None,
                "url": f"https://doi.org/{doi}" if doi else p.get("url"),
            }


def preparar_plataformas(cfg: dict, solo: list[str] | None) -> tuple[list, list[str]]:
    activas, avisos = [], []
    pedidas = cfg.get("plataformas", {})

    def quiere(nombre):
        return pedidas.get(nombre) and (not solo or nombre in solo)

    if quiere("openalex"):
        k = os.getenv("OPENALEX_API_KEY")
        activas.append(OpenAlex(k)) if k else avisos.append("OpenAlex omitido: falta OPENALEX_API_KEY")
    if quiere("ieee"):
        k = os.getenv("IEEE_API_KEY")
        activas.append(IEEE(k)) if k else avisos.append("IEEE Xplore omitido: falta IEEE_API_KEY")
    if quiere("semantic_scholar"):
        activas.append(SemanticScholar(os.getenv("S2_API_KEY")))
    return activas, avisos


# ---------------------------------------------------------------- histórico
def guardar_historico(hoy: str, conteos: dict):
    filas = []
    if HISTORICO.exists():
        with HISTORICO.open(newline="", encoding="utf-8") as f:
            filas = [r for r in csv.DictReader(f) if r["fecha"] != hoy]  # re-ejecución el mismo día
    for plat, por_consulta in conteos.items():
        for consulta, por_anio in por_consulta.items():
            for anio, n in sorted(por_anio.items()):
                filas.append({"fecha": hoy, "plataforma": plat, "consulta": consulta,
                              "anio": anio, "n": n})
    HISTORICO.parent.mkdir(parents=True, exist_ok=True)
    with HISTORICO.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fecha", "plataforma", "consulta", "anio", "n"])
        w.writeheader()
        w.writerows(filas)
    return filas


def evolucion_desde_historico(filas) -> dict:
    """Total del periodo en cada snapshot, por plataforma y consulta."""
    acum: dict = {}
    for r in filas:
        d = acum.setdefault(r["plataforma"], {}).setdefault(r["consulta"], {})
        d[r["fecha"]] = d.get(r["fecha"], 0) + int(r["n"])
    return {p: {c: [{"fecha": f, "total": t} for f, t in sorted(v.items())] for c, v in cs.items()}
            for p, cs in acum.items()}


# ---------------------------------------------------------------- modo demo
def datos_demo(cfg, consultas, desde, hasta):
    random.seed(7)
    plataformas = ["OpenAlex", "IEEE Xplore", "Semantic Scholar"]
    escala = {"OpenAlex": 1.0, "IEEE Xplore": 0.35, "Semantic Scholar": 0.8}
    conteos, evolucion = {}, {}
    hoy = dt.date.today()
    for p in plataformas:
        conteos[p], evolucion[p] = {}, {}
        for combo in consultas:
            et = etiqueta(combo)
            base = 4000 / (len(combo) ** 2.2) * escala[p] * random.uniform(0.6, 1.4)
            crec = random.uniform(1.12, 1.35)
            serie = {}
            for a in range(desde, hasta + 1):
                v = base * crec ** (a - desde) * random.uniform(0.9, 1.1)
                if a == hasta:
                    v *= hoy.timetuple().tm_yday / 365 * 0.85  # año en curso incompleto
                serie[a] = int(v)
            conteos[p][et] = serie
            total = sum(serie.values())
            evolucion[p][et] = [
                {"fecha": (hoy - dt.timedelta(weeks=w)).isoformat(), "total": int(total * (1 - w * 0.006))}
                for w in range(11, -1, -1)]
    novedades = []
    for i in range(18):
        combo = random.choice(consultas)
        novedades.append({
            "id": f"10.0000/demo.{i}", "titulo": f"Artículo de ejemplo {i + 1} sobre {etiqueta(combo)}",
            "fecha_pub": (hoy - dt.timedelta(days=random.randint(1, 60))).isoformat(),
            "fuente": random.choice(["IEEE Access", "Sensors", "Journal of Manufacturing Systems"]),
            "url": None, "plataforma": random.choice(plataformas), "consultas": [etiqueta(combo)],
            "detectado": (hoy - dt.timedelta(weeks=i // 4)).isoformat()})
    return conteos, evolucion, novedades


# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="genera datos inventados para probar la web")
    ap.add_argument("--solo", nargs="*", help="openalex ieee semantic_scholar")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    hoy = dt.date.today()
    desde = int(cfg["periodo"]["desde"])
    hasta = hoy.year if str(cfg["periodo"]["hasta"]).lower() == "actual" else int(cfg["periodo"]["hasta"])
    consultas = generar_consultas(cfg)
    errores: list[str] = []

    if args.demo:
        conteos, evolucion, novedades = datos_demo(cfg, consultas, desde, hasta)
        nombres = list(conteos)
    else:
        plataformas, avisos = preparar_plataformas(cfg, args.solo)
        errores += avisos
        nombres = [p.nombre for p in plataformas]
        conteos = {p.nombre: {} for p in plataformas}

        primera_vez = not VISTOS.exists()
        vistos: dict = json.loads(VISTOS.read_text()) if VISTOS.exists() else {}
        novedades: list = json.loads(NOVEDADES.read_text()) if NOVEDADES.exists() else []
        por_id = {n["id"]: n for n in novedades}
        n_rec = int(cfg.get("novedades", {}).get("por_consulta", 25))

        for plat in plataformas:
            for combo in consultas:
                et = etiqueta(combo)
                print(f"[{plat.nombre}] {et} ...", flush=True)
                try:
                    conteos[plat.nombre][et] = plat.conteos(combo, desde, hasta)
                except Exception as e:  # una plataforma caída no debe parar el resto
                    errores.append(f"{plat.nombre} · {et} · conteos: {e}")
                    continue
                try:
                    for art in plat.recientes(combo, desde, hasta, n_rec):
                        if not art["id"] or not art["titulo"]:
                            continue
                        if art["id"] in vistos:
                            if art["id"] in por_id and et not in por_id[art["id"]]["consultas"]:
                                por_id[art["id"]]["consultas"].append(et)
                            continue
                        vistos[art["id"]] = hoy.isoformat()
                        if not primera_vez:  # en la primera ejecución todo es "nuevo": no lo marcamos
                            nuevo = {**art, "plataforma": plat.nombre, "consultas": [et],
                                     "detectado": hoy.isoformat()}
                            novedades.append(nuevo)
                            por_id[art["id"]] = nuevo
                except Exception as e:
                    errores.append(f"{plat.nombre} · {et} · novedades: {e}")

        novedades.sort(key=lambda n: (n["detectado"], n.get("fecha_pub") or ""), reverse=True)
        novedades = novedades[: int(cfg.get("novedades", {}).get("max_mostradas", 300))]
        DIR_DATOS.mkdir(parents=True, exist_ok=True)
        VISTOS.write_text(json.dumps(vistos, ensure_ascii=False, indent=0), encoding="utf-8")
        NOVEDADES.write_text(json.dumps(novedades, ensure_ascii=False, indent=1), encoding="utf-8")
        filas = guardar_historico(hoy.isoformat(), {k: v for k, v in conteos.items() if v})
        evolucion = evolucion_desde_historico(filas)

    salida = {
        "proyecto": cfg.get("proyecto", "Estado del arte"),
        "generado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "demo": bool(args.demo),
        "periodo": {"desde": desde, "hasta": hasta, "anio_en_curso": hoy.year},
        "plataformas": [n for n in nombres if conteos.get(n)],
        "consultas": [{"etiqueta": etiqueta(c), "terminos": c, "booleana": booleana(c)} for c in consultas],
        "conteos": {p: {c: {str(a): n for a, n in s.items()} for c, s in cs.items()}
                    for p, cs in conteos.items() if cs},
        "evolucion": evolucion,
        "novedades": novedades,
        "errores": errores,
    }
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Escrito {SALIDA.relative_to(RAIZ)} · {len(salida['plataformas'])} plataformas · "
          f"{len(consultas)} consultas · {len(errores)} avisos/errores")
    for e in errores:
        print("  ⚠", e, file=sys.stderr)
    # Si alguna plataforma ha fallado, salimos con error para que GitHub te avise por correo
    sys.exit(1 if any("omitido" not in e for e in errores) else 0)


if __name__ == "__main__":
    main()
