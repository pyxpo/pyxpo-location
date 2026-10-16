"""Device location, heading and geocoding: the expo-location API.

    import pyxpo_location as Location

    perm = await Location.request_foreground_permissions_async()
    if perm["granted"]:
        loc = await Location.get_current_position_async(accuracy=Location.Accuracy.HIGH)
        loc["coords"]["latitude"], loc["coords"]["longitude"], loc["timestamp"]

    sub = await Location.watch_position_async({"accuracy": Location.Accuracy.BALANCED, "distanceInterval": 10},
                                              lambda loc: print(loc["coords"]))
    sub.remove()

    places = await Location.reverse_geocode_async({"latitude": 52.52, "longitude": 13.40})

iOS uses Core Location. Android uses the platform `LocationManager` (no Google Play services), its sensors for
the heading and `Geocoder` for geocoding. Background updates and geofencing need a task manager, which pyxpo
does not have yet: those functions raise `NotSupportedError`.
"""
from __future__ import annotations

import asyncio
import itertools
import re
import time
from enum import Enum, IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pyxpo.module_api import NotSupportedError, call, on_event, permission_response, platform, require_linked

__version__ = "0.1.0a1"

LocationObject = Dict[str, Any]
Handler = Callable[[Any], Any]


class Accuracy(IntEnum):
    """How precise a location should be. Higher costs more battery."""
    LOWEST = 1                # about 3 km
    LOW = 2                   # about 1 km
    BALANCED = 3              # about 100 m
    HIGH = 4                  # about 10 m
    HIGHEST = 5               # the best the device can do
    BEST_FOR_NAVIGATION = 6   # the best, plus sensor data on iOS


LocationAccuracy = Accuracy


class ActivityType(IntEnum):
    """iOS: what the app tracks, so the system can pause updates wisely."""
    OTHER = 1
    AUTOMOTIVE_NAVIGATION = 2
    FITNESS = 3
    OTHER_NAVIGATION = 4
    AIRBORNE = 5


LocationActivityType = ActivityType


class GeofencingEventType(IntEnum):
    ENTER = 1
    EXIT = 2


LocationGeofencingEventType = GeofencingEventType


class GeofencingRegionState(IntEnum):
    UNKNOWN = 0
    INSIDE = 1
    OUTSIDE = 2


LocationGeofencingRegionState = GeofencingRegionState


class PermissionStatus(str, Enum):
    GRANTED = "granted"
    DENIED = "denied"
    UNDETERMINED = "undetermined"


_POSITION, _HEADING = "Location.position", "Location.heading"
_ids = itertools.count(1)


def _native() -> None:
    if platform() == "desktop":
        raise NotSupportedError("location needs iOS or Android")
    require_linked("Location", "location", package="pyxpo-location")


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _options(options: Optional[Dict[str, Any]], kw: Dict[str, Any]) -> Dict[str, Any]:
    """Expo option dicts use camelCase; keyword arguments may use either spelling."""
    out = {_snake(k): v for k, v in (options or {}).items()}
    out.update({_snake(k): v for k, v in kw.items()})
    return out


def _accuracy(value: Any) -> int:
    try:
        return int(Accuracy(int(value)))
    except (TypeError, ValueError):
        raise ValueError("accuracy must be a Location.Accuracy (1 to 6), got %r" % (value,)) from None


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("%s must be a number >= 0, got %r" % (name, value))
    return float(value)


# permissions

def _permission(raw: Optional[Dict[str, Any]], background: bool) -> Dict[str, Any]:
    raw = raw or {}
    out = permission_response(str(raw.get("status", "denied")), bool(raw.get("canAskAgain", False)))
    if not background:
        p = platform()
        if p == "ios":
            out["ios"] = {"scope": raw.get("scope", "none")}
        elif p == "android":
            out["android"] = {"accuracy": raw.get("accuracy", "none")}
    return out


async def get_foreground_permissions_async() -> Dict[str, Any]:
    """The permission to use the location while the app is open, without asking. Expo's `PermissionResponse`
    plus `ios: {"scope": "whenInUse" | "always" | "none"}` or `android: {"accuracy": "fine" | "coarse" | "none"}`."""
    _native()
    return _permission(await call("Location", "getPermission", False, reply=True), False)


