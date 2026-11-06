"""Client OPC DA 2.0 minimal (ctypes pur, sans dépendance) pour le serveur PcVue.

Toutes les méthodes sont synchrones et doivent être appelées depuis un même
thread initialisé COM (voir `OpcDaWorker`).
"""

from __future__ import annotations

import ctypes as C
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

ole32 = C.windll.ole32
oleaut32 = C.windll.oleaut32
ole32.CoTaskMemFree.argtypes = [C.c_void_p]
oleaut32.SysAllocString.restype = C.c_void_p
oleaut32.SysAllocString.argtypes = [C.c_wchar_p]

CLSCTX_LOCAL_SERVER = 0x4
OPC_DS_DEVICE = 2
OPC_BRANCH, OPC_LEAF, OPC_FLAT = 1, 2, 3
OPC_BROWSE_DOWN, OPC_BROWSE_TO = 2, 3
SERVER_STATES = {1: "RUNNING", 2: "FAILED", 3: "NOCONFIG", 4: "SUSPENDED", 5: "TEST", 6: "COMM_FAULT"}


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------

class GUID(C.Structure):
    _fields_ = [("Data1", C.c_ulong), ("Data2", C.c_ushort), ("Data3", C.c_ushort), ("Data4", C.c_ubyte * 8)]


def guid(text: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(C.c_wchar_p(text), C.byref(g))
    return g


IID_IUnknown = guid("{00000000-0000-0000-C000-000000000046}")
IID_IOPCServer = guid("{39C13A4D-011E-11D0-9675-0020AFD8ADB3}")
IID_IOPCBrowseServerAddressSpace = guid("{39C13A4F-011E-11D0-9675-0020AFD8ADB3}")
IID_IOPCItemMgt = guid("{39C13A54-011E-11D0-9675-0020AFD8ADB3}")
IID_IOPCSyncIO = guid("{39C13A52-011E-11D0-9675-0020AFD8ADB3}")


class FILETIME(C.Structure):
    _fields_ = [("low", C.c_uint32), ("high", C.c_uint32)]

    def to_datetime(self) -> datetime | None:
        ticks = (self.high << 32) | self.low
        if not ticks:
            return None
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)


class _VU(C.Union):
    _fields_ = [("llVal", C.c_longlong), ("lVal", C.c_long), ("iVal", C.c_short), ("bVal", C.c_ubyte),
                ("fltVal", C.c_float), ("dblVal", C.c_double), ("boolVal", C.c_short),
                ("bstrVal", C.c_void_p), ("_pad", C.c_void_p * 2)]


class VARIANT(C.Structure):
    _fields_ = [("vt", C.c_ushort), ("r1", C.c_ushort), ("r2", C.c_ushort), ("r3", C.c_ushort), ("u", _VU)]


class OPCSERVERSTATUS(C.Structure):
    _fields_ = [("ftStartTime", FILETIME), ("ftCurrentTime", FILETIME), ("ftLastUpdateTime", FILETIME),
                ("dwServerState", C.c_int), ("dwGroupCount", C.c_uint32), ("dwBandWidth", C.c_uint32),
                ("wMajorVersion", C.c_ushort), ("wMinorVersion", C.c_ushort), ("wBuildNumber", C.c_ushort),
                ("wReserved", C.c_ushort), ("szVendorInfo", C.c_void_p)]


class OPCITEMDEF(C.Structure):
    _fields_ = [("szAccessPath", C.c_wchar_p), ("szItemID", C.c_wchar_p), ("bActive", C.c_int),
                ("hClient", C.c_uint32), ("dwBlobSize", C.c_uint32), ("pBlob", C.c_void_p),
                ("vtRequestedDataType", C.c_ushort), ("wReserved", C.c_ushort)]


class OPCITEMRESULT(C.Structure):
    _fields_ = [("hServer", C.c_uint32), ("vtCanonicalDataType", C.c_ushort), ("wReserved", C.c_ushort),
                ("dwAccessRights", C.c_uint32), ("dwBlobSize", C.c_uint32), ("pBlob", C.c_void_p)]


