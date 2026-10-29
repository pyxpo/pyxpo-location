"""pyxpo-location: expo-location API over a fake bridge."""
import asyncio
import time

import pytest

from pyxpo.module_api import NotSupportedError, dispatch
from pyxpo.module_testing import FakeBridge

import pyxpo_location as Location


def run(coro):
    return asyncio.run(coro)


def loc(lat=52.5, lon=13.4, acc=5.0, ts=None):
    return {"coords": {"latitude": lat, "longitude": lon, "altitude": None, "accuracy": acc,
                       "altitudeAccuracy": None, "heading": None, "speed": None},
            "timestamp": time.time() * 1000 if ts is None else ts, "mocked": False}


def test_desktop():
    with pytest.raises(NotSupportedError):
        run(Location.get_current_position_async())
    with pytest.raises(NotSupportedError):
        run(Location.request_foreground_permissions_async())
    assert run(Location.has_services_enabled_async()) is False
    assert run(Location.is_background_location_available_async()) is False


def test_not_linked():
    with FakeBridge("ios"):
        with pytest.raises(Exception, match="pyxpo-location"):
            run(Location.get_current_position_async())


def test_permissions():
    with FakeBridge("android") as fb:
        fb.on("Location", "getPermission", lambda bg: {"status": "undetermined", "canAskAgain": True})
        fb.on("Location", "requestPermission",
              lambda bg: {"status": "granted", "canAskAgain": True, "accuracy": "coarse"})
        p = run(Location.get_foreground_permissions_async())
        assert p["status"] == "undetermined" and p["canAskAgain"] and not p["granted"]
        assert p["android"] == {"accuracy": "none"}
        p = run(Location.requestForegroundPermissionsAsync())
        assert p["granted"] and p["android"] == {"accuracy": "coarse"}
        bg = run(Location.request_background_permissions_async())
        assert bg["granted"] and "android" not in bg
        assert fb.calls[-1] == ("Location", "requestPermission", (True,))
    with FakeBridge("ios") as fb:
        fb.on("Location", "getPermission", lambda bg: {"status": "granted", "canAskAgain": False, "scope": "whenInUse"})
        p = run(Location.get_foreground_permissions_async())
        assert p["ios"] == {"scope": "whenInUse"} and p["expires"] == "never"


def test_current_and_last_known():
    with FakeBridge("ios") as fb:
        fb.on("Location", "getCurrentPosition", lambda o: loc())
        assert run(Location.get_current_position_async(accuracy=Location.Accuracy.HIGH))["coords"]["latitude"] == 52.5
        assert run(Location.getCurrentPositionAsync({"accuracy": 6}))["mocked"] is False
        assert [c[2] for c in fb.calls] == [({"accuracy": 4},), ({"accuracy": 6},)]
        with pytest.raises(ValueError):
            run(Location.get_current_position_async(accuracy=9))

        last = [loc(acc=50.0, ts=time.time() * 1000 - 60000)]
        fb.on("Location", "getLastKnown", lambda: last[0])
        assert run(Location.get_last_known_position_async())["coords"]["accuracy"] == 50.0
        assert run(Location.get_last_known_position_async({"maxAge": 1000})) is None
        assert run(Location.get_last_known_position_async(required_accuracy=10)) is None
        assert run(Location.get_last_known_position_async(max_age=120000, required_accuracy=100)) is not None
        last[0] = None
        assert run(Location.get_last_known_position_async()) is None
        with pytest.raises(ValueError):
            run(Location.get_last_known_position_async(max_age=-1))


