# pyright: reportMissingImports=false, reportMissingModuleSource=false, reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false
"""Windows Portable Devices (WPD/MTP) access for `pix import`.

Phones present over USB as WPD objects (not a drive letter), reached through the
WPD COM API. `pywin32` does not wrap WPD; the working path — validated against a
real iPhone, see spec/import.md → Validation results — is `comtypes`, which
auto-generates the interfaces from `PortableDeviceApi.dll` + `PortableDeviceTypes.dll`.

comtypes is a Windows-only dependency and its interface modules are generated on
first use, so the import is **lazy**: `import pix.wpd` succeeds anywhere; the COM
machinery only spins up when a function is called, raising `WpdUnavailable` if
comtypes (or Windows) is missing.

Two comtypes quirks this layer hides (both found during validation):
- `IEnumPortableDeviceObjectIDs::Next` surfaces only the first id of the array it
  fills, so we enumerate exactly one object per call.
- `IPortableDeviceManager::GetDevices` returns its in/out params as `[array, count]`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


class WpdUnavailable(Exception):
    """WPD/comtypes could not be initialised (not Windows, or comtypes missing)."""


class WpdError(Exception):
    """A WPD operation failed."""


# --- lazy COM initialisation -------------------------------------------------
_port: Any = None
_types: Any = None
_comtypes: Any = None


def _ensure() -> None:
    """Import comtypes and generate the WPD interface modules once."""
    global _port, _types, _comtypes
    if _port is not None:
        return
    try:
        import comtypes  # noqa: PLC0415
        from comtypes.client import GetModule  # noqa: PLC0415

        GetModule("portabledeviceapi.dll")
        GetModule("portabledevicetypes.dll")
        from comtypes.gen import PortableDeviceApiLib as port  # noqa: PLC0415
        from comtypes.gen import PortableDeviceTypesLib as types_  # noqa: PLC0415
    except Exception as e:  # ImportError, OSError (non-Windows), COM errors
        raise WpdUnavailable(
            "WPD access requires Windows + comtypes. Install with "
            "`uv sync` (comtypes is a Windows-only dependency)."
        ) from e
    _comtypes, _port, _types = comtypes, port, types_


def _pk(fmtid: str, pid: int) -> Any:
    k = _port._tagpropertykey()
    k.fmtid = _comtypes.GUID(fmtid)
    k.pid = pid
    return k


# WPD_OBJECT_PROPERTIES_V1 / WPD_DEVICE_PROPERTIES_V1 property keys, built lazily
# (they need the generated struct type). Cached after first build.
_KEYS: dict[str, Any] = {}


def _keys() -> dict[str, Any]:
    if _KEYS:
        return _KEYS
    obj = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"
    dev = "{26D4979A-E643-4626-9E2B-736DC0C92FDC}"
    _KEYS.update(
        name=_pk(obj, 4),
        puid=_pk(obj, 5),
        format=_pk(obj, 6),
        ctype=_pk(obj, 7),
        size=_pk(obj, 11),
        orig=_pk(obj, 12),
        created=_pk(obj, 18),
        modified=_pk(obj, 19),
        dev_manufacturer=_pk(dev, 7),
        dev_model=_pk(dev, 8),
        dev_serial=_pk(dev, 9),
        dev_friendly=_pk(dev, 12),
        resource_default=_pk("{E81E79BE-34F0-41BF-B53F-F1A06AE87842}", 0),
    )
    return _KEYS


_CONTENT_TYPES = {
    "{27E2E392-A111-48E0-AB0C-E17705A05F85}": "FOLDER",
    "{99ED0160-17FF-4C44-9D98-1D7A6F941921}": "FUNCTIONAL",
}
_STGM_READ = 0
DEVICE_ROOT = "DEVICE"


@dataclass(frozen=True)
class DeviceInfo:
    """Identity of a connected portable device."""

    device_id: str
    manufacturer: str | None
    model: str | None
    serial: str | None
    friendly: str | None

    @property
    def is_apple(self) -> bool:
        return (self.manufacturer or "").lower().startswith("apple")


@dataclass(frozen=True)
class WpdObject:
    """One enumerated device object (folder or file), from cheap properties only."""

    id: str
    name: str | None
    orig: str | None
    ctype: str | None
    format: str | None
    size: int | None
    puid: str | None
    created: str | None
    modified: str | None

    @property
    def is_folder(self) -> bool:
        return self.ctype in ("FOLDER", "FUNCTIONAL")

    @property
    def filename(self) -> str:
        """Best display/landing name: original filename, else object name, else id."""
        return self.orig or self.name or self.id


# --- value extraction --------------------------------------------------------
def _get_str(vals: Any, key: Any) -> str | None:
    try:
        return vals.GetStringValue(key)
    except Exception:
        return None


def _get_u64(vals: Any, key: Any) -> int | None:
    try:
        return int(vals.GetUnsignedLargeIntegerValue(key))
    except Exception:
        try:
            return int(vals.GetUnsignedIntegerValue(key))
        except Exception:
            return None


def _get_guid(vals: Any, key: Any) -> str | None:
    try:
        return str(vals.GetGuidValue(key))
    except Exception:
        return None


def _get_date(vals: Any, key: Any) -> str | None:
    # iOS serves dates as strings 'YYYY/MM/DD:HH:MM:SS.fff'; VT_DATE fallback.
    s = _get_str(vals, key)
    if s:
        return s
    try:
        return f"ole:{vals.GetFloatValue(key)}"
    except Exception:
        return None


def list_devices() -> list[DeviceInfo]:
    """Enumerate connected portable devices. Raises `WpdUnavailable` off Windows."""
    _ensure()
    from ctypes import POINTER, c_wchar_p, cast  # noqa: PLC0415

    mgr = _comtypes.client.CreateObject(
        _port.PortableDeviceManager, interface=_port.IPortableDeviceManager
    )
    mgr.RefreshDeviceList()
    _, n = mgr.GetDevices(POINTER(c_wchar_p)(), 0)  # [array, count]
    n = int(n)
    if n == 0:
        return []
    arr = (c_wchar_p * n)()
    mgr.GetDevices(cast(arr, POINTER(c_wchar_p)), n)
    out: list[DeviceInfo] = []
    for i in range(n):
        dev_id = arr[i]
        try:
            with open_device(dev_id) as dev:
                out.append(dev.info())
        except Exception:
            out.append(DeviceInfo(dev_id, None, None, None, None))
    return out


def open_device(device_id: str) -> "Device":
    """Open a WPD session on `device_id`. Use as a context manager."""
    _ensure()
    handle = _comtypes.client.CreateObject(
        _port.PortableDevice, interface=_port.IPortableDevice
    )
    client_info = _comtypes.client.CreateObject(
        _types.PortableDeviceValues, interface=_port.IPortableDeviceValues
    )
    try:
        handle.Open(device_id, client_info)
    except Exception as e:
        raise WpdError(f"could not open device {device_id}: {e}") from e
    return Device(device_id, handle)


class Device:
    """An open WPD device session (enumerate + stream). Context manager."""

    def __init__(self, device_id: str, handle: Any) -> None:
        self.device_id = device_id
        self._handle = handle
        self._content = handle.Content()
        self._props = self._content.Properties()
        self._obj_keys: Any = None

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._handle.Close()
        except Exception:
            pass

    def info(self) -> DeviceInfo:
        k = _keys()
        kc = self._make_keys(
            k["dev_manufacturer"], k["dev_model"], k["dev_serial"], k["dev_friendly"]
        )
        vals = self._props.GetValues(DEVICE_ROOT, kc)
        return DeviceInfo(
            device_id=self.device_id,
            manufacturer=_get_str(vals, k["dev_manufacturer"]),
            model=_get_str(vals, k["dev_model"]),
            serial=_get_str(vals, k["dev_serial"]),
            friendly=_get_str(vals, k["dev_friendly"]),
        )

    def children(self, parent_id: str = DEVICE_ROOT) -> Iterator[WpdObject]:
        """Yield immediate child objects of `parent_id` (cheap properties only)."""
        from ctypes import c_ulong  # noqa: PLC0415

        enum = self._content.EnumObjects(c_ulong(0), parent_id, None)
        while True:
            res = enum.Next(1)  # comtypes: [firstObjID, fetched]
            obj_id, fetched = res[0], int(res[1])
            if fetched == 0 or not obj_id:
                break
            yield self._record(obj_id)

    def _record(self, obj_id: str) -> WpdObject:
        k = _keys()
        if self._obj_keys is None:
            self._obj_keys = self._make_keys(
                k["name"], k["orig"], k["ctype"], k["format"],
                k["size"], k["puid"], k["created"], k["modified"],
            )
        vals = self._props.GetValues(obj_id, self._obj_keys)
        guid = _get_guid(vals, k["ctype"])
        ctype = _CONTENT_TYPES.get(guid.upper(), guid) if guid else None
        return WpdObject(
            id=obj_id,
            name=_get_str(vals, k["name"]),
            orig=_get_str(vals, k["orig"]),
            ctype=ctype,
            format=_get_guid(vals, k["format"]),
            size=_get_u64(vals, k["size"]),
            puid=_get_str(vals, k["puid"]),
            created=_get_date(vals, k["created"]),
            modified=_get_date(vals, k["modified"]),
        )

    def stream(self, obj_id: str, chunk: int = 256 * 1024) -> Iterator[bytes]:
        """Yield the object's content in `chunk`-sized byte blocks."""
        from ctypes import c_ulong  # noqa: PLC0415

        resources = self._content.Transfer()
        _, stm = resources.GetStream(
            obj_id, _keys()["resource_default"], c_ulong(_STGM_READ)
        )
        while True:
            data, nread = stm.RemoteRead(chunk)
            n = int(nread)
            if n == 0:
                break
            yield bytes(data[:n]) if not isinstance(data, bytes) else data[:n]

    def _make_keys(self, *keys: Any) -> Any:
        kc = _comtypes.client.CreateObject(
            _types.PortableDeviceKeyCollection,
            interface=_port.IPortableDeviceKeyCollection,
        )
        for key in keys:
            kc.Add(key)
        return kc