class OPCITEMSTATE(C.Structure):
    _fields_ = [("hClient", C.c_uint32), ("ftTimeStamp", FILETIME), ("wQuality", C.c_ushort),
                ("wReserved", C.c_ushort), ("vDataValue", VARIANT)]


# --------------------------------------------------------------------------
# Appels COM par vtable
# --------------------------------------------------------------------------

class OpcError(Exception):
    pass


def _hr(code: int) -> str:
    return f"0x{code & 0xFFFFFFFF:08X}"


class ComPtr:
    """Pointeur d'interface COM brut, appelé par index de vtable."""

    def __init__(self, ptr: int):
        self.ptr = ptr

    def call(self, index: int, argtypes: list, *args) -> int:
        vtbl = C.cast(self.ptr, C.POINTER(C.POINTER(C.c_void_p)))[0]
        fn = C.WINFUNCTYPE(C.c_long, C.c_void_p, *argtypes)(vtbl[index])
        hr = fn(self.ptr, *args)
        if hr < 0:
            raise OpcError(f"Appel COM #{index} en échec ({_hr(hr)})")
        return hr

    def query(self, iid: GUID) -> "ComPtr":
        out = C.c_void_p()
        self.call(0, [C.POINTER(GUID), C.POINTER(C.c_void_p)], C.byref(iid), C.byref(out))
        return ComPtr(out.value)

    def release(self) -> None:
        if self.ptr:
            vtbl = C.cast(self.ptr, C.POINTER(C.POINTER(C.c_void_p)))[0]
            C.WINFUNCTYPE(C.c_ulong, C.c_void_p)(vtbl[2])(self.ptr)
            self.ptr = None


# --------------------------------------------------------------------------
# VARIANT <-> Python
# --------------------------------------------------------------------------

_INT_VT = {2, 3, 16, 17, 18, 19, 20, 21, 22, 23}
_FLOAT_VT = {4, 5, 6, 14}
VT_NAMES = {0: "EMPTY", 2: "I2", 3: "I4", 4: "R4", 5: "R8", 7: "DATE", 8: "BSTR", 11: "BOOL",
            16: "I1", 17: "UI1", 18: "UI2", 19: "UI4", 20: "I8", 21: "UI8", 22: "INT", 23: "UINT"}


def _change(src: VARIANT, vt: int) -> VARIANT:
    dst = VARIANT()
    # LCID en-US : point décimal quelle que soit la langue de Windows
    hr = oleaut32.VariantChangeTypeEx(C.byref(dst), C.byref(src), 0x0409, 0, vt)
    if hr < 0:
        raise OpcError(f"Conversion VARIANT vers {VT_NAMES.get(vt, vt)} impossible ({_hr(hr)})")
    return dst


_ELEM_CTYPE = {2: C.c_short, 3: C.c_long, 4: C.c_float, 5: C.c_double, 7: C.c_double, 11: C.c_short,
               16: C.c_byte, 17: C.c_ubyte, 18: C.c_ushort, 19: C.c_ulong, 20: C.c_longlong,
               21: C.c_ulonglong, 22: C.c_int, 23: C.c_uint}


def _safearray_to_list(psa: int, elem_vt: int) -> list[Any]:
    """Décode un SAFEARRAY à une dimension en liste Python."""
    lo, hi = C.c_long(), C.c_long()
    oleaut32.SafeArrayGetLBound(C.c_void_p(psa), 1, C.byref(lo))
    oleaut32.SafeArrayGetUBound(C.c_void_p(psa), 1, C.byref(hi))
    out = []
    for i in range(lo.value, hi.value + 1):
        idx = C.c_long(i)
        if elem_vt == 12:      # VARIANT
            el = VARIANT()
            oleaut32.SafeArrayGetElement(C.c_void_p(psa), C.byref(idx), C.byref(el))
            out.append(variant_to_py(el))
            oleaut32.VariantClear(C.byref(el))
        elif elem_vt == 8:     # BSTR
            b = C.c_void_p()
            oleaut32.SafeArrayGetElement(C.c_void_p(psa), C.byref(idx), C.byref(b))
            out.append(C.wstring_at(b.value) if b.value else "")
            oleaut32.SysFreeString(b)
        elif elem_vt in _ELEM_CTYPE:
            el = _ELEM_CTYPE[elem_vt]()
            oleaut32.SafeArrayGetElement(C.c_void_p(psa), C.byref(idx), C.byref(el))
            out.append(el.value != 0 if elem_vt == 11 else el.value)
        else:
            out.append(f"<type {elem_vt}>")
    return out


