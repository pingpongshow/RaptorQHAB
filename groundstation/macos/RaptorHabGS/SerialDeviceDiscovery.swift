//
//  SerialDeviceDiscovery.swift
//  RaptorHabGS
//
//  Identifies serial devices by USB vendor and product ID rather than by
//  guessing from the device path.
//
//  The app now has three classes of device on the bus -- the Heltec ground
//  modem, the Pi payload's USB gadget, and a Meshtastic node -- and the old
//  approach of accepting anything matching "usbserial", "usbmodem", or "cu."
//  matches essentially every serial device on the Mac, including Bluetooth
//  ports. Connecting the wrong one at the wrong baud rate produces silence
//  and no clue why.
//

import Foundation
import IOKit
import IOKit.serial

/// What a serial device most likely is.
enum SerialDeviceKind: String, CaseIterable {
    case raptorHabPayload = "RaptorHab Payload"
    case heltecModem      = "Heltec Modem"
    case meshtasticNode   = "Meshtastic Node"
    case genericUSB       = "USB Serial"
    case unknown          = "Unknown"

    /// Native baud rate for this device class. The old code hardcoded a
    /// single static rate for every connection, which cannot work when the
    /// modem runs at 921600 and a Meshtastic node at 115200.
    var defaultBaudRate: speed_t {
        switch self {
        case .raptorHabPayload: return 115200  // CDC-ACM ignores it, but be honest
        case .heltecModem:      return 921600
        case .meshtasticNode:   return 115200
        case .genericUSB, .unknown: return 115200
        }
    }

    var symbolName: String {
        switch self {
        case .raptorHabPayload: return "shippingbox.circle"
        case .heltecModem:      return "antenna.radiowaves.left.and.right"
        case .meshtasticNode:   return "point.3.connected.trianglepath.dotted"
        case .genericUSB:       return "cable.connector"
        case .unknown:          return "questionmark.circle"
        }
    }
}

struct SerialDevice: Identifiable, Hashable {
    let path: String
    let name: String
    let vendorID: Int?
    let productID: Int?
    let vendorName: String?
    let productName: String?
    let serialNumber: String?
    let kind: SerialDeviceKind

    var id: String { path }

    /// A description an operator can act on: what it is and where it is.
    var displayName: String {
        let device = URL(fileURLWithPath: path).lastPathComponent
        if let product = productName, !product.isEmpty {
            return "\(product) (\(device))"
        }
        return "\(kind.rawValue) (\(device))"
    }

    var identifierDescription: String {
        guard let vendorID, let productID else { return "no USB identity" }
        return String(format: "%04x:%04x", vendorID, productID)
    }
}

enum SerialDeviceDiscovery {

    // Known USB identities. Matching on these rather than on path substrings
    // is what lets the app tell three superficially identical /dev/cu.usbmodem
    // devices apart.
    private struct Signature {
        let vendorID: Int
        let productID: Int?      // nil matches any product from this vendor
        let kind: SerialDeviceKind
    }

    private static let signatures: [Signature] = [
        // The Pi gadget advertises the Linux Foundation composite ID. Several
        // gadgets share it, so the product string is the real discriminator
        // and is checked separately below.
        Signature(vendorID: 0x1d6b, productID: 0x0104, kind: .raptorHabPayload),

        // Espressif ESP32-S3 native USB, which is what the Heltec boards use.
        Signature(vendorID: 0x303a, productID: nil, kind: .heltecModem),

        // Common USB-serial bridges found on Meshtastic hardware.
        Signature(vendorID: 0x10c4, productID: 0xea60, kind: .meshtasticNode),  // CP2102
        Signature(vendorID: 0x1a86, productID: 0x7523, kind: .meshtasticNode),  // CH340
        Signature(vendorID: 0x1a86, productID: 0x55d4, kind: .meshtasticNode),  // CH9102
        Signature(vendorID: 0x0403, productID: nil, kind: .genericUSB),         // FTDI
        Signature(vendorID: 0x2e8a, productID: nil, kind: .meshtasticNode),     // RP2040
    ]