async def request_foreground_permissions_async() -> Dict[str, Any]:
    """Asks for the location while the app is open. Android asks for precise and approximate location; the user
    may allow only approximate (`android.accuracy == "coarse"`)."""
    _native()
    return _permission(await call("Location", "requestPermission", False, reply=True), False)


async def get_background_permissions_async() -> Dict[str, Any]:
    """The permission to use the location while the app is in the background ("Always" on iOS), without asking."""
    _native()
    return _permission(await call("Location", "getPermission", True, reply=True), True)


async def request_background_permissions_async() -> Dict[str, Any]:
    """Asks for the location in the background. Ask for the foreground permission first. Android needs
    `ACCESS_BACKGROUND_LOCATION` in `[tool.pyxpo.android] permissions`; Android 11+ sends the user to Settings."""
    _native()
    return _permission(await call("Location", "requestPermission", True, reply=True), True)


def _permission_hook(getter: Callable, requester: Callable, get: bool, request: bool) -> Tuple[Any, Callable, Callable]:
    from pyxpo import use_effect, use_state
    status, set_status = use_state(None)

    async def ask() -> Dict[str, Any]:
        res = await requester()
        set_status(res)
        return res

    async def read() -> Dict[str, Any]:
        res = await getter()
        set_status(res)
        return res

    def start() -> None:
        if request:
            asyncio.ensure_future(ask())
        elif get:
            asyncio.ensure_future(read())

    use_effect(start, [])
    return status, ask, read


def use_foreground_permissions(get: bool = True, request: bool = False) -> Tuple[Any, Callable, Callable]:
    """`(response, request, get)`: the foreground permission (None until read), and coroutines that ask for it
    or read it again and update the component. `get=True` reads it on mount; `request=True` asks on mount."""
    return _permission_hook(get_foreground_permissions_async, request_foreground_permissions_async, get, request)


def use_background_permissions(get: bool = True, request: bool = False) -> Tuple[Any, Callable, Callable]:
    """Like `use_foreground_permissions`, for the background permission."""
    return _permission_hook(get_background_permissions_async, request_background_permissions_async, get, request)


# positions

async def get_current_position_async(options: Optional[Dict[str, Any]] = None, **kw: Any) -> LocationObject:
    """A fresh location. Options: `accuracy` (default `Accuracy.BALANCED`). Raises when the permission is
    missing, location services are off, or no fix arrives.

    Returns `{"coords": {"latitude", "longitude", "altitude", "accuracy", "altitudeAccuracy", "heading",
    "speed"}, "timestamp", "mocked"}`; values the device does not know are None, `timestamp` is milliseconds
    since 1970, `mocked` is Android only."""
    o = _options(options, kw)
    acc = _accuracy(o.get("accuracy", Accuracy.BALANCED))
    _native()
    return dict(await call("Location", "getCurrentPosition", {"accuracy": acc}, reply=True))


async def get_last_known_position_async(options: Optional[Dict[str, Any]] = None, **kw: Any) -> Optional[LocationObject]:
    """The last location the device knows, without waiting for a new one; None when there is none or it does
    not meet the options. Options: `max_age` (milliseconds), `required_accuracy` (meters)."""
    o = _options(options, kw)
    max_age = _number("max_age", o["max_age"]) if o.get("max_age") is not None else None
    required = _number("required_accuracy", o["required_accuracy"]) if o.get("required_accuracy") is not None else None
    _native()
    loc = await call("Location", "getLastKnown", reply=True)
    if not loc:
        return None
    if max_age is not None and time.time() * 1000 - float(loc.get("timestamp") or 0) > max_age:
        return None
    acc = (loc.get("coords") or {}).get("accuracy")
    if required is not None and (acc is None or acc > required):
        return None
    return dict(loc)


class LocationSubscription:
    def __init__(self, kind: str, wid: int) -> None:
        self._kind, self._id = kind, wid

    def remove(self) -> None:
        """Stops the updates. Safe to call twice."""
        table = _watches[self._kind]
        if table.pop(self._id, None) is None:
            return
        call("Location", "removeWatch", self._id)
        if not table and _offs.get(self._kind):
            _offs.pop(self._kind)()


_watches: Dict[str, Dict[int, Tuple[Handler, Optional[Handler]]]] = {_POSITION: {}, _HEADING: {}}
_offs: Dict[str, Callable[[], None]] = {}


