// Contract smoke for the iOS/Swift SDK.
//
// Run from the repo root against a seeded backend (see backend/scripts/seed_sdk_contract.py):
//
//     cd sdk/ios && CONTROLVAR_API_KEY=... swift run contract-smoke
//
// Environment: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY
// (required), CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default
// sdk_contract_flag), CONTRACT_USER_ID (default smoke-<uuid>).
//
// Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and
// exits 1. Only the SDK's public API is used.

import Foundation
import Controlvar

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("ios contract smoke: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func env(_ name: String, default defaultValue: String) -> String {
    let value = ProcessInfo.processInfo.environment[name] ?? ""
    return value.isEmpty ? defaultValue : value
}

func jsonString(_ value: String) -> String {
    var escaped = ""
    for scalar in value.unicodeScalars {
        switch scalar {
        case "\"": escaped += "\\\""
        case "\\": escaped += "\\\\"
        case "\n": escaped += "\\n"
        case "\r": escaped += "\\r"
        case "\t": escaped += "\\t"
        default:
            if scalar.value < 0x20 {
                escaped += String(format: "\\u%04x", scalar.value)
            } else {
                escaped.unicodeScalars.append(scalar)
            }
        }
    }
    return "\"\(escaped)\""
}

let apiURL = env("CONTROLVAR_API_URL", default: env("EXPERIMENTLY_API_URL", default: "http://localhost:8000"))
let apiKey = env("CONTROLVAR_API_KEY", default: env("EXPERIMENTLY_API_KEY", default: ""))
guard !apiKey.isEmpty else { fail("CONTROLVAR_API_KEY is required") }
let experimentKey = env("CONTRACT_EXPERIMENT_KEY", default: "sdk_contract_ab")
let flagKey = env("CONTRACT_FLAG_KEY", default: "sdk_contract_flag")
let userId = env("CONTRACT_USER_ID", default: "smoke-\(UUID().uuidString.lowercased())")

// No UserDefaults writes from the smoke run.
let config = SdkConfig(baseURL: apiURL, apiKey: apiKey, enableOfflineFallback: false)
let user = User(id: userId, attributes: ["platform": "ios", "sdk": "swift"])

// `client` keeps the assignment and flag cached (needed for the fan-out step); `freshClient`
// starts with an empty cache so the second assignment is a real server round-trip.
let client = ExperimentationClient(config: config)
let freshClient = ExperimentationClient(config: config)

do {
    // 1. Assign twice → identical (sticky on the server).
    let first = try await client.getAssignment(experimentKey, user: user)
    let second = try await freshClient.getAssignment(experimentKey, user: user)
    guard ["control", "treatment"].contains(first.variantName) else {
        fail("unexpected variant_name '\(first.variantName)' for \(experimentKey)")
    }
    let sticky = first.variantName == second.variantName && first.variantId == second.variantId
    guard sticky else {
        fail("assignment not sticky: \(first.variantName)/\(first.variantId ?? "-") vs \(second.variantName)/\(second.variantId ?? "-")")
    }

    // 2. Evaluate the flag → enabled is a Bool (seeded flag is 100% on).
    let flag = try await client.evaluateFlag(flagKey, user: user)

    // 3. Track `purchase` with a value and the experiment key → /tracking/track.
    let trackOK = await client.trackWithStatus(TrackEvent(
        userId: userId,
        eventName: "purchase",
        properties: ["sdk": "ios", "currency": "USD"],
        experimentKey: experimentKey,
        value: 12.5
    ))
    guard trackOK else { fail("track purchase failed") }

    // 4. Track `page_view` without a key → fans out to the cached assignment + flag.
    guard client.getAssignments(for: userId).count == 1, client.getEvaluatedFlags(for: userId).count == 1 else {
        fail("expected one cached assignment and one cached flag before fan-out")
    }
    let fanoutOK = await client.trackWithStatus(TrackEvent(
        userId: userId,
        eventName: "page_view",
        properties: ["page": "/smoke"]
    ))
    guard fanoutOK else { fail("fan-out track failed") }

    // The SDK exposes a batch call, so also send a 2-event batch.
    let batchOK = await client.trackBatch([
        TrackEvent(userId: userId, eventName: "add_to_cart", experimentKey: experimentKey, value: 1),
        TrackEvent(userId: userId, eventName: "checkout", featureFlagKey: flagKey),
    ])
    guard batchOK else { fail("batch track failed") }

    let line = "{\"sdk\":\"ios\",\"assign\":{\"variant_name\":\(jsonString(first.variantName)),"
        + "\"is_control\":\(first.isControl),\"sticky\":\(sticky)},"
        + "\"flag\":{\"enabled\":\(flag.enabled)},"
        + "\"track\":{\"ok\":\(trackOK)},"
        + "\"fanout\":{\"ok\":\(fanoutOK && batchOK)}}"
    print(line)
    exit(0)
} catch {
    fail("\(error.localizedDescription)")
}
