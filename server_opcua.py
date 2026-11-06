"""Connecteur MCP pour PcVue (via le serveur OPC UA de PcVue).

Outils exposés :
  - pcvue_statut            : teste la connexion et décrit le serveur
  - pcvue_parcourir         : liste les enfants d'un nœud de l'arborescence
  - pcvue_chercher          : cherche des variables par nom
  - pcvue_lire              : lit une ou plusieurs variables (temps réel)
  - pcvue_historique        : lit l'historique d'une variable + statistiques
  - pcvue_alarmes           : liste les alarmes actives / non acquittées
  - pcvue_ecrire            : écrit une consigne (liste blanche + journal)

Usage :
  python server.py            -> démarre le serveur MCP (stdio)
  python server.py --test     -> test de connexion en ligne de commande
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from asyncua import Client, ua
from mcp.types import ToolAnnotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

# stdout est réservé au protocole MCP : tout le log part sur stderr.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pcvue-mcp")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config_opcua.json"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "endpoint": "opc.tcp://localhost:4840",
    "security": None,               # ex. "Basic256Sha256,SignAndEncrypt,cert.der,key.pem"
    "username": None,
    "password": None,
    "timeout_s": 10,
    "namespace_uri": None,          # prioritaire sur namespace_index si renseigné
    "namespace_index": 2,
    "node_id_template": "ns={ns};s={name}",
    "browse_root": None,            # NodeId de départ pour la recherche (défaut : Objects)
    "max_browse_nodes": 5000,
    "ecriture_active": False,
    "variables_ecriture": {},       # {"Nom.Variable": {"min": 0, "max": 100}}
    "journal_ecriture": "journal_ecritures.jsonl",
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


# --------------------------------------------------------------------------
# Helpers OPC UA
# --------------------------------------------------------------------------

class PcVue:
    """Connexion courte à PcVue, ouverte pour la durée d'un appel d'outil."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.client = Client(url=cfg["endpoint"], timeout=cfg["timeout_s"])
        self._ns: int | None = None

    async def __aenter__(self) -> "PcVue":
        if self.cfg.get("username"):
            self.client.set_user(self.cfg["username"])
            self.client.set_password(self.cfg.get("password") or "")
        if self.cfg.get("security"):
            await self.client.set_security_string(self.cfg["security"])
        await self.client.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            await self.client.disconnect()
        except Exception:  # la déconnexion ne doit jamais masquer l'erreur d'origine
            pass

    async def ns(self) -> int:
        if self._ns is None:
            uri = self.cfg.get("namespace_uri")
            self._ns = (await self.client.get_namespace_index(uri)) if uri else int(self.cfg["namespace_index"])
        return self._ns

    async def node(self, name: str):
        """Accepte un NodeId complet (ns=2;s=...) ou un nom de variable PcVue."""
        if name.startswith(("ns=", "i=", "s=", "g=", "b=")):
            return self.client.get_node(name)
        return self.client.get_node(self.cfg["node_id_template"].format(ns=await self.ns(), name=name))


def to_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.astimezone().isoformat(timespec="seconds")
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, ua.LocalizedText):
        return value.Text
    if isinstance(value, ua.QualifiedName):
        return value.Name
    if isinstance(value, ua.NodeId):
        return value.to_string()
    if isinstance(value, ua.StatusCode):
        return value.name
    return str(value)


def datavalue_to_dict(dv: ua.DataValue) -> dict[str, Any]:
    return {
        "valeur": to_json(dv.Value.Value if dv.Value else None),
        "qualite": dv.StatusCode.name if dv.StatusCode else "Good",
        "horodatage": to_json(dv.SourceTimestamp or dv.ServerTimestamp),
    }