def _dispatch(kind: str, key: str) -> Callable[[Any], None]:
    def handle(payload: Any) -> None:
        payload = payload or {}
        entry = _watches[kind].get(payload.get("id"))
        if entry is None:
            return
        callback, on_error = entry
        if payload.get("error") is not None:
            if on_error is not None:
                on_error(str(payload["error"]))
        else:
            callback(payload.get(key))
    return handle


async def _watch(kind: str, key: str, method: str, args: List[Any], callback: Handler,
                 error_handler: Optional[Handler]) -> LocationSubscription:
    if not callable(callback):
        raise TypeError("callback must be callable")
    _native()
    wid = next(_ids)
    if kind not in _offs:
        _offs[kind] = on_event(kind, _dispatch(kind, key))
    _watches[kind][wid] = (callback, error_handler)
    sub = LocationSubscription(kind, wid)
    try:
        await call("Location", method, wid, *args, reply=True)
    except BaseException:
        sub.remove()
        raise
    return sub


async def watch_position_async(options: Optional[Dict[str, Any]] = None, callback: Optional[Handler] = None,
                               error_handler: Optional[Handler] = None, **kw: Any) -> LocationSubscription:
    """Calls `callback(location)` with each new location until `.remove()` on the returned subscription.
    Options: `accuracy` (default `Accuracy.BALANCED`), `time_interval` (Android: the minimum milliseconds between
    updates), `distance_interval` (the minimum meters between updates). `error_handler(message)` gets errors
    that stop the updates."""
    o = _options(options, kw)
    native = {"accuracy": _accuracy(o.get("accuracy", Accuracy.BALANCED)),
              "timeInterval": _number("time_interval", o.get("time_interval") or 0),
              "distanceInterval": _number("distance_interval", o.get("distance_interval") or 0)}
    return await _watch(_POSITION, "location", "watchPosition", [native], callback, error_handler)


async def watch_heading_async(callback: Handler, error_handler: Optional[Handler] = None) -> LocationSubscription:
    """Calls `callback(heading)` as the compass heading changes: `{"trueHeading", "magHeading", "accuracy"}`.
    Headings are degrees from north; `trueHeading` is -1 while the location is unknown. iOS `accuracy` is the
    error in degrees; Android `accuracy` is 0 (unreliable) to 3 (high). Raises when the device has no compass."""
    return await _watch(_HEADING, "heading", "watchHeading", [], callback, error_handler)


async def get_heading_async() -> Dict[str, Any]:
    """One compass heading, once it is accurate enough or after a few readings (as Expo does)."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()
    tries = [0]

    def on_heading(h: Dict[str, Any]) -> None:
        if done.done():
            return
        if (h.get("accuracy") or 0) > 1 or tries[0] > 5:
            done.set_result(h)
        tries[0] += 1

    def on_error(message: str) -> None:
        if not done.done():
            done.set_exception(RuntimeError(message))

    sub = await watch_heading_async(on_heading, on_error)
    try:
        return dict(await done)
    finally:
        sub.remove()


# geocoding and services

async def geocode_async(address: str) -> List[Dict[str, Any]]:
    """Places for an address: `[{"latitude", "longitude", "altitude", "accuracy"}]`. Needs the network. Android
    devices without a geocoder raise."""
    if not isinstance(address, str) or not address.strip():
        raise ValueError("address must be a non-empty string")
    _native()
    return [dict(x) for x in await call("Location", "geocode", address, reply=True) or []]


async def reverse_geocode_async(location: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Addresses at `{"latitude", "longitude"}`: each has `name`, `street`, `streetNumber`, `district`, `city`,
    `subregion`, `region`, `postalCode`, `country`, `isoCountryCode`, `timezone` and `formattedAddress`, None
    when unknown."""
    try:
        lat, lon = float(location["latitude"]), float(location["longitude"])
    except (TypeError, KeyError, ValueError):
        raise ValueError("location needs number latitude and longitude") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude or longitude out of range")
    _native()
    return [dict(x) for x in await call("Location", "reverseGeocode", lat, lon, reply=True) or []]


async def has_services_enabled_async() -> bool:
    """Whether location services are on for the device. False on the desktop."""
    if platform() == "desktop":
        return False
    _native()
    return bool(await call("Location", "servicesEnabled", reply=True))


async def get_provider_status_async() -> Dict[str, Any]:
    """`locationServicesEnabled`, `backgroundModeEnabled`, and on Android `gpsAvailable`, `networkAvailable`,
    `passiveAvailable`."""
    _native()
    return dict(await call("Location", "providerStatus", reply=True))


