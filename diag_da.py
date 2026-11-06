"""Diagnostic OPC DA PcVue : statut, arborescence, lecture des premières variables.
À lancer en administrateur (même niveau de droits que PcVue). Lecture seule."""
import ctypes, json, sys, traceback

import opcda

out = {"admin": bool(ctypes.windll.shell32.IsUserAnAdmin())}
ctypes.windll.ole32.CoInitializeEx(None, 2)
try:
    with opcda.OpcDaClient() as c:
        out["statut"] = c.status()
        out["racine"] = c.browse([])
        items = c.all_items()
        out["nb_variables"] = len(items)
        out["exemples"] = items[:40]
        if items:
            out["lecture"] = c.read(items[:10])
except Exception:
    out["erreur"] = traceback.format_exc()

text = json.dumps(out, ensure_ascii=False, indent=1, default=str)
if len(sys.argv) > 1:
    open(sys.argv[1], "w", encoding="utf-8").write(text)
print(text)