def variant_to_py(v: VARIANT) -> Any:
    try:
        return _variant_to_py(v)
    except Exception as e:  # une valeur exotique ne doit pas faire échouer toute la lecture
        return f"<valeur non décodable vt={v.vt}: {e}>"


def _variant_to_py(v: VARIANT) -> Any:
    vt = v.vt
    if vt & 0x2000:            # VT_ARRAY
        if vt & 0x4000 or not v.u.bstrVal:  # VT_BYREF non géré / tableau vide
            return []
        return _safearray_to_list(v.u.bstrVal, vt & 0x0FFF)
    if vt in (0, 1):
        return None
    if vt == 11:
        return v.u.boolVal != 0
    if vt == 8:
        return C.wstring_at(v.u.bstrVal) if v.u.bstrVal else ""
    if vt in _INT_VT or vt in _FLOAT_VT:
        d = _change(v, 5).u.dblVal
        return int(d) if vt in _INT_VT else d
    if vt == 7:
        return (datetime(1899, 12, 30) + timedelta(days=v.u.dblVal)).isoformat(timespec="seconds")
    s = _change(v, 8)
    try:
        return C.wstring_at(s.u.bstrVal) if s.u.bstrVal else ""
    finally:
        oleaut32.VariantClear(C.byref(s))


def py_to_variant(value: Any, canonical_vt: int) -> VARIANT:
    src = VARIANT()
    if isinstance(value, bool):
        src.vt, src.u.boolVal = 11, -1 if value else 0
    elif isinstance(value, int):
        src.vt, src.u.llVal = 20, value
    elif isinstance(value, float):
        src.vt, src.u.dblVal = 5, value
    else:
        src.vt, src.u.bstrVal = 8, oleaut32.SysAllocString(str(value))
    if canonical_vt in (0, src.vt):
        return src
    try:
        return _change(src, canonical_vt)
    finally:
        oleaut32.VariantClear(C.byref(src))


def quality_name(q: int) -> str:
    return {0xC0: "Good", 0x40: "Uncertain", 0x00: "Bad"}.get(q & 0xC0, f"0x{q:02X}")


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

def process_running(image: str) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    return image.lower() in out.lower()


