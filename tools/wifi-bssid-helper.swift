import CoreLocation
import CoreWLAN
import Foundation

func outputPath() -> String? {
    let args = CommandLine.arguments
    guard let index = args.firstIndex(of: "--out"), index + 1 < args.count else {
        return nil
    }
    return args[index + 1]
}

var outputLines: [String] = []

final class LocationDelegate: NSObject, CLLocationManagerDelegate {
    let semaphore: DispatchSemaphore
    var latestLocation: CLLocation?

    init(semaphore: DispatchSemaphore) {
        self.semaphore = semaphore
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        semaphore.signal()
    }

    func locationManager(_ manager: CLLocationManager, didChangeAuthorization status: CLAuthorizationStatus) {
        semaphore.signal()
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        latestLocation = locations.last
        semaphore.signal()
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        semaphore.signal()
    }
}

func compact(_ value: String?) -> String {
    guard let value = value, !value.isEmpty else {
        return ""
    }
    return value.replacingOccurrences(of: " ", with: "")
}

func authStatusText(_ status: CLAuthorizationStatus) -> String {
    switch status {
    case .notDetermined:
        return "notDetermined"
    case .restricted:
        return "restricted"
    case .denied:
        return "denied"
    case .authorized:
        return "authorized"
    case .authorizedAlways:
        return "authorizedAlways"
    @unknown default:
        return "unknown"
    }
}

let semaphore = DispatchSemaphore(value: 0)
let manager = CLLocationManager()
let delegate = LocationDelegate(semaphore: semaphore)
manager.delegate = delegate

let status = manager.authorizationStatus
if status == .notDetermined {
    manager.requestWhenInUseAuthorization()
    let deadline = Date().addingTimeInterval(20)
    while manager.authorizationStatus == .notDetermined && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
    }
}

if manager.authorizationStatus == .authorizedAlways || manager.authorizationStatus == .authorized {
    manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    manager.requestLocation()
    let deadline = Date().addingTimeInterval(3)
    while delegate.latestLocation == nil && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
    }
}

let client = CWWiFiClient.shared()
guard let interfaces = client.interfaces(), !interfaces.isEmpty else {
    fputs("no_wifi_interfaces\n", stderr)
    exit(2)
}

var emitted = false
for interface in interfaces {
    guard interface.powerOn() else {
        continue
    }

    let ssid = compact(interface.ssid())
    let bssid = compact(interface.bssid())
    let channel = interface.wlanChannel()
    let channelText: String
    if let channel = channel {
        channelText = "\(channel.channelNumber)"
    } else {
        channelText = ""
    }

    var fields = [
        "interface=\(interface.interfaceName ?? "")",
        "location_auth=\(authStatusText(manager.authorizationStatus))",
        "ssid=\(ssid)",
        "bssid=\(bssid)",
        "rssi=\(interface.rssiValue())dBm",
        "noise=\(interface.noiseMeasurement())dBm",
        "tx_rate=\(interface.transmitRate())Mbps",
        "phy=\(interface.activePHYMode().rawValue)",
        "channel=\(channelText)"
    ]
    if let location = delegate.latestLocation {
        fields.append("latitude=\(String(format: "%.7f", location.coordinate.latitude))")
        fields.append("longitude=\(String(format: "%.7f", location.coordinate.longitude))")
        fields.append("location_accuracy_m=\(String(format: "%.1f", location.horizontalAccuracy))")
    }
    outputLines.append(fields.joined(separator: ","))
    emitted = true
}

if !emitted {
    fputs("no_powered_wifi_interfaces\n", stderr)
    exit(3)
}

let output = outputLines.joined(separator: "\n") + "\n"
if let path = outputPath() {
    try output.write(toFile: path, atomically: true, encoding: .utf8)
} else {
    print(output, terminator: "")
}
