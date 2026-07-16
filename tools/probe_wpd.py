"""WPD/MTP probe for `pix import` spec validation (Windows-only; see spec/import.md).

Reproducible harness behind the "Validation results" in spec/import.md. Needs
`comtypes` (not pywin32 — it does not wrap WPD). Run without adding a runtime dep:

    uv run --with comtypes python tools/probe_wpd.py <mode> ...

or install the optional group:  uv sync --group probe

Modes:
  list                       enumerate portable devices (id, mfr, model, serial). [D1]
  report [--max N] [--read]  walk one device's camera roll, dump per-object props,
                             optionally stream the largest file. [W1,W3,W4,C1,C2,C3,S1,V1]
  snapshot OUT [--max N]     write JSON of sample objects (PUID + fallback key) for
                             a reconnect test. [prep for D1,I1]
  compare  OUT               re-enumerate and diff vs snapshot: serial + PUID stable? [D1,I1]
  w2 [--min-mb N]            serial vs concurrent read throughput; is the device single-lane? [W2]

Common flags: --device-index N (default 0).
"""

import sys
import json
import time
import threading
from ctypes import c_ulong, c_wchar_p, POINTER, cast

import comtypes
from comtypes.client import CreateObject, GetModule

GetModule("portabledeviceapi.dll")
GetModule("portabledevicetypes.dll")
from comtypes.gen import PortableDeviceApiLib as port      # type: ignore  # noqa: E402
from comtypes.gen import PortableDeviceTypesLib as types_   # type: ignore  # noqa: E402
from comtypes import GUID                                    # noqa: E402


# ---- PROPERTYKEYs -----------------------------------------------------------
def pk(fmtid: str, pid: int):
    k = port._tagpropertykey()
    k.fmtid = GUID(fmtid)
    k.pid = pid
    return k


OBJ = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"  # WPD_OBJECT_PROPERTIES_V1
WPD_OBJECT_NAME = pk(OBJ, 4)
WPD_OBJECT_PERSISTENT_UNIQUE_ID = pk(OBJ, 5)
WPD_OBJECT_FORMAT = pk(OBJ, 6)
WPD_OBJECT_CONTENT_TYPE = pk(OBJ, 7)
WPD_OBJECT_SIZE = pk(OBJ, 11)
WPD_OBJECT_ORIGINAL_FILE_NAME = pk(OBJ, 12)
WPD_OBJECT_DATE_CREATED = pk(OBJ, 18)
WPD_OBJECT_DATE_MODIFIED = pk(OBJ, 19)

DEV = "{26D4979A-E643-4626-9E2B-736DC0C92FDC}"  # WPD_DEVICE_PROPERTIES_V1
WPD_DEVICE_MANUFACTURER = pk(DEV, 7)
WPD_DEVICE_MODEL = pk(DEV, 8)
WPD_DEVICE_SERIAL_NUMBER = pk(DEV, 9)
WPD_DEVICE_FRIENDLY_NAME = pk(DEV, 12)

WPD_RESOURCE_DEFAULT = pk("{E81E79BE-34F0-41BF-B53F-F1A06AE87842}", 0)
STGM_READ = 0

CONTENT_TYPES = {
    "{27E2E392-A111-48E0-AB0C-E17705A05F85}": "FOLDER",
    "{99ED0160-17FF-4C44-9D98-1D7A6F941921}": "FUNCTIONAL",
    "{613CA327-AB93-4900-B4FA-895BB5874B79}": "IMAGE",
    "{9261B03C-3D78-4519-85E3-02C5E1F50BB9}": "VIDEO",
    "{4AD2C85E-5E2D-45E5-8864-4F229E3C6CF0}": "AUDIO",
}


# ---- device enumeration -----------------------------------------------------
def list_device_ids():
    mgr = CreateObject(port.PortableDeviceManager, interface=port.IPortableDeviceManager)
    mgr.RefreshDeviceList()
    # comtypes returns the (in,out) params as a list [pPnPDeviceIDs, count].
    _, n = mgr.GetDevices(POINTER(c_wchar_p)(), 0)
    n = int(n)
    if n == 0:
        return mgr, []
    arr = (c_wchar_p * n)()
    mgr.GetDevices(cast(arr, POINTER(c_wchar_p)), n)
    return mgr, [arr[i] for i in range(n)]


