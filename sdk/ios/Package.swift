// swift-tools-version:5.5
import PackageDescription

let package = Package(
    name: "Controlvar",
    platforms: [
        .iOS(.v14),
        .macOS(.v11)
    ],
    products: [
        .library(
            name: "Controlvar",
            targets: ["Controlvar"]
        ),
        // Contract smoke against a live backend: `swift run contract-smoke`.
        .executable(
            name: "contract-smoke",
            targets: ["contract-smoke"]
        )
    ],
    targets: [
        .target(
            name: "Controlvar",
            dependencies: [],
            path: "Sources/Controlvar"
        ),
        .executableTarget(
            name: "contract-smoke",
            dependencies: ["Controlvar"],
            path: "Sources/contract-smoke"
        ),
        .testTarget(
            name: "ControlvarTests",
            dependencies: ["Controlvar"],
            path: "Tests/ControlvarTests"
        )
    ]
)
