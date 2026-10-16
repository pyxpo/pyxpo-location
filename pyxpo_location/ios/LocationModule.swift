import CoreLocation
import UIKit

/// expo-location on iOS: Core Location permissions, one-shot and watched locations, the compass, and CLGeocoder.
/// Every CLLocationManager here is made and used on the main thread, which also delivers its delegate calls.
final class LocationModule: PxModule {
    static let moduleName = "Location"

    private let manager = CLLocationManager()
    private var asks: [PermissionAsk] = []
    private var shots: [OneShot] = []
    private var watches: [Int: Watch] = [:]

    required init() {}

    deinit { watches.values.forEach { $0.stop() } }

    func call(_ ctx: PxModuleContext, _ method: String, _ args: [Any], _ reply: @escaping PxReply) {
        switch method {
        case "getPermission":
            reply(Self.permission(manager.authorizationStatus, background: args.first as? Bool ?? false), nil)
        case "requestPermission":
            requestPermission(background: args.first as? Bool ?? false, reply)
        case "getCurrentPosition":
            guard authorized(reply) else { return }
            let opts = args.first as? [String: Any] ?? [:]
            let shot = OneShot(accuracy: Self.accuracy(opts["accuracy"]))
            shots.append(shot)
            shot.start { [weak self, weak shot] value, error in
                self?.shots.removeAll { $0 === shot }
                reply(value, error)
            }
        case "getLastKnown":
            guard authorized(reply) else { return }
            reply(manager.location.map(Self.location) ?? NSNull(), nil)
        case "watchPosition", "watchHeading":
            guard let id = (args.first as? NSNumber)?.intValue else { reply(nil, "missing watch id"); return }
            if method == "watchHeading" {
                guard CLLocationManager.headingAvailable() else { reply(nil, "Heading is not available on this device"); return }
            } else {
                guard authorized(reply) else { return }
            }
            let w = Watch(id: id, event: method == "watchHeading" ? "Location.heading" : "Location.position", ctx: ctx)
            watches[id]?.stop()
            watches[id] = w
            if method == "watchHeading" {
                w.startHeading()
            } else {
                let opts = args.count > 1 ? args[1] as? [String: Any] ?? [:] : [:]
                w.startPosition(accuracy: Self.accuracy(opts["accuracy"]),
                                distance: (opts["distanceInterval"] as? NSNumber)?.doubleValue ?? 0)
            }
            reply(nil, nil)
        case "removeWatch":
            if let id = (args.first as? NSNumber)?.intValue { watches.removeValue(forKey: id)?.stop() }
            reply(nil, nil)
        case "geocode":
            CLGeocoder().geocodeAddressString(args.first as? String ?? "") { marks, error in
                if let e = error, !Self.noResult(e) { reply(nil, "Geocoding failed: \(e.localizedDescription)"); return }
                reply((marks ?? []).compactMap { $0.location }.map { l in
                    ["latitude": l.coordinate.latitude, "longitude": l.coordinate.longitude,
                     "altitude": l.altitude, "accuracy": l.horizontalAccuracy] as [String: Any]
                }, nil)
            }
        case "reverseGeocode":
            let lat = (args.first as? NSNumber)?.doubleValue ?? 0
            let lon = (args.count > 1 ? args[1] as? NSNumber : nil)?.doubleValue ?? 0
            CLGeocoder().reverseGeocodeLocation(CLLocation(latitude: lat, longitude: lon)) { marks, error in
                if let e = error, !Self.noResult(e) { reply(nil, "Reverse geocoding failed: \(e.localizedDescription)"); return }
                reply((marks ?? []).map(Self.address), nil)
            }
        case "servicesEnabled":
            // Apple warns against calling this on the main thread: it can block.
            DispatchQueue.global().async { reply(CLLocationManager.locationServicesEnabled(), nil) }
        case "providerStatus":
            let modes = Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String] ?? []
            DispatchQueue.global().async {
                reply(["locationServicesEnabled": CLLocationManager.locationServicesEnabled(),
                       "backgroundModeEnabled": modes.contains("location")], nil)
            }
        default:
            reply(nil, "unknown native call Location.\(method)")
        }
    }

    // permissions

    private static let askedAlwaysKey = "pyxpo.location.askedAlways"

    private func authorized(_ reply: PxReply) -> Bool {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: return true
        default:
            reply(nil, "Not authorized to use location services")
            return false
        }
    }

    static func permission(_ s: CLAuthorizationStatus, background: Bool) -> [String: Any] {
        switch s {
        case .authorizedAlways:
            return ["status": "granted", "canAskAgain": true, "scope": "always"]
        case .authorizedWhenInUse:
            if !background { return ["status": "granted", "canAskAgain": true, "scope": "whenInUse"] }
            // iOS shows the "Always" prompt once; after that the app can only send the user to Settings.
            let asked = UserDefaults.standard.bool(forKey: askedAlwaysKey)
            return ["status": asked ? "denied" : "undetermined", "canAskAgain": !asked, "scope": "whenInUse"]
        case .notDetermined:
            return ["status": "undetermined", "canAskAgain": true, "scope": "none"]
        default:
            return ["status": "denied", "canAskAgain": false, "scope": "none"]
        }
    }

    private func requestPermission(background: Bool, _ reply: @escaping PxReply) {
        let now = manager.authorizationStatus
        let answer = { [weak self] in
            reply(Self.permission(self?.manager.authorizationStatus ?? .notDetermined, background: background), nil)
        }
        let askAlways = background && (now == .notDetermined || now == .authorizedWhenInUse)
            && !UserDefaults.standard.bool(forKey: Self.askedAlwaysKey)
        guard now == .notDetermined || askAlways else { answer(); return }
        if background { UserDefaults.standard.set(true, forKey: Self.askedAlwaysKey) }
        let ask = PermissionAsk(from: now)
        asks.append(ask)
        ask.start(always: background) { [weak self, weak ask] in
            self?.asks.removeAll { $0 === ask }
            answer()
        }
    }

    // values

    static func accuracy(_ raw: Any?) -> CLLocationAccuracy {
        switch (raw as? NSNumber)?.intValue ?? 3 {
        case 1: return kCLLocationAccuracyThreeKilometers
        case 2: return kCLLocationAccuracyKilometer
        case 3: return kCLLocationAccuracyHundredMeters
        case 4: return kCLLocationAccuracyNearestTenMeters
        case 5: return kCLLocationAccuracyBest
        default: return kCLLocationAccuracyBestForNavigation
        }
    }

    static func location(_ l: CLLocation) -> [String: Any] {
        func valid(_ v: Double, _ ok: Bool) -> Any { ok ? v : NSNull() }
        return [
            "coords": [
                "latitude": l.coordinate.latitude,
                "longitude": l.coordinate.longitude,
                "altitude": valid(l.altitude, l.verticalAccuracy >= 0),
                "accuracy": valid(l.horizontalAccuracy, l.horizontalAccuracy >= 0),
                "altitudeAccuracy": valid(l.verticalAccuracy, l.verticalAccuracy >= 0),
                "heading": valid(l.course, l.course >= 0),
                "speed": valid(l.speed, l.speed >= 0),
            ] as [String: Any],
            "timestamp": l.timestamp.timeIntervalSince1970 * 1000,
        ]
    }

    private static func address(_ p: CLPlacemark) -> [String: Any] {
        func s(_ v: String?) -> Any { v ?? NSNull() }
        return [
            "name": s(p.name), "street": s(p.thoroughfare), "streetNumber": s(p.subThoroughfare),
            "district": s(p.subLocality), "city": s(p.locality), "subregion": s(p.subAdministrativeArea),
            "region": s(p.administrativeArea), "postalCode": s(p.postalCode), "country": s(p.country),
            "isoCountryCode": s(p.isoCountryCode), "timezone": s(p.timeZone?.identifier), "formattedAddress": NSNull(),
        ]
    }

    private static func noResult(_ e: Error) -> Bool {
        (e as? CLError)?.code == .geocodeFoundNoResult
    }
}