def open_device(dev_id):
    dev = CreateObject(port.PortableDevice, interface=port.IPortableDevice)
    client_info = CreateObject(types_.PortableDeviceValues, interface=port.IPortableDeviceValues)
    dev.Open(dev_id, client_info)
    return dev


# ---- property reads ---------------------------------------------------------
def make_keys(*keys):
    kc = CreateObject(types_.PortableDeviceKeyCollection, interface=port.IPortableDeviceKeyCollection)
    for k in keys:
        kc.Add(k)
    return kc


def _all_obj_keys():
    return make_keys(
        WPD_OBJECT_NAME, WPD_OBJECT_ORIGINAL_FILE_NAME, WPD_OBJECT_CONTENT_TYPE,
        WPD_OBJECT_FORMAT, WPD_OBJECT_SIZE, WPD_OBJECT_PERSISTENT_UNIQUE_ID,
        WPD_OBJECT_DATE_CREATED, WPD_OBJECT_DATE_MODIFIED,
    )


ALL_OBJ_KEYS = None  # lazily built after COM is ready


def get_str(vals, key):
    try:
        return vals.GetStringValue(key)
    except Exception:
        return None


def get_u64(vals, key):
    try:
        return int(vals.GetUnsignedLargeIntegerValue(key))
    except Exception:
        try:
            return int(vals.GetUnsignedIntegerValue(key))
        except Exception:
            return None


def get_guid(vals, key):
    try:
        return str(vals.GetGuidValue(key))
    except Exception:
        return None


def get_date(vals, key):
    # iOS reports dates as strings 'YYYY/MM/DD:HH:MM:SS.fff'; keep VT_DATE fallback.
    s = get_str(vals, key)
    if s:
        return s
    try:
        return f"ole:{vals.GetFloatValue(key)}"
    except Exception:
        return None


def content_type_name(guid_str):
    if not guid_str:
        return None
    return CONTENT_TYPES.get(guid_str.upper(), guid_str)


# ---- enumeration ------------------------------------------------------------
def enum_children(content, parent_id):
    """Yield child object-id strings under parent_id.

    NOTE: comtypes surfaces only the first element of the LPWSTR* array that
    Next() fills, so we request exactly one id per call. A production reader
    wanting batched Next() needs a hand-written memberspec.
    """
    enum = content.EnumObjects(c_ulong(0), parent_id, None)
    while True:
        obj_id, fetched = (lambda r: (r[0], int(r[1])))(enum.Next(1))
        if fetched == 0 or not obj_id:
            break
        yield obj_id


def obj_record(props, obj_id):
    global ALL_OBJ_KEYS
    if ALL_OBJ_KEYS is None:
        ALL_OBJ_KEYS = _all_obj_keys()
    vals = props.GetValues(obj_id, ALL_OBJ_KEYS)
    return {
        "id": obj_id,
        "name": get_str(vals, WPD_OBJECT_NAME),
        "orig": get_str(vals, WPD_OBJECT_ORIGINAL_FILE_NAME),
        "ctype": content_type_name(get_guid(vals, WPD_OBJECT_CONTENT_TYPE)),
        "format": get_guid(vals, WPD_OBJECT_FORMAT),
        "size": get_u64(vals, WPD_OBJECT_SIZE),
        "puid": get_str(vals, WPD_OBJECT_PERSISTENT_UNIQUE_ID),
        "created": get_date(vals, WPD_OBJECT_DATE_CREATED),
        "modified": get_date(vals, WPD_OBJECT_DATE_MODIFIED),
    }


def is_folder(rec):
    return rec["ctype"] in ("FOLDER", "FUNCTIONAL")


def walk(content, props, root_id, max_files=None):
    """Iterative DFS yielding records (with ['path']); hard-stops at max_files leaves."""
    stack = [(root_id, "")]
    files = 0
    while stack:
        parent, ppath = stack.pop()
        for cid in enum_children(content, parent):
            rec = obj_record(props, cid)
            label = rec["orig"] or rec["name"] or cid
            rec["path"] = f"{ppath}/{label}" if ppath else label
            yield rec
            if is_folder(rec):
                stack.append((cid, rec["path"]))
            else:
                files += 1
                if max_files and files >= max_files:
                    return


