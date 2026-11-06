# Connecteur MCP PcVue

Permet à Claude de lire les variables et les alarmes de PcVue en temps réel, et
d'écrire des consignes autorisées, via le **serveur OPC DA intégré à PcVue**
(`SV.OPCDAServer`). Aucun mapping ni licence supplémentaire : toutes les
variables du projet chargé sont visibles.

Testé avec PcVue 16.3 et Python 3.14 (64 bits).

## Outils

| Outil | Rôle |
|---|---|
| `pcvue_statut` | État du serveur, nombre de variables et d'alarmes, compteurs `OnAlm` / `OnNoAckAlm` |
| `pcvue_parcourir` | Arborescence, dossier par dossier (`Batiment1.Zone2`) |
| `pcvue_chercher` | Recherche par mots (`zone2 temp`) |
| `pcvue_lire` | Valeur, qualité, horodatage ; avec `avec_details` : unité, description, état d'alarme |
| `pcvue_echantillonner` | Relevé périodique sur une courte durée (max 10 min) + statistiques |
| `pcvue_alarmes` | Alarmes actives (et masquées en option), triées par niveau |
| `pcvue_ecrire` | Écrit une consigne (liste blanche, bornes, journal) |

## Fichiers

- `server.py` : connecteur MCP (OPC DA)
- `opcda.py` : client OPC DA en Python pur (ctypes, sans dépendance)
- `config.json` : configuration (écriture, taille des lots, cache)
- `diag_da.py` : diagnostic en lecture seule
- `server_opcua.py` / `config_opcua.json` : variante OPC UA, si l'option serveur OPC UA de PcVue est un jour disponible

## Installation

```
py -m pip install --user mcp
py server.py --test
```

Déclaration dans Claude :

```json
{ "mcpServers": { "pcvue": { "command": "py", "args": ["C:\\chemin\\vers\\pcvue-mcp\\server.py"] } } }
```

## Niveau de droits

Le connecteur doit tourner avec **le même niveau de droits que PcVue** : les
deux en administrateur, ou aucun des deux. Sinon Windows refuse la connexion
(erreur `0x80080005`). Par sécurité, le connecteur refuse de se connecter si
`sv32.exe` n'est pas lancé, pour éviter que Windows démarre PcVue tout seul.

## Alarmes

L'état est lu dans la propriété `AlarmState` de chaque variable d'alarme.
Codage observé sur PcVue 16 (cohérent avec les compteurs `OnAlm` / `OnNoAckAlm`) :

| Bit | Signification |
|---|---|
| `0x001` | Alarme active |
| `0x200` | Acquittée |
| `0x400` | Masquée |

À confirmer avec la documentation PcVue lors de la première alarme non acquittée.

## Historique

L'historique n'est pas accessible en OPC DA. PcVue l'expose via des serveurs
propriétaires (`SV.HisSrv`, `SV.HDS`) ou une base SQL : piste à étudier.
En attendant, `pcvue_echantillonner` permet d'observer une tendance courte.

## Sécurité de l'écriture

- Désactivée par défaut (`"ecriture_active": false`).
- Seules les variables listées dans `variables_ecriture` sont modifiables,
  dans leurs bornes `min` / `max`.
- Chaque tentative (réussie, refusée ou en échec) est tracée dans
  `journal_ecritures.jsonl` avec la valeur avant / après et le motif.
- L'outil est marqué « destructif » : Claude demande confirmation avant chaque écriture.

Tester d'abord sur un projet de test, jamais directement sur un site en production.