async def enable_network_provider_async() -> None:
    """Android: when network location is off, opens the location settings and returns once the user turns it on;
    raises when the user comes back without turning it on. Does nothing on iOS."""
    _native()
    if platform() == "android":
        await call("Location", "enableNetworkProvider", reply=True)


async def is_background_location_available_async() -> bool:
    """Whether background location updates can run. Always False: pyxpo has no task manager yet."""
    return False


# task-based APIs

def _no_tasks(name: str) -> NotSupportedError:
    return NotSupportedError("%s needs a background task manager, which pyxpo does not have yet; "
                             "use watch_position_async while the app is open" % name)


async def start_location_updates_async(task_name: str, options: Optional[Dict[str, Any]] = None) -> None:
    raise _no_tasks("start_location_updates_async")


async def stop_location_updates_async(task_name: str) -> None:
    """Nothing to stop: background updates cannot start."""


async def has_started_location_updates_async(task_name: str) -> bool:
    return False


async def start_geofencing_async(task_name: str, regions: Optional[List[Dict[str, Any]]] = None) -> None:
    raise _no_tasks("start_geofencing_async")


async def stop_geofencing_async(task_name: str) -> None:
    """Nothing to stop: geofencing cannot start."""


async def has_started_geofencing_async(task_name: str) -> bool:
    return False


getForegroundPermissionsAsync = get_foreground_permissions_async
requestForegroundPermissionsAsync = request_foreground_permissions_async
getBackgroundPermissionsAsync = get_background_permissions_async
requestBackgroundPermissionsAsync = request_background_permissions_async
useForegroundPermissions = use_foreground_permissions
useBackgroundPermissions = use_background_permissions
getCurrentPositionAsync = get_current_position_async
getLastKnownPositionAsync = get_last_known_position_async
watchPositionAsync = watch_position_async
watchHeadingAsync = watch_heading_async
getHeadingAsync = get_heading_async
geocodeAsync = geocode_async
reverseGeocodeAsync = reverse_geocode_async
hasServicesEnabledAsync = has_services_enabled_async
getProviderStatusAsync = get_provider_status_async
enableNetworkProviderAsync = enable_network_provider_async
isBackgroundLocationAvailableAsync = is_background_location_available_async
startLocationUpdatesAsync = start_location_updates_async
stopLocationUpdatesAsync = stop_location_updates_async
hasStartedLocationUpdatesAsync = has_started_location_updates_async
startGeofencingAsync = start_geofencing_async
stopGeofencingAsync = stop_geofencing_async
hasStartedGeofencingAsync = has_started_geofencing_async

__all__ = [
    "Accuracy", "LocationAccuracy", "ActivityType", "LocationActivityType", "GeofencingEventType",
    "LocationGeofencingEventType", "GeofencingRegionState", "LocationGeofencingRegionState", "PermissionStatus",
    "LocationSubscription", "get_foreground_permissions_async", "request_foreground_permissions_async",
    "get_background_permissions_async", "request_background_permissions_async", "use_foreground_permissions",
    "use_background_permissions", "get_current_position_async", "get_last_known_position_async",
    "watch_position_async", "watch_heading_async", "get_heading_async", "geocode_async", "reverse_geocode_async",
    "has_services_enabled_async", "get_provider_status_async", "enable_network_provider_async",
    "is_background_location_available_async", "start_location_updates_async", "stop_location_updates_async",
    "has_started_location_updates_async", "start_geofencing_async", "stop_geofencing_async",
    "has_started_geofencing_async",
    "getForegroundPermissionsAsync", "requestForegroundPermissionsAsync", "getBackgroundPermissionsAsync",
    "requestBackgroundPermissionsAsync", "useForegroundPermissions", "useBackgroundPermissions",
    "getCurrentPositionAsync", "getLastKnownPositionAsync", "watchPositionAsync", "watchHeadingAsync",
    "getHeadingAsync", "geocodeAsync", "reverseGeocodeAsync", "hasServicesEnabledAsync", "getProviderStatusAsync",
    "enableNetworkProviderAsync", "isBackgroundLocationAvailableAsync", "startLocationUpdatesAsync",
    "stopLocationUpdatesAsync", "hasStartedLocationUpdatesAsync", "startGeofencingAsync", "stopGeofencingAsync",
    "hasStartedGeofencingAsync",
]