# ---- content read (W1/V1) ---------------------------------------------------
def read_object_bytes(content, obj_id, expected_size=None, chunk=256 * 1024, cap=None):
    resources = content.Transfer()
    optimal, stream = resources.GetStream(obj_id, WPD_RESOURCE_DEFAULT, c_ulong(STGM_READ))
    total = 0
    t0 = time.time()
    while True:
        data, nread = stream.RemoteRead(chunk)
        n = int(nread)
        if n == 0:
            break
        total += n
        if cap and total >= cap:
            break
        if expected_size and total >= expected_size:
            break
    dt = time.time() - t0
    return {
        "read": total,
        "optimal_buffer": int(optimal) if optimal is not None else None,
        "seconds": round(dt, 3),
        "mbps": round(total / 1e6 / dt, 2) if dt > 0 else None,
        "size_match": (expected_size is not None and total == expected_size),
    }


def device_info(dev):
    content = dev.Content()
    props = content.Properties()
    vals = props.GetValues("DEVICE", make_keys(
        WPD_DEVICE_MANUFACTURER, WPD_DEVICE_MODEL,
        WPD_DEVICE_SERIAL_NUMBER, WPD_DEVICE_FRIENDLY_NAME,
    ))
    return content, props, {
        "manufacturer": get_str(vals, WPD_DEVICE_MANUFACTURER),
        "model": get_str(vals, WPD_DEVICE_MODEL),
        "serial": get_str(vals, WPD_DEVICE_SERIAL_NUMBER),
        "friendly": get_str(vals, WPD_DEVICE_FRIENDLY_NAME),
    }


# ---- modes ------------------------------------------------------------------
def pick_device(idx):
    _, ids = list_device_ids()
    if not ids:
        print("No portable devices connected.")
        sys.exit(2)
    if idx >= len(ids):
        print(f"device-index {idx} out of range (found {len(ids)})")
        sys.exit(2)
    return ids[idx]


def mode_list():
    _, ids = list_device_ids()
    print(f"Found {len(ids)} portable device(s):")
    for i, dev_id in enumerate(ids):
        try:
            dev = open_device(dev_id)
            _, _, info = device_info(dev)
            dev.Close()
        except Exception as e:
            info = {"error": str(e)}
        print(f"[{i}] {dev_id}")
        for k, v in info.items():
            print(f"      {k}: {v}")


def mode_report(idx, max_files, do_read):
    dev = open_device(pick_device(idx))
    content, props, info = device_info(dev)
    print("=== DEVICE ===")
    for k, v in info.items():
        print(f"  {k}: {v}")

    print("\n=== WALK ===")
    files = []
    t0 = time.time()
    for rec in walk(content, props, "DEVICE", max_files=max_files):
        if is_folder(rec):
            print(f"  DIR  {rec['path']}")
        else:
            files.append(rec)
            print(f"  FILE {rec['path']}  size={rec['size']} ctype={rec['ctype']} "
                  f"fmt={rec['format']} puid={rec['puid']} created={rec['created']}")
    dt = time.time() - t0
    if dt > 0:
        print(f"\n  {len(files)} files in {dt:.2f}s = {len(files)/dt:.1f} files/s (cold=slow, warm cache=fast)")

    if do_read and files:
        target = max(files, key=lambda r: r["size"] or 0)
        print(f"\n=== READ ({target['path']}, size={target['size']}) ===")
        for k, v in read_object_bytes(content, target["id"], target["size"]).items():
            print(f"  {k}: {v}")
    dev.Close()


def _sample(idx, max_files):
    dev_id = pick_device(idx)
    dev = open_device(dev_id)
    content, props, info = device_info(dev)
    sample = [
        {"path": r["path"], "name": r["name"], "orig": r["orig"], "size": r["size"],
         "puid": r["puid"], "created": r["created"], "id": r["id"]}
        for r in walk(content, props, "DEVICE", max_files=max_files) if not is_folder(r)
    ]
    dev.Close()
    return {"device_id": dev_id, "device": info, "sample": sample}


def mode_snapshot(out_path, idx, max_files):
    snap = _sample(idx, max_files)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
    print(f"Wrote {len(snap['sample'])} objects. serial={snap['device']['serial']}")
    print(f"Now UNPLUG the phone, replug (or reboot), then run:  compare {out_path}")


