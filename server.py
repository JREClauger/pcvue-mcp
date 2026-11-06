"""Connecteur MCP pour PcVue, via le serveur OPC DA de PcVue (SV.OPCDAServer).

Outils exposés :
  - pcvue_statut          : état du serveur, nombre de variables / alarmes
  - pcvue_parcourir       : arborescence des variables (dossier par dossier)
  - pcvue_chercher        : recherche de variables par nom
  - pcvue_lire            : valeurs temps réel (+ description, unité… en option)
  - pcvue_echantillonner  : relevé d'une ou plusieurs variables sur une courte durée
  - pcvue_alarmes         : alarmes actives (et masquées en option)
  - pcvue_ecrire          : écriture d'une consigne (liste blanche + bornes + journal)

Le connecteur doit tourner avec le MÊME niveau de droits que PcVue
(les deux en administrateur, ou aucun des deux).

Usage :
  py server.py            -> serveur MCP (stdio)
  py server.py --test     -> test de connexion en ligne de commande
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

import opcda

logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "progid": "SV.OPCDAServer",
    "process_image": "sv32.exe",
    "taille_lot": 1000,
    "cache_minutes": 10,
    "ecriture_active": False,
    "variables_ecriture": {},
    "journal_ecriture": "journal_ecritures.jsonl",
}

# Codage AlarmState observé sur PcVue 16 (cohérent avec OnAlm / OnNoAckAlm)
ALM_ACTIVE, ALM_ACK, ALM_MASKED = 0x001, 0x200, 0x400
DETAIL_PROPS = ("Description", "Units", "Domain", "Nature", "Format")


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


# --------------------------------------------------------------------------
# Accès PcVue (exécuté dans le thread COM dédié)
# --------------------------------------------------------------------------

_worker = opcda.OpcDaWorker()
_cache: dict[str, Any] = {"t": 0.0, "variables": [], "alarmes": [], "items": set()}


def _client(cfg) -> opcda.OpcDaClient:
    return opcda.OpcDaClient(cfg["progid"], cfg["process_image"])


def _refresh_cache(c: opcda.OpcDaClient, cfg, force: bool = False) -> dict[str, Any]:
    if force or time.time() - _cache["t"] > cfg["cache_minutes"] * 60 or not _cache["items"]:
        items = c.all_items()
        variables = sorted(i[: -len(".Nature")] for i in items if i.endswith(".Nature"))
        _cache.update(
            t=time.time(),
            items=set(items),
            variables=variables or sorted(items),
            alarmes=sorted(i[: -len(".AlarmState")] for i in items if i.endswith(".AlarmState")),
        )
    return _cache


def _read_batched(c: opcda.OpcDaClient, cfg, item_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    n = cfg["taille_lot"]
    for k in range(0, len(item_ids), n):
        out.update(c.read(item_ids[k:k + n]))
    return out


async def _run(fn, *args):
    """Exécute fn(client, cfg, *args) dans le thread COM, connexion ouverte."""
    cfg = load_config()

    def job():
        with _client(cfg) as c:
            return fn(c, cfg, *args)

    try:
        return await asyncio.wrap_future(_worker.submit(job))
    except Exception as e:
        return {"erreur": f"{type(e).__name__}: {e}"}


def _decode_alarm(state: Any) -> dict[str, Any]:
    s = int(state) if isinstance(state, (int, float)) else 0
    return {"active": bool(s & ALM_ACTIVE), "acquittee": bool(s & ALM_ACK),
            "masquee": bool(s & ALM_MASKED), "code_etat": s}


# --------------------------------------------------------------------------
# Serveur MCP
# --------------------------------------------------------------------------

mcp = MCPServer("pcvue", instructions=(
    "Connecteur temps réel PcVue (supervision industrielle). Les noms de variables "
    "sont hiérarchiques avec des points (ex. 'Batiment1.Zone2.Temperature'). "
    "Utiliser pcvue_chercher pour trouver une variable avant de la lire. "
    "pcvue_ecrire agit sur le process réel : ne l'appeler que sur demande explicite."))
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_statut() -> dict[str, Any]:
    """État du serveur PcVue, nombre de variables et d'alarmes, compteurs
    d'alarmes actives / non acquittées, et configuration d'écriture."""
    def fn(c, cfg):
        st = c.status()
        cache = _refresh_cache(c, cfg)
        cpt = c.read(["OnAlm", "OnNoAckAlm"])
        return {
            "connecte": True, **st,
            "nb_variables": len(cache["variables"]), "nb_alarmes_definies": len(cache["alarmes"]),
            "alarmes_actives": cpt.get("OnAlm", {}).get("valeur"),
            "alarmes_non_acquittees": cpt.get("OnNoAckAlm", {}).get("valeur"),
            "ecriture_active": cfg["ecriture_active"],
            "variables_ecriture": list(cfg["variables_ecriture"]),
        }
    return await _run(fn)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_parcourir(chemin: str = "") -> dict[str, Any]:
    """Liste les sous-dossiers et variables d'un dossier de l'arborescence PcVue.
    `chemin` est un nom de dossier avec des points (ex. 'Batiment1.Zone2'), vide = racine."""
    path = [p for p in chemin.split(".") if p]
    return await _run(lambda c, cfg: c.browse(path))


@mcp.tool(annotations=READ_ONLY)
async def pcvue_chercher(motif: str, limite: int = 50, alarmes_seulement: bool = False) -> dict[str, Any]:
    """Cherche des variables dont le nom contient tous les mots de `motif`
    (insensible à la casse, mots séparés par des espaces)."""
    mots = [m.lower() for m in motif.split() if m]

    def fn(c, cfg):
        cache = _refresh_cache(c, cfg)
        base = cache["alarmes"] if alarmes_seulement else cache["variables"]
        res = [v for v in base if all(m in v.lower() for m in mots)]
        return {"motif": motif, "nombre_total": len(res), "resultats": res[:limite],
                "tronque": len(res) > limite}
    return await _run(fn)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_lire(variables: list[str], avec_details: bool = False) -> dict[str, Any]:
    """Lit la valeur temps réel, la qualité et l'horodatage de variables PcVue.
    Avec `avec_details`, ajoute description, unité, domaine, nature et, pour une
    alarme, son état et son niveau."""
    def fn(c, cfg):
        res = _read_batched(c, cfg, variables)
        if not avec_details:
            return res
        cache = _refresh_cache(c, cfg)
        props = [f"{v}.{p}" for v in variables for p in DETAIL_PROPS + ("AlarmState", "AlarmLevel")
                 if f"{v}.{p}" in cache["items"]]
        det = _read_batched(c, cfg, props)
        for v in variables:
            if "erreur" in res.get(v, {}):
                continue
            for p in DETAIL_PROPS + ("AlarmLevel",):
                val = det.get(f"{v}.{p}", {}).get("valeur")
                if val not in (None, ""):
                    res[v][p.lower()] = val
            if f"{v}.AlarmState" in det:
                res[v]["alarme"] = _decode_alarm(det[f"{v}.AlarmState"].get("valeur"))
        return res
    return await _run(fn)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_echantillonner(variables: list[str], duree_s: int = 60, periode_s: float = 5) -> dict[str, Any]:
    """Relève des variables à intervalle régulier pendant `duree_s` secondes
    (max 600) et renvoie les points et statistiques (min, max, moyenne).
    Utile pour observer une tendance courte en l'absence d'historique."""
    duree_s = max(1, min(int(duree_s), 600))
    periode_s = max(0.5, float(periode_s))

    def fn(c, cfg):
        series: dict[str, list] = {v: [] for v in variables}
        t_end = time.time() + duree_s
        while True:
            r = c.read(variables)
            t = datetime.now().astimezone().isoformat(timespec="seconds")
            for v in variables:
                series[v].append((t, r.get(v, {}).get("valeur")))
            if time.time() + periode_s > t_end:
                break
            time.sleep(periode_s)
        out = {}
        for v, pts in series.items():
            nums = [p[1] for p in pts if isinstance(p[1], (int, float)) and not isinstance(p[1], bool)]
            out[v] = {"points": pts, "statistiques": {
                "min": min(nums), "max": max(nums), "moyenne": round(statistics.fmean(nums), 4),
                "premiere": nums[0], "derniere": nums[-1]} if nums else None}
        return out
    return await _run(fn)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_alarmes(inclure_masquees: bool = False, filtre: str = "") -> dict[str, Any]:
    """Liste les alarmes PcVue actives (et masquées si demandé), triées par niveau,
    avec description et état d'acquittement. `filtre` restreint aux noms contenant ce texte."""
    def fn(c, cfg):
        cache = _refresh_cache(c, cfg)
        noms = [a for a in cache["alarmes"] if filtre.lower() in a.lower()]
        etats = _read_batched(c, cfg, [f"{a}.AlarmState" for a in noms])
        retenues = []
        for a in noms:
            d = _decode_alarm(etats.get(f"{a}.AlarmState", {}).get("valeur"))
            if d["active"] or (inclure_masquees and d["masquee"]):
                retenues.append((a, d))
        props = [f"{a}.{p}" for a, _ in retenues for p in ("AlarmLevel", "Description", "Domain", "Nature")]
        det = _read_batched(c, cfg, props + [a for a, _ in retenues])
        alarmes = []
        for a, d in retenues:
            g = lambda p: det.get(f"{a}.{p}", {}).get("valeur")
            alarmes.append({"variable": a, **d, "niveau": g("AlarmLevel"),
                            "description": g("Description") or None, "domaine": g("Domain") or None,
                            "nature": g("Nature") or None, "valeur": det.get(a, {}).get("valeur"),
                            "depuis": det.get(a, {}).get("horodatage")})
        alarmes.sort(key=lambda x: (-(x["niveau"] or 0), x["variable"]))
        return {"nombre": len(alarmes),
                "actives": sum(a["active"] for a in alarmes),
                "non_acquittees": sum(a["active"] and not a["acquittee"] for a in alarmes),
                "alarmes": alarmes}
    return await _run(fn)