/// Waits for the answer to a permission prompt. The delegate fires when the status changes; when the user keeps
/// "While Using" at the "Always" prompt nothing changes, so coming back to the foreground also ends the wait,
/// and so does a short timer when no prompt appeared at all.
private final class PermissionAsk: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private let from: CLAuthorizationStatus
    private var done: (() -> Void)?
    private var left = false
    private var tokens: [NSObjectProtocol] = []

    init(from: CLAuthorizationStatus) { self.from = from }

    func start(always: Bool, _ done: @escaping () -> Void) {
        self.done = done
        let nc = NotificationCenter.default
        tokens.append(nc.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
            self?.left = true
        })
        tokens.append(nc.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            guard self?.left == true else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self?.finish() }
        })
        manager.delegate = self
        if always { manager.requestAlwaysAuthorization() } else { manager.requestWhenInUseAuthorization() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            if self?.left == false { self?.finish() }
        }
    }

    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        // Also called once when the delegate is set, with the unchanged status.
        if m.authorizationStatus != from { finish() }
    }

    private func finish() {
        guard let d = done else { return }
        done = nil
        tokens.forEach { NotificationCenter.default.removeObserver($0) }
        tokens = []
        manager.delegate = nil
        d()
    }
}

/// One fresh location through `requestLocation`.
private final class OneShot: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var reply: PxReply?

    init(accuracy: CLLocationAccuracy) {
        super.init()
        manager.desiredAccuracy = accuracy
    }

    func start(_ reply: @escaping PxReply) {
        self.reply = reply
        manager.delegate = self
        manager.requestLocation()
    }

    func locationManager(_ m: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let l = locations.last else { return }
        finish(LocationModule.location(l), nil)
    }

    func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
        let code = (error as? CLError)?.code
        finish(nil, code == .denied ? "Not authorized to use location services"
                                    : "Current location is unavailable: \(error.localizedDescription)")
    }

    private func finish(_ value: Any?, _ error: String?) {
        guard let r = reply else { return }
        reply = nil
        manager.delegate = nil
        r(value, error)
    }
}