def mode_compare(in_path, idx):
    with open(in_path, encoding="utf-8") as f:
        old = json.load(f)
    new = _sample(idx, len(old["sample"]) + 50)
    print("=== D1 serial stability ===")
    print(f"  before={old['device']['serial']}  after={new['device']['serial']}  "
          f"STABLE={old['device']['serial'] == new['device']['serial']}")

    by_key = {}
    for r in new["sample"]:
        by_key.setdefault((r["orig"] or r["name"], r["size"], r["created"]), []).append(r)
    stable = changed = missing = idchg = 0
    for r in old["sample"]:
        cands = by_key.get((r["orig"] or r["name"], r["size"], r["created"]), [])
        if not cands:
            missing += 1
            continue
        nr = cands[0]
        if nr["puid"] == r["puid"]:
            stable += 1
        else:
            changed += 1
            print(f"  PUID CHANGED {r['path']}: {r['puid']} -> {nr['puid']}")
        idchg += (nr["id"] != r["id"])
    print(f"\n=== I1 PUID stability ===")
    print(f"  stable={stable} changed={changed} missing={missing} (object-id changed on {idchg} matched)")
    print("  => I1 holds iff changed==0 and missing==0 (object-id churn alone is fine).")


def mode_w2(idx, min_mb):
    dev = open_device(pick_device(idx))
    content = dev.Content()
    props = content.Properties()
    _, _, info = device_info(dev)
    print("device:", info["serial"])
    min_bytes = min_mb * 1_000_000
    big = []
    for r in walk(content, props, "DEVICE"):
        if not is_folder(r) and (r["size"] or 0) >= min_bytes:
            big.append((r["id"], r["size"]))
            if len(big) >= 6:
                break
    ids_ = [o for o, _ in big]
    print(f"selected {len(big)} files, {sum(s for _, s in big)/1e6:.0f} MB")
    if len(big) < 2:
        print("not enough large files; lower --min-mb")
        dev.Close()
        return

    res = content.Transfer()
    t0 = time.time()
    sbytes = sum(read_object_bytes(content, o)["read"] for o in ids_)
    sdt = time.time() - t0
    smbps = sbytes / 1e6 / sdt
    print(f"\nSERIAL (1 session): {sbytes/1e6:.0f} MB in {sdt:.2f}s = {smbps:.1f} MB/s")

    half = len(ids_) // 2
    sets = [ids_[:half], ids_[half:]]
    results = [0, 0]

    def worker(i):
        comtypes.CoInitialize()
        try:
            d = open_device(pick_device(idx))
            c = d.Content()
            results[i] = sum(read_object_bytes(c, o)["read"] for o in sets[i])
            d.Close()
        except comtypes.COMError as e:
            print(f"  worker {i}: {e.args[0]:#010x} {e.args[1]} (concurrent stream refused)")
        finally:
            comtypes.CoUninitialize()

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    cdt = time.time() - t0
    cmbps = sum(results) / 1e6 / cdt if cdt else 0
    print(f"CONCURRENT (2 sessions): {sum(results)/1e6:.0f} MB in {cdt:.2f}s = {cmbps:.1f} MB/s")
    ratio = cmbps / smbps if smbps else 0
    print(f"\nconcurrent/serial ratio = {ratio:.2f}  "
          f"({'single-lane, no win' if ratio < 1.4 else 'real parallelism'})")
    dev.Close()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    mode, rest = args[0], args[1:]

    def flag(name, default, cast_fn):
        return cast_fn(rest[rest.index(name) + 1]) if name in rest else default

    idx = flag("--device-index", 0, int)
    if mode == "list":
        mode_list()
    elif mode == "report":
        mode_report(idx, flag("--max", None, int), "--read" in rest)
    elif mode == "snapshot":
        mode_snapshot(rest[0], idx, flag("--max", 200, int))
    elif mode == "compare":
        mode_compare(rest[0], idx)
    elif mode == "w2":
        mode_w2(idx, flag("--min-mb", 20, int))
    else:
        print(f"unknown mode: {mode}\n{__doc__}")


if __name__ == "__main__":
    main()