def parse_date(text: str | None, default: datetime) -> datetime:
    if not text:
        return default
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:          # heure locale du poste si non précisé
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def error(e: Exception) -> dict[str, Any]:
    return {"erreur": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# Serveur MCP
# --------------------------------------------------------------------------

mcp = MCPServer("pcvue")
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_statut() -> dict[str, Any]:
    """Teste la connexion au serveur OPC UA de PcVue et renvoie l'état du serveur,
    les espaces de noms et la configuration d'écriture."""
    cfg = load_config()
    try:
        async with PcVue(cfg) as pv:
            c = pv.client
            state = await c.get_node(ua.ObjectIds.Server_ServerStatus_State).read_value()
            build = await c.get_node(ua.ObjectIds.Server_ServerStatus_BuildInfo).read_value()
            return {
                "connecte": True,
                "endpoint": cfg["endpoint"],
                "etat_serveur": ua.ServerState(state).name,
                "produit": f"{build.ManufacturerName} {build.ProductName} {build.SoftwareVersion}",
                "espaces_de_noms": await c.get_namespace_array(),
                "namespace_utilise": await pv.ns(),
                "ecriture_active": cfg["ecriture_active"],
                "variables_ecriture": list(cfg["variables_ecriture"]),
            }
    except Exception as e:
        return {"connecte": False, "endpoint": cfg["endpoint"], **error(e)}


@mcp.tool(annotations=READ_ONLY)
async def pcvue_parcourir(node_id: str | None = None) -> dict[str, Any]:
    """Liste les enfants directs d'un nœud OPC UA (par défaut le dossier Objects).
    Permet d'explorer l'arborescence des variables PcVue pas à pas."""
    cfg = load_config()
    try:
        async with PcVue(cfg) as pv:
            parent = pv.client.get_node(node_id) if node_id else pv.client.nodes.objects
            enfants = []
            for child in await parent.get_children():
                enfants.append({
                    "node_id": child.nodeid.to_string(),
                    "nom": (await child.read_browse_name()).Name,
                    "classe": (await child.read_node_class()).name,
                })
            return {"parent": parent.nodeid.to_string(), "enfants": enfants}
    except Exception as e:
        return error(e)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_chercher(motif: str, limite: int = 100) -> dict[str, Any]:
    """Cherche les variables dont le nom ou le NodeId contient `motif`
    (insensible à la casse). Renvoie leur NodeId à utiliser avec les autres outils."""
    cfg = load_config()
    motif_l = motif.lower()
    try:
        async with PcVue(cfg) as pv:
            root = pv.client.get_node(cfg["browse_root"]) if cfg.get("browse_root") else pv.client.nodes.objects
            trouves, visites, file = [], 0, [root]
            while file and len(trouves) < limite and visites < cfg["max_browse_nodes"]:
                node = file.pop(0)
                visites += 1
                for child in await node.get_children():
                    nclass = await child.read_node_class()
                    if nclass == ua.NodeClass.Variable:
                        name = (await child.read_browse_name()).Name
                        nid = child.nodeid.to_string()
                        if motif_l in name.lower() or motif_l in nid.lower():
                            trouves.append({"nom": name, "node_id": nid})
                    elif nclass == ua.NodeClass.Object:
                        file.append(child)
            return {"motif": motif, "resultats": trouves[:limite],
                    "noeuds_explores": visites, "exploration_complete": not file}
    except Exception as e:
        return error(e)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_lire(variables: list[str]) -> dict[str, Any]:
    """Lit la valeur temps réel, la qualité et l'horodatage d'une ou plusieurs
    variables PcVue (nom PcVue ou NodeId complet)."""
    cfg = load_config()
    try:
        async with PcVue(cfg) as pv:
            nodes = [await pv.node(v) for v in variables]
            resultats = {}
            for name, node in zip(variables, nodes):
                try:
                    resultats[name] = datavalue_to_dict(await node.read_data_value())
                except Exception as e:
                    resultats[name] = error(e)
            return resultats
    except Exception as e:
        return error(e)


@mcp.tool(annotations=READ_ONLY)
async def pcvue_historique(variable: str, debut: str | None = None, fin: str | None = None,
                           max_valeurs: int = 1000) -> dict[str, Any]:
    """Lit l'historique brut d'une variable entre `debut` et `fin` (dates ISO,
    heure locale si non précisée ; par défaut la dernière heure) et calcule
    des statistiques (min, max, moyenne) pour les valeurs numériques."""
    cfg = load_config()
    now = datetime.now(timezone.utc)
    try:
        t_fin = parse_date(fin, now)
        t_debut = parse_date(debut, t_fin - timedelta(hours=1))
        async with PcVue(cfg) as pv:
            node = await pv.node(variable)
            dvs = await node.read_raw_history(t_debut, t_fin, numvalues=max_valeurs)
            points = [datavalue_to_dict(dv) for dv in dvs]
            nums = [p["valeur"] for p in points
                    if isinstance(p["valeur"], (int, float)) and not isinstance(p["valeur"], bool)]
            stats = None
            if nums:
                stats = {"nombre": len(nums), "min": min(nums), "max": max(nums),
                         "moyenne": round(statistics.fmean(nums), 4)}
            return {"variable": variable, "debut": to_json(t_debut), "fin": to_json(t_fin),
                    "nombre_points": len(points), "tronque": len(points) >= max_valeurs,
                    "statistiques": stats, "points": points}
    except Exception as e:
        return error(e)


class _AlarmCollector:
    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def event_notification(self, event):
        self.events.append(event.get_event_props_as_fields_dict())


def _field(ev: dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in ev:
            v = ev[n]
            return v.Value if isinstance(v, ua.Variant) else v
    return None


@mcp.tool(annotations=READ_ONLY)
async def pcvue_alarmes(inclure_inactives_non_acquittees: bool = True) -> dict[str, Any]:
    """Liste les alarmes présentes dans PcVue (actives, et par défaut aussi les
    alarmes retombées mais non acquittées), via OPC UA Alarms & Conditions."""
    cfg = load_config()
    try:
        async with PcVue(cfg) as pv:
            c = pv.client
            collector = _AlarmCollector()
            sub = await c.create_subscription(250, collector)
            await sub.subscribe_events(c.nodes.server, c.get_node(ua.ObjectIds.AlarmConditionType))
            # ConditionRefresh : le serveur renvoie l'état de toutes les conditions retenues
            await c.get_node(ua.ObjectIds.ConditionType).call_method(
                ua.NodeId(ua.ObjectIds.ConditionType_ConditionRefresh),
                ua.Variant(sub.subscription_id, ua.VariantType.UInt32))
            await asyncio.sleep(2)
            await sub.delete()

            alarmes = {}
            for ev in collector.events:
                cond_id = _field(ev, "ConditionId", "NodeId")
                actif = _field(ev, "ActiveState/Id")
                acquitte = _field(ev, "AckedState/Id")
                retain = _field(ev, "Retain")
                if retain is False:
                    continue
                if not actif and (acquitte or not inclure_inactives_non_acquittees):
                    continue
                key = to_json(cond_id) or f"{_field(ev, 'SourceName')}/{_field(ev, 'ConditionName')}"
                alarmes[key] = {
                    "source": to_json(_field(ev, "SourceName")),
                    "alarme": to_json(_field(ev, "ConditionName")),
                    "message": to_json(_field(ev, "Message")),
                    "severite": to_json(_field(ev, "Severity")),
                    "active": to_json(actif),
                    "acquittee": to_json(acquitte),
                    "horodatage": to_json(_field(ev, "Time")),
                }
            liste = sorted(alarmes.values(), key=lambda a: a["severite"] or 0, reverse=True)
            return {"nombre": len(liste), "alarmes": liste}
    except Exception as e:
        return {**error(e), "note": "Le serveur OPC UA de PcVue doit exposer les alarmes "
                                    "(Alarms & Conditions) pour que cet outil fonctionne."}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=True, openWorldHint=True))
