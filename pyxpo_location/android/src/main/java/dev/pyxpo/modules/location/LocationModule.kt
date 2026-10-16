package dev.pyxpo.modules.location

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.GeomagneticField
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Address
import android.location.Geocoder
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.Looper
import android.provider.Settings
import dev.pyxpo.moduleapi.PxModule
import dev.pyxpo.moduleapi.PxModuleContext
import dev.pyxpo.moduleapi.PxReply
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale
import kotlin.math.abs

/**
 * expo-location on Android with the platform LocationManager (no Google Play services): permissions, one-shot and
 * watched locations, a compass from the accelerometer and magnetometer, and Geocoder.
 */
class LocationModule : PxModule() {
    override val name = "Location"

    private val watches = HashMap<Int, () -> Unit>()

    override fun call(ctx: PxModuleContext, method: String, args: JSONArray, reply: PxReply) {
        val a = ctx.activity
        try {
            when (method) {
                "getPermission" -> reply(if (args.optBoolean(0)) background(a) else foreground(a), null)
                "requestPermission" -> if (args.optBoolean(0)) requestBackground(ctx, reply) else requestForeground(ctx, reply)
                "getCurrentPosition" -> {
                    if (!authorized(a)) { reply(null, DENIED); return }
                    current(a, args.optJSONObject(0)?.optInt("accuracy", 3) ?: 3, reply)
                }
                "getLastKnown" -> {
                    if (!authorized(a)) { reply(null, DENIED); return }
                    val lm = lm(a)
                    val best = lm.getProviders(true).mapNotNull { p ->
                        try { lm.getLastKnownLocation(p) } catch (_: SecurityException) { null }
                    }.maxByOrNull { it.time }
                    reply(best?.let(::locationMap), null)
                }
                "watchPosition" -> {
                    if (!authorized(a)) { reply(null, DENIED); return }
                    val id = args.getInt(0)
                    watches.remove(id)?.invoke()
                    watches[id] = watchPosition(ctx, id, args.optJSONObject(1) ?: JSONObject())
                    reply(null, null)
                }
                "watchHeading" -> {
                    val id = args.getInt(0)
                    watches.remove(id)?.invoke()
                    val stop = watchHeading(ctx, id) ?: run { reply(null, "Heading is not available on this device"); return }
                    watches[id] = stop
                    reply(null, null)
                }
                "removeWatch" -> { watches.remove(args.optInt(0))?.invoke(); reply(null, null) }
                "geocode" -> { val q = args.optString(0); geocode(a, reply, "Geocoding failed", ::coords) { g, l -> fromName(g, l, q) } }
                "reverseGeocode" -> {
                    val lat = args.getDouble(0)
                    val lon = args.getDouble(1)
                    geocode(a, reply, "Reverse geocoding failed", ::address) { g, l -> fromLocation(g, l, lat, lon) }
                }
                "servicesEnabled" -> reply(servicesEnabled(lm(a)), null)
                "providerStatus" -> {
                    val lm = lm(a)
                    val on = servicesEnabled(lm)
                    reply(mapOf("locationServicesEnabled" to on, "backgroundModeEnabled" to on,
                        "gpsAvailable" to enabled(lm, LocationManager.GPS_PROVIDER),
                        "networkAvailable" to enabled(lm, LocationManager.NETWORK_PROVIDER),
                        "passiveAvailable" to enabled(lm, LocationManager.PASSIVE_PROVIDER)), null)
                }
                "enableNetworkProvider" -> {
                    val lm = lm(a)
                    if (enabled(lm, LocationManager.NETWORK_PROVIDER)) { reply(null, null); return }
                    if (LocationManager.NETWORK_PROVIDER !in lm.allProviders) { reply(null, "This device has no network location provider"); return }
                    ctx.startForResult(Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS)) { _, _ ->
                        if (enabled(lm, LocationManager.NETWORK_PROVIDER)) reply(null, null)
                        else reply(null, "Network location was not turned on")
                    }
                }
                else -> reply(null, "unknown native call Location.$method")
            }
        } catch (e: SecurityException) {
            reply(null, DENIED)
        }
    }

    // permissions

    private fun prefs(a: Activity) = a.getSharedPreferences("pyxpo.permissions", Context.MODE_PRIVATE)

    private fun granted(a: Activity, p: String) = a.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun authorized(a: Activity) = granted(a, FINE) || granted(a, COARSE)

    /** Expo status: never asked is `undetermined`; denied can be asked again until Android stops showing the dialog. */
    private fun status(a: Activity, perms: List<String>): Pair<String, Boolean> {
        if (perms.any { granted(a, it) }) return "granted" to true
        if (perms.any { !prefs(a).getBoolean(it, false) }) return "undetermined" to true
        return "denied" to perms.any { a.shouldShowRequestPermissionRationale(it) }
    }

    private fun foreground(a: Activity): Map<String, Any> {
        val (s, again) = status(a, listOf(FINE, COARSE))
        val accuracy = when { granted(a, FINE) -> "fine"; granted(a, COARSE) -> "coarse"; else -> "none" }
        return mapOf("status" to s, "canAskAgain" to again, "accuracy" to accuracy)
    }

    private fun background(a: Activity): Map<String, Any> {
        // Before Android 10 the foreground permission covers the background.
        if (Build.VERSION.SDK_INT < 29) return foreground(a) - "accuracy"
        val (s, again) = status(a, listOf(BACKGROUND))
        return mapOf("status" to s, "canAskAgain" to again)
    }

    private fun requestForeground(ctx: PxModuleContext, reply: PxReply) {
        val a = ctx.activity
        val now = foreground(a)
        if (granted(a, FINE) || now["canAskAgain"] != true) { reply(now, null); return }
        val perms = arrayOf(FINE, COARSE)
        ctx.requestPermissions(perms) { _ ->
            prefs(a).edit().apply { perms.forEach { putBoolean(it, true) } }.apply()
            reply(foreground(a), null)
        }
    }

    private fun requestBackground(ctx: PxModuleContext, reply: PxReply) {
        val a = ctx.activity
        if (Build.VERSION.SDK_INT < 29) { requestForeground(ctx) { v, e -> reply((v as? Map<*, *>)?.filterKeys { it != "accuracy" }, e) }; return }
        if (!declared(a, BACKGROUND)) {
            reply(null, "Add \"$BACKGROUND\" to [tool.pyxpo.android] permissions to ask for background location")
            return
        }
        if (!authorized(a)) { reply(null, "Ask for the foreground location permission first"); return }
        val now = background(a)
        if (now["status"] == "granted" || now["canAskAgain"] != true) { reply(now, null); return }
        ctx.requestPermissions(arrayOf(BACKGROUND)) { _ ->
            prefs(a).edit().putBoolean(BACKGROUND, true).apply()
            reply(background(a), null)
        }
    }

    private fun declared(a: Activity, perm: String): Boolean = try {
        @Suppress("DEPRECATION")
        val info = a.packageManager.getPackageInfo(a.packageName, PackageManager.GET_PERMISSIONS)
        info.requestedPermissions?.contains(perm) == true
    } catch (_: PackageManager.NameNotFoundException) { false }

    // locations

    private fun lm(c: Context) = c.getSystemService(Context.LOCATION_SERVICE) as LocationManager

    private fun enabled(lm: LocationManager, p: String) = try { lm.isProviderEnabled(p) } catch (_: Exception) { false }

    private fun servicesEnabled(lm: LocationManager) =
        if (Build.VERSION.SDK_INT >= 28) lm.isLocationEnabled
        else enabled(lm, LocationManager.GPS_PROVIDER) || enabled(lm, LocationManager.NETWORK_PROVIDER)

    /** The enabled provider that best fits `accuracy` (1 lowest .. 6 best for navigation). GPS needs the fine permission. */
    private fun provider(a: Activity, accuracy: Int): String? {
        val lm = lm(a)
        val fused = if (Build.VERSION.SDK_INT >= 31) listOf(LocationManager.FUSED_PROVIDER) else emptyList()
        val gps = listOf(LocationManager.GPS_PROVIDER)
        val net = listOf(LocationManager.NETWORK_PROVIDER)
        val order = when {
            accuracy >= 4 -> gps + fused + net
            accuracy == 3 -> fused + net + gps
            else -> net + fused + gps
        }
        return order.firstOrNull { p ->
            p in lm.allProviders && enabled(lm, p) && (p != LocationManager.GPS_PROVIDER || granted(a, FINE))
        }
    }

    private fun current(a: Activity, accuracy: Int, reply: PxReply) {
        val p = provider(a, accuracy) ?: run { reply(null, "Location services are disabled"); return }
        val lm = lm(a)
        val done = { loc: Location? ->
            if (loc == null) reply(null, "Current location is unavailable") else reply(locationMap(loc), null)
        }
        if (Build.VERSION.SDK_INT >= 30) {
            lm.getCurrentLocation(p, CancellationSignal(), a.mainExecutor) { done(it) }
        } else {
            @Suppress("DEPRECATION")
            lm.requestSingleUpdate(p, object : Listener() {
                override fun onLocationChanged(location: Location) = done(location)
            }, Looper.getMainLooper())
        }
    }

    private fun watchPosition(ctx: PxModuleContext, id: Int, opts: JSONObject): () -> Unit {
        val a = ctx.activity
        val lm = lm(a)
        val p = provider(a, opts.optInt("accuracy", 3)) ?: throw IllegalStateException("Location services are disabled")
        val listener = object : Listener() {
            override fun onLocationChanged(location: Location) =
                ctx.emit("Location.position", mapOf("id" to id, "location" to locationMap(location)))

            override fun onProviderDisabled(provider: String) =
                ctx.emit("Location.position", mapOf("id" to id, "error" to "Location provider $provider was turned off"))
        }
        lm.requestLocationUpdates(p, opts.optDouble("timeInterval", 0.0).toLong(),
            opts.optDouble("distanceInterval", 0.0).toFloat(), listener, Looper.getMainLooper())
        return { lm.removeUpdates(listener) }
    }

    /** Android before 11 has no default bodies for these, and calls them. */
    private abstract class Listener : LocationListener {
        override fun onProviderEnabled(provider: String) {}
        override fun onProviderDisabled(provider: String) {}
        @Deprecated("Deprecated in Java")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    }

    private fun locationMap(l: Location): Map<String, Any> = mapOf(
        "coords" to mapOf(
            "latitude" to l.latitude,
            "longitude" to l.longitude,
            "altitude" to if (l.hasAltitude()) l.altitude else JSONObject.NULL,
            "accuracy" to if (l.hasAccuracy()) l.accuracy.toDouble() else JSONObject.NULL,
            "altitudeAccuracy" to if (Build.VERSION.SDK_INT >= 26 && l.hasVerticalAccuracy()) l.verticalAccuracyMeters.toDouble() else JSONObject.NULL,
            "heading" to if (l.hasBearing()) l.bearing.toDouble() else JSONObject.NULL,
            "speed" to if (l.hasSpeed()) l.speed.toDouble() else JSONObject.NULL,
        ),
        "timestamp" to l.time,
        "mocked" to if (Build.VERSION.SDK_INT >= 31) l.isMock else @Suppress("DEPRECATION") l.isFromMockProvider,
    )

    // heading

    private fun watchHeading(ctx: PxModuleContext, id: Int): (() -> Unit)? {
        val a = ctx.activity
        val sm = a.getSystemService(Context.SENSOR_SERVICE) as SensorManager
        val accel = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER) ?: return null
        val magnet = sm.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD) ?: return null
        val listener = object : SensorEventListener {
            var gravity: FloatArray? = null
            var field: FloatArray? = null
            var accuracy = 0
            var last = Float.NaN
            var lastAt = 0L

            override fun onSensorChanged(e: SensorEvent) {
                if (e.sensor.type == Sensor.TYPE_ACCELEROMETER) gravity = e.values.clone()
                else { field = e.values.clone(); accuracy = e.accuracy }
                val g = gravity ?: return
                val f = field ?: return
                val r = FloatArray(9)
                if (!SensorManager.getRotationMatrix(r, null, g, f)) return
                val o = FloatArray(3)
                SensorManager.getOrientation(r, o)
                val mag = (Math.toDegrees(o[0].toDouble()).toFloat() + 360f) % 360f
                val now = System.currentTimeMillis()
                // Like Expo: only real changes, at most every 50 ms.
                if (!last.isNaN() && (delta(mag, last) < 1f || now - lastAt < 50)) return
                last = mag
                lastAt = now
                ctx.emit("Location.heading", mapOf("id" to id, "heading" to mapOf(
                    "trueHeading" to trueHeading(a, mag), "magHeading" to mag.toDouble(), "accuracy" to accuracy)))
            }

            override fun onAccuracyChanged(sensor: Sensor, acc: Int) {
                if (sensor.type == Sensor.TYPE_MAGNETIC_FIELD) accuracy = acc
            }
        }
        sm.registerListener(listener, accel, SensorManager.SENSOR_DELAY_UI)
        sm.registerListener(listener, magnet, SensorManager.SENSOR_DELAY_UI)
        return { sm.unregisterListener(listener) }
    }

    private fun delta(x: Float, y: Float): Float { val d = abs(x - y) % 360f; return if (d > 180f) 360f - d else d }

    /** Magnetic heading corrected by the declination at the last known location; -1 without one. */
    private fun trueHeading(a: Activity, mag: Float): Double {
        if (!authorized(a)) return -1.0
        val lm = lm(a)
        val loc = try {
            lm.getProviders(true).mapNotNull { lm.getLastKnownLocation(it) }.maxByOrNull { it.time }
        } catch (_: SecurityException) { null } ?: return -1.0
        val field = GeomagneticField(loc.latitude.toFloat(), loc.longitude.toFloat(), loc.altitude.toFloat(), System.currentTimeMillis())
        return ((mag + field.declination + 360f) % 360f).toDouble()
    }

    // geocoding

    /** Runs `lookup` with the platform Geocoder (async on Android 13+, a worker thread before) and replies with `map`. */
    private fun geocode(a: Activity, reply: PxReply, failed: String, map: (Address) -> Map<String, Any>?,
                        lookup: (Geocoder, Geocoder.GeocodeListener?) -> List<Address>?) {
        if (!Geocoder.isPresent()) { reply(null, "Geocoder service is not available on this device"); return }
        val g = Geocoder(a, Locale.getDefault())
        val done = { list: List<Address> -> reply(list.mapNotNull(map), null) }
        if (Build.VERSION.SDK_INT >= 33) {
            lookup(g, object : Geocoder.GeocodeListener {
                override fun onGeocode(list: MutableList<Address>) = done(list)
                override fun onError(message: String?) = reply(null, "$failed: ${message ?: "unknown error"}")
            })
        } else Thread {
            try { done(lookup(g, null) ?: emptyList()) }
            catch (e: Exception) { reply(null, "$failed: ${e.message}") }
        }.start()
    }

    @Suppress("DEPRECATION")
    private fun fromName(g: Geocoder, l: Geocoder.GeocodeListener?, address: String): List<Address>? =
        if (l != null && Build.VERSION.SDK_INT >= 33) { g.getFromLocationName(address, 5, l); null }
        else g.getFromLocationName(address, 5)

    @Suppress("DEPRECATION")
    private fun fromLocation(g: Geocoder, l: Geocoder.GeocodeListener?, lat: Double, lon: Double): List<Address>? =
        if (l != null && Build.VERSION.SDK_INT >= 33) { g.getFromLocation(lat, lon, 5, l); null }
        else g.getFromLocation(lat, lon, 5)

    private fun coords(ad: Address): Map<String, Any>? =
        if (!ad.hasLatitude() || !ad.hasLongitude()) null
        else mapOf("latitude" to ad.latitude, "longitude" to ad.longitude, "altitude" to 0.0, "accuracy" to 0.0)

    private fun address(ad: Address): Map<String, Any> {
        fun s(v: String?): Any = v ?: JSONObject.NULL
        val formatted = (0..ad.maxAddressLineIndex).mapNotNull { ad.getAddressLine(it) }.joinToString(", ")
        return mapOf(
            "name" to s(ad.featureName), "street" to s(ad.thoroughfare), "streetNumber" to s(ad.subThoroughfare),
            "district" to s(ad.subLocality), "city" to s(ad.locality), "subregion" to s(ad.subAdminArea),
            "region" to s(ad.adminArea), "postalCode" to s(ad.postalCode), "country" to s(ad.countryName),
            "isoCountryCode" to s(ad.countryCode), "timezone" to JSONObject.NULL,
            "formattedAddress" to s(formatted.ifEmpty { null }),
        )
    }

    companion object {
        private const val FINE = Manifest.permission.ACCESS_FINE_LOCATION
        private const val COARSE = Manifest.permission.ACCESS_COARSE_LOCATION
        private const val BACKGROUND = "android.permission.ACCESS_BACKGROUND_LOCATION"
        private const val DENIED = "Not authorized to use location services"
    }
}