    /// Enumerate serial devices with whatever USB identity they expose.
    static func discover() -> [SerialDevice] {
        var devices: [SerialDevice] = []

        guard let matching = IOServiceMatching(kIOSerialBSDServiceValue) else {
            return devices
        }

        // Callout devices only. Matching dial-in (/dev/tty.*) as well would
        // list every port twice and, worse, opening a tty.* blocks waiting
        // for carrier detect.
        let dictionary = matching as NSMutableDictionary
        dictionary[kIOSerialBSDTypeKey] = kIOSerialBSDAllTypes

        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, matching, &iterator)
                == KERN_SUCCESS else {
            return devices
        }
        defer { IOObjectRelease(iterator) }

        while case let service = IOIteratorNext(iterator), service != 0 {
            defer { IOObjectRelease(service) }

            guard let path = stringProperty(service, kIOCalloutDeviceKey),
                  path.contains("/cu.") else { continue }

            // USB properties live on an ancestor of the serial node, not on
            // the node itself.
            let vendorID = ancestorNumberProperty(service, "idVendor")
            let productID = ancestorNumberProperty(service, "idProduct")
            let vendorName = ancestorStringProperty(service, "USB Vendor Name")
            let productName = ancestorStringProperty(service, "USB Product Name")
            let serialNumber = ancestorStringProperty(service, "USB Serial Number")

            devices.append(SerialDevice(
                path: path,
                name: stringProperty(service, kIOTTYDeviceKey) ?? path,
                vendorID: vendorID,
                productID: productID,
                vendorName: vendorName,
                productName: productName,
                serialNumber: serialNumber,
                kind: classify(vendorID: vendorID,
                               productID: productID,
                               productName: productName,
                               path: path)
            ))
        }

        return devices.sorted { $0.path < $1.path }
    }

    static func devices(of kind: SerialDeviceKind) -> [SerialDevice] {
        discover().filter { $0.kind == kind }
    }

    private static func classify(
        vendorID: Int?, productID: Int?, productName: String?, path: String
    ) -> SerialDeviceKind {
        // The product string is the strongest signal, and the only reliable
        // way to distinguish our gadget from any other Linux composite device
        // sharing the Linux Foundation vendor ID.
        if let productName {
            let lowered = productName.lowercased()
            if lowered.contains("raptorhab") { return .raptorHabPayload }
            if lowered.contains("meshtastic") || lowered.contains("t-beam")
                || lowered.contains("heltec lora") { return .meshtasticNode }
            if lowered.contains("heltec") { return .heltecModem }
        }

        if let vendorID {
            for signature in signatures where signature.vendorID == vendorID {
                if signature.productID == nil || signature.productID == productID {
                    return signature.kind
                }
            }
            return .genericUSB
        }

        // No USB identity at all: a Bluetooth port or a virtual device.
        return path.contains("usbmodem") || path.contains("usbserial")
            ? .genericUSB : .unknown
    }

    // MARK: - IOKit property helpers

    private static func stringProperty(_ service: io_object_t, _ key: String) -> String? {
        IORegistryEntryCreateCFProperty(service, key as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? String
    }

    /// Walk up the IORegistry looking for a property. USB metadata sits on the
    /// device node, several levels above the serial stream node.
    private static func ancestorProperty(_ service: io_object_t, _ key: String) -> Any? {
        var current = service
        var retainedByUs = false
        defer { if retainedByUs { IOObjectRelease(current) } }

        for _ in 0..<8 {
            if let value = IORegistryEntryCreateCFProperty(
                current, key as CFString, kCFAllocatorDefault, 0
            )?.takeRetainedValue() {
                return value
            }

            var parent: io_object_t = 0
            guard IORegistryEntryGetParentEntry(current, kIOServicePlane, &parent)
                    == KERN_SUCCESS else { return nil }

            if retainedByUs { IOObjectRelease(current) }
            current = parent
            retainedByUs = true
        }
        return nil
    }

    private static func ancestorStringProperty(_ service: io_object_t, _ key: String) -> String? {
        ancestorProperty(service, key) as? String
    }

    private static func ancestorNumberProperty(_ service: io_object_t, _ key: String) -> Int? {
        (ancestorProperty(service, key) as? NSNumber)?.intValue
    }
}