class OpcDaClient:
    def __init__(self, progid: str = "SV.OPCDAServer", process_image: str = "sv32.exe"):
        self.progid = progid
        self.process_image = process_image
        self.server: ComPtr | None = None

    # -- connexion -----------------------------------------------------------
    def connect(self) -> None:
        # Sécurité : ne jamais laisser COM démarrer PcVue tout seul.
        if self.process_image and not process_running(self.process_image):
            raise OpcError(f"PcVue ({self.process_image}) n'est pas lancé : connexion annulée.")
        clsid = GUID()
        hr = ole32.CLSIDFromProgID(C.c_wchar_p(self.progid), C.byref(clsid))
        if hr < 0:
            raise OpcError(f"ProgID {self.progid} introuvable ({_hr(hr)})")
        out = C.c_void_p()
        hr = ole32.CoCreateInstance(C.byref(clsid), None, CLSCTX_LOCAL_SERVER,
                                    C.byref(IID_IOPCServer), C.byref(out))
        if hr < 0:
            raise OpcError(f"Connexion à {self.progid} impossible ({_hr(hr)}). "
                           "Le connecteur et PcVue doivent tourner avec le même niveau de droits.")
        self.server = ComPtr(out.value)

    def close(self) -> None:
        if self.server:
            self.server.release()
            self.server = None

    def __enter__(self) -> "OpcDaClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- statut --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        p = C.POINTER(OPCSERVERSTATUS)()
        self.server.call(6, [C.POINTER(C.POINTER(OPCSERVERSTATUS))], C.byref(p))
        s = p.contents
        try:
            return {
                "etat": SERVER_STATES.get(s.dwServerState, s.dwServerState),
                "fournisseur": C.wstring_at(s.szVendorInfo) if s.szVendorInfo else None,
                "version": f"{s.wMajorVersion}.{s.wMinorVersion}.{s.wBuildNumber}",
                "demarre_le": s.ftStartTime.to_datetime(),
                "heure_serveur": s.ftCurrentTime.to_datetime(),
                "groupes": s.dwGroupCount,
            }
        finally:
            if s.szVendorInfo:
                ole32.CoTaskMemFree(s.szVendorInfo)
            ole32.CoTaskMemFree(p)

    # -- navigation ----------------------------------------------------------
    def _browser(self) -> ComPtr:
        return self.server.query(IID_IOPCBrowseServerAddressSpace)

    @staticmethod
    def _enum(b: ComPtr, kind: int, filt: str = "") -> list[str]:
        enum = C.c_void_p()
        hr = b.call(5, [C.c_int, C.c_wchar_p, C.c_ushort, C.c_uint32, C.POINTER(C.c_void_p)],
                    kind, filt, 0, 0, C.byref(enum))
        if hr == 1 or not enum.value:  # S_FALSE : rien à énumérer
            return []
        e = ComPtr(enum.value)
        names = []
        try:
            buf = (C.c_void_p * 256)()
            while True:
                fetched = C.c_ulong()
                hr = e.call(3, [C.c_ulong, C.POINTER(C.c_void_p), C.POINTER(C.c_ulong)],
                            256, buf, C.byref(fetched))
                for i in range(fetched.value):
                    names.append(C.wstring_at(buf[i]))
                    ole32.CoTaskMemFree(buf[i])
                if hr != 0 or fetched.value == 0:
                    return names
        finally:
            e.release()

    @staticmethod
    def _item_id(b: ComPtr, name: str) -> str:
        out = C.c_void_p()
        b.call(6, [C.c_wchar_p, C.POINTER(C.c_void_p)], name, C.byref(out))
        try:
            return C.wstring_at(out.value)
        finally:
            ole32.CoTaskMemFree(out)

    def browse(self, path: list[str]) -> dict[str, Any]:
        b = self._browser()
        try:
            b.call(4, [C.c_int, C.c_wchar_p], OPC_BROWSE_TO, "")
            for seg in path:
                b.call(4, [C.c_int, C.c_wchar_p], OPC_BROWSE_DOWN, seg)
            branches = self._enum(b, OPC_BRANCH)
            leaves = [{"nom": n, "item_id": self._item_id(b, n)} for n in self._enum(b, OPC_LEAF)]
            return {"chemin": path, "dossiers": branches, "variables": leaves}
        finally:
            b.release()

    def all_items(self) -> list[str]:
        b = self._browser()
        try:
            return self._enum(b, OPC_FLAT)
        finally:
            b.release()

    # -- lecture / écriture --------------------------------------------------
    def _with_items(self, item_ids: list[str], fn):
        """Crée un groupe temporaire, y ajoute les items, appelle fn, puis nettoie."""
        h_group = C.c_uint32()
        revised = C.c_uint32()
        unk = C.c_void_p()
        self.server.call(3, [C.c_wchar_p, C.c_int, C.c_uint32, C.c_uint32, C.c_void_p, C.c_void_p,
                             C.c_uint32, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32),
                             C.POINTER(GUID), C.POINTER(C.c_void_p)],
                         "", 1, 1000, 1, None, None, 0, C.byref(h_group), C.byref(revised),
                         C.byref(IID_IOPCItemMgt), C.byref(unk))
        mgt = ComPtr(unk.value)
        sync = None
        try:
            sync = mgt.query(IID_IOPCSyncIO)
            n = len(item_ids)
            defs = (OPCITEMDEF * n)()
            for i, iid in enumerate(item_ids):
                defs[i] = OPCITEMDEF("", iid, 1, i, 0, None, 0, 0)
            p_res = C.POINTER(OPCITEMRESULT)()
            p_err = C.POINTER(C.c_long)()
            mgt.call(3, [C.c_uint32, C.POINTER(OPCITEMDEF), C.POINTER(C.POINTER(OPCITEMRESULT)),
                         C.POINTER(C.POINTER(C.c_long))],
                     n, defs, C.byref(p_res), C.byref(p_err))
            items = []
            for i in range(n):
                err = p_err[i]
                items.append({"item_id": item_ids[i], "erreur": _hr(err) if err < 0 else None,
                              "h": p_res[i].hServer, "vt": p_res[i].vtCanonicalDataType,
                              "droits": p_res[i].dwAccessRights})
            ole32.CoTaskMemFree(p_res)
            ole32.CoTaskMemFree(p_err)
            return fn(sync, items)
        finally:
            if sync:
                sync.release()
            mgt.release()
            try:
                self.server.call(7, [C.c_uint32, C.c_int], h_group.value, 1)
            except OpcError:
                pass

    def read(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        def do(sync: ComPtr, items):
            out = {it["item_id"]: {"erreur": f"Variable inconnue ou refusée ({it['erreur']})"}
                   for it in items if it["erreur"]}
            ok = [it for it in items if not it["erreur"]]
            if not ok:
                return out
            handles = (C.c_uint32 * len(ok))(*[it["h"] for it in ok])
            p_st = C.POINTER(OPCITEMSTATE)()
            p_err = C.POINTER(C.c_long)()
            sync.call(3, [C.c_int, C.c_uint32, C.POINTER(C.c_uint32), C.POINTER(C.POINTER(OPCITEMSTATE)),
                          C.POINTER(C.POINTER(C.c_long))],
                      OPC_DS_DEVICE, len(ok), handles, C.byref(p_st), C.byref(p_err))
            for i, it in enumerate(ok):
                st = p_st[i]
                if p_err[i] < 0:
                    out[it["item_id"]] = {"erreur": f"Lecture en échec ({_hr(p_err[i])})"}
                else:
                    ts = st.ftTimeStamp.to_datetime()
                    out[it["item_id"]] = {
                        "valeur": variant_to_py(st.vDataValue),
                        "qualite": quality_name(st.wQuality),
                        "horodatage": ts.astimezone().isoformat(timespec="seconds") if ts else None,
                        "type": VT_NAMES.get(it["vt"], it["vt"]),
                        "ecriture_possible": bool(it["droits"] & 2),
                    }
                oleaut32.VariantClear(C.byref(st.vDataValue))
            ole32.CoTaskMemFree(p_st)
            ole32.CoTaskMemFree(p_err)
            return out

        return self._with_items(item_ids, do)

    def write(self, item_id: str, value: Any) -> None:
        def do(sync: ComPtr, items):
            it = items[0]
            if it["erreur"]:
                raise OpcError(f"Variable inconnue ou refusée ({it['erreur']})")
            if not it["droits"] & 2:
                raise OpcError("Variable en lecture seule côté PcVue.")
            var = py_to_variant(value, it["vt"])
            handles = (C.c_uint32 * 1)(it["h"])
            p_err = C.POINTER(C.c_long)()
            try:
                sync.call(4, [C.c_uint32, C.POINTER(C.c_uint32), C.POINTER(VARIANT),
                              C.POINTER(C.POINTER(C.c_long))],
                          1, handles, C.byref(var), C.byref(p_err))
                err = p_err[0]
                ole32.CoTaskMemFree(p_err)
                if err < 0:
                    raise OpcError(f"Écriture refusée par PcVue ({_hr(err)})")
            finally:
                oleaut32.VariantClear(C.byref(var))

        self._with_items([item_id], do)


class OpcDaWorker:
    """Exécute les appels OPC DA dans un thread dédié initialisé COM."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opcda",
                                       initializer=lambda: ole32.CoInitializeEx(None, 2))

    def submit(self, fn, *args):
        return self.pool.submit(fn, *args)
