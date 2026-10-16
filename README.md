# pyxpo-location

Device location, compass heading and geocoding for [pyxpo](https://github.com/pyxpo/pyxpo) apps. The API follows
[expo-location](https://docs.expo.dev/versions/latest/sdk/location/). iOS uses Core Location. Android uses the
platform `LocationManager` (no Google Play services), the accelerometer and magnetometer for the heading, and
`Geocoder` for geocoding.

```sh
pip install pyxpo-location
```

```toml
[tool.pyxpo]
modules = ["location"]

# Optional: the text of the iOS permission prompts.
[tool.pyxpo.module_options.location]
locationWhenInUsePermission = "Show places near you."
```

```python
import pyxpo_location as Location

perm = await Location.request_foreground_permissions_async()
if perm["granted"]:
    loc = await Location.get_current_position_async(accuracy=Location.Accuracy.HIGH)
    print(loc["coords"]["latitude"], loc["coords"]["longitude"])

sub = await Location.watch_position_async({"accuracy": Location.Accuracy.BALANCED, "distanceInterval": 10},
                                          lambda loc: print(loc["coords"]))
sub.remove()

places = await Location.reverse_geocode_async({"latitude": 48.8584, "longitude": 2.2945})
```

## Functions

| Function | Notes |
| --- | --- |
| `request_foreground_permissions_async()` / `get_foreground_permissions_async()` | Expo's permission response, plus `ios: {"scope"}` (`"whenInUse"`, `"always"`, `"none"`) or `android: {"accuracy"}` (`"fine"`, `"coarse"`, `"none"`). The Android user may allow only approximate location. |
| `request_background_permissions_async()` / `get_background_permissions_async()` | Ask for the foreground permission first. Android needs `ACCESS_BACKGROUND_LOCATION` in `[tool.pyxpo.android] permissions`. |
| `use_foreground_permissions()` / `use_background_permissions()` | Hooks: `(response, request, get)`. |
| `get_current_position_async(accuracy=...)` | A fresh location: `{"coords": {"latitude", "longitude", "altitude", "accuracy", "altitudeAccuracy", "heading", "speed"}, "timestamp", "mocked"}`. Unknown values are None. `mocked` is Android only. |
| `get_last_known_position_async(max_age=None, required_accuracy=None)` | The cached location, or None. |
| `watch_position_async(options, callback, error_handler=None)` | Options: `accuracy`, `time_interval` (Android, ms), `distance_interval` (m). Returns a subscription; call `.remove()`. |
| `watch_heading_async(callback)` / `get_heading_async()` | `{"trueHeading", "magHeading", "accuracy"}` in degrees. `trueHeading` is -1 until the location is known. Raises on devices without a compass (and on the iOS Simulator). |
| `geocode_async(address)` | `[{"latitude", "longitude", "altitude", "accuracy"}]`. Needs the network. |
| `reverse_geocode_async({"latitude", "longitude"})` | Addresses: `name`, `street`, `streetNumber`, `district`, `city`, `subregion`, `region`, `postalCode`, `country`, `isoCountryCode`, `timezone`, `formattedAddress`. |
| `has_services_enabled_async()` | Whether location services are on. |
| `get_provider_status_async()` | `locationServicesEnabled`, `backgroundModeEnabled`; on Android also `gpsAvailable`, `networkAvailable`, `passiveAvailable`. |
| `enable_network_provider_async()` | Android: opens the location settings when network location is off. |

`Accuracy` runs from `LOWEST` (about 3 km) to `BEST_FOR_NAVIGATION`. camelCase names work too
(`getCurrentPositionAsync`, `{"distanceInterval": 10}`).

## Not supported yet

Background updates (`start_location_updates_async`) and geofencing (`start_geofencing_async`) need a background
task manager, which pyxpo does not have yet. They raise `NotSupportedError`; `is_background_location_available_async()`
returns False. Use `watch_position_async` while the app is open.

## Platforms

iOS 15+ and Android 7+ (API 24). On desktop the functions raise `NotSupportedError`, except
`has_services_enabled_async()`, which returns False.