def _journaliser(cfg: dict[str, Any], entree: dict[str, Any]) -> None:
    with (BASE_DIR / cfg["journal_ecriture"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entree, ensure_ascii=False, default=str) + "\n")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=True, openWorldHint=True))
async def pcvue_ecrire(variable: str, valeur: float | int | bool | str, motif: str) -> dict[str, Any]:
    """ÉCRIT une valeur sur le process via PcVue. Action réelle sur l'installation :
    ne l'utiliser qu'à la demande explicite de l'utilisateur, après confirmation.
    Seules les variables de la liste blanche (config.json) sont modifiables, dans
    leurs bornes min/max. Chaque tentative est journalisée avec son `motif`."""
    cfg = load_config()
    entree = {"date": datetime.now().astimezone().isoformat(timespec="seconds"),
              "variable": variable, "valeur_demandee": valeur, "motif": motif}
    regles = cfg["variables_ecriture"].get(variable)
    refus = None
    if not cfg["ecriture_active"]:
        refus = "L'écriture est désactivée (ecriture_active = false dans config.json)."
    elif regles is None:
        refus = f"'{variable}' n'est pas dans la liste blanche variables_ecriture."
    elif isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
        if "min" in regles and valeur < regles["min"]:
            refus = f"Valeur {valeur} inférieure au minimum autorisé {regles['min']}."
        elif "max" in regles and valeur > regles["max"]:
            refus = f"Valeur {valeur} supérieure au maximum autorisé {regles['max']}."
    if refus:
        entree["resultat"] = f"REFUS {refus}"
        _journaliser(cfg, entree)
        return {"refuse": refus, "autorisees": list(cfg["variables_ecriture"])}

    def fn(c, cfg):
        avant = c.read([variable])[variable]
        if "erreur" in avant:
            raise opcda.OpcError(avant["erreur"])
        c.write(variable, valeur)
        apres = c.read([variable])[variable]
        return {"ok": True, "variable": variable, "avant": avant, "apres": apres}

    res = await _run(fn)
    if res.get("ok"):
        entree.update(valeur_avant=res["avant"]["valeur"], valeur_apres=res["apres"]["valeur"], resultat="OK")
    else:
        entree["resultat"] = f"ECHEC {res.get('erreur')}"
    _journaliser(cfg, entree)
    return res


# --------------------------------------------------------------------------

async def _cli_test() -> None:
    print(json.dumps(await pcvue_statut(), ensure_ascii=False, indent=1, default=str))
    print(json.dumps(await pcvue_alarmes(), ensure_ascii=False, indent=1, default=str)[:3000])


if __name__ == "__main__":
    if "--test" in sys.argv:
        asyncio.run(_cli_test())
    else:
        mcp.run()