async def pcvue_ecrire(variable: str, valeur: float | int | bool | str, motif: str) -> dict[str, Any]:
    """ÉCRIT une valeur sur le process via PcVue. Action réelle sur l'installation :
    ne l'utiliser qu'à la demande explicite de l'utilisateur, après confirmation.
    Seules les variables de la liste blanche (config.json) sont modifiables, dans
    leurs bornes min/max. Chaque écriture est journalisée avec son `motif`."""
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

    try:
        async with PcVue(cfg) as pv:
            node = await pv.node(variable)
            avant = datavalue_to_dict(await node.read_data_value())
            vtype = await node.read_data_type_as_variant_type()
            await node.write_value(ua.DataValue(ua.Variant(valeur, vtype)))
            apres = datavalue_to_dict(await node.read_data_value())
            entree.update(valeur_avant=avant["valeur"], valeur_apres=apres["valeur"], resultat="OK")
            return {"ok": True, "variable": variable, "avant": avant, "apres": apres}
    except Exception as e:
        entree.update(resultat=f"ECHEC {type(e).__name__}: {e}")
        return error(e)
    finally:
        _journaliser(cfg, entree)


def _journaliser(cfg: dict[str, Any], entree: dict[str, Any]) -> None:
    with (BASE_DIR / cfg["journal_ecriture"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entree, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------------

async def _cli_test() -> None:
    print(json.dumps(await pcvue_statut(), ensure_ascii=False, indent=2, default=str))
    print(json.dumps(await pcvue_parcourir(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    if "--test" in sys.argv:
        asyncio.run(_cli_test())
    else:
        mcp.run()