/// A `watchPositionAsync` or `watchHeadingAsync` subscription; sends events tagged with its id.
private final class Watch: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private let id: Int
    private let event: String
    private weak var ctx: PxModuleContext?

    init(id: Int, event: String, ctx: PxModuleContext) {
        self.id = id
        self.event = event
        self.ctx = ctx
        super.init()
        manager.delegate = self
    }

    func startPosition(accuracy: CLLocationAccuracy, distance: Double) {
        manager.desiredAccuracy = accuracy
        manager.distanceFilter = distance > 0 ? distance : kCLDistanceFilterNone
        manager.startUpdatingLocation()
    }

    func startHeading() {
        manager.headingFilter = 1
        manager.startUpdatingHeading()
    }

    func stop() {
        manager.stopUpdatingLocation()
        manager.stopUpdatingHeading()
        manager.delegate = nil
    }

    func locationManager(_ m: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        for l in locations { ctx?.emit("Location.position", ["id": id, "location": LocationModule.location(l)]) }
    }

    func locationManager(_ m: CLLocationManager, didUpdateHeading h: CLHeading) {
        ctx?.emit("Location.heading", ["id": id, "heading": [
            "trueHeading": h.trueHeading, "magHeading": h.magneticHeading, "accuracy": h.headingAccuracy,
        ] as [String: Any]])
    }

    func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
        let code = (error as? CLError)?.code
        // `locationUnknown` is temporary: Core Location keeps trying.
        if code == .locationUnknown { return }
        ctx?.emit(event, ["id": id, "error": code == .denied ? "Not authorized to use location services"
                                                           : error.localizedDescription])
    }
}