def test_watch_position_and_heading():
    with FakeBridge("android") as fb:
        fb.on("Location", "watchPosition", lambda wid, o: None)
        fb.on("Location", "watchHeading", lambda wid: None)
        fb.on("Location", "removeWatch", lambda wid: None)
        got, errors, heads = [], [], []

        async def go():
            a = await Location.watch_position_async({"distanceInterval": 10}, got.append, errors.append)
            h = await Location.watch_heading_async(heads.append)
            return a, h

        a, h = run(go())
        pos_id, head_id = fb.calls[0][2][0], fb.calls[1][2][0]
        assert fb.calls[0][2][1] == {"accuracy": 3, "timeInterval": 0.0, "distanceInterval": 10.0}
        dispatch({"name": "Location.position", "data": {"id": pos_id, "location": loc(lat=1.0)}})
        dispatch({"name": "Location.position", "data": {"id": 999, "location": loc()}})
        dispatch({"name": "Location.heading", "data": {"id": head_id, "heading": {"trueHeading": 10, "magHeading": 8,
                                                                                   "accuracy": 3}}})
        dispatch({"name": "Location.position", "data": {"id": pos_id, "error": "provider off"}})
        assert [g["coords"]["latitude"] for g in got] == [1.0] and errors == ["provider off"]
        assert heads == [{"trueHeading": 10, "magHeading": 8, "accuracy": 3}]
        a.remove()
        a.remove()
        dispatch({"name": "Location.position", "data": {"id": pos_id, "location": loc()}})
        assert len(got) == 1
        h.remove()
        assert [c[1] for c in fb.calls].count("removeWatch") == 2
        with pytest.raises(TypeError):
            run(Location.watch_heading_async(None))


def test_watch_fails_cleans_up():
    def fail(wid):
        raise RuntimeError("no compass")

    with FakeBridge("ios") as fb:
        fb.on("Location", "watchHeading", fail)
        fb.on("Location", "removeWatch", lambda wid: None)
        with pytest.raises(RuntimeError, match="no compass"):
            run(Location.get_heading_async())
        assert Location._watches[Location._HEADING] == {}


def test_get_heading_waits_for_accuracy():
    with FakeBridge("ios") as fb:
        fb.on("Location", "removeWatch", lambda wid: None)

        def start(wid):
            loop = asyncio.get_event_loop()
            for acc in (0, 1, 5):
                loop.call_soon(dispatch, {"name": "Location.heading",
                                          "data": {"id": wid, "heading": {"trueHeading": acc, "accuracy": acc}}})

        fb.on("Location", "watchHeading", start)
        assert run(Location.get_heading_async())["accuracy"] == 5
        assert fb.calls[-1][1] == "removeWatch"


def test_geocoding_and_services():
    with FakeBridge("android") as fb:
        fb.on("Location", "geocode", lambda q: [{"latitude": 1.0, "longitude": 2.0, "altitude": 0.0, "accuracy": 0.0}])
        fb.on("Location", "reverseGeocode", lambda lat, lon: [{"city": "Berlin", "timezone": None}])
        fb.on("Location", "servicesEnabled", lambda: True)
        fb.on("Location", "providerStatus", lambda: {"locationServicesEnabled": True, "gpsAvailable": True})
        fb.on("Location", "enableNetworkProvider", lambda: None)
        assert run(Location.geocode_async("Berlin"))[0]["longitude"] == 2.0
        assert run(Location.reverseGeocodeAsync({"latitude": 52.5, "longitude": 13.4})) == [{"city": "Berlin",
                                                                                           "timezone": None}]
        assert fb.calls[-1] == ("Location", "reverseGeocode", (52.5, 13.4))
        for bad in ({"latitude": 91, "longitude": 0}, {"latitude": "x", "longitude": 0}, {}):
            with pytest.raises(ValueError):
                run(Location.reverse_geocode_async(bad))
        with pytest.raises(ValueError):
            run(Location.geocode_async("  "))
        assert run(Location.has_services_enabled_async()) is True
        assert run(Location.get_provider_status_async())["gpsAvailable"] is True
        run(Location.enable_network_provider_async())
        assert fb.calls[-1][1] == "enableNetworkProvider"
    with FakeBridge("ios") as fb:
        fb.on("Location", "servicesEnabled", lambda: True)
        run(Location.enable_network_provider_async())
        assert fb.calls == []


def test_task_apis_not_supported():
    with pytest.raises(NotSupportedError, match="task manager"):
        run(Location.start_location_updates_async("t"))
    with pytest.raises(NotSupportedError):
        run(Location.startGeofencingAsync("t", []))
    assert run(Location.has_started_location_updates_async("t")) is False
    assert run(Location.stop_geofencing_async("t")) is None
