// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ModelDeck",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "ModelDeck", targets: ["ModelDeck"]),
        .executable(name: "ModelDeckPanelDemo", targets: ["ModelDeckPanelDemo"]),
        .library(name: "ModelDeckPresentation", targets: ["ModelDeckPresentation"]),
        .library(name: "ModelDeckPlatform", targets: ["ModelDeckPlatform"]),
        .library(name: "ModelDeckClient", targets: ["ModelDeckClient"]),
    ],
    targets: [
        .target(
            name: "ModelDeckContracts",
            path: "Sources/ModelDeckContracts",
            resources: [
                .copy("Resources/fixtures"),
                .copy("Resources/contracts"),
            ]
        ),
        .testTarget(
            name: "ModelDeckContractsTests",
            dependencies: ["ModelDeckContracts"],
            path: "Tests/ModelDeckContractsTests"
        ),
        .target(
            name: "ModelDeckClient",
            dependencies: ["ModelDeckContracts"],
            path: "Sources/ModelDeckClient"
        ),
        .testTarget(
            name: "ModelDeckClientTests",
            dependencies: ["ModelDeckClient", "ModelDeckContracts"],
            path: "Tests/ModelDeckClientTests"
        ),
        .target(
            name: "ModelDeckPresentation",
            dependencies: ["ModelDeckClient"],
            path: "Sources/ModelDeckPresentation",
            resources: [.copy("Resources/provider_presets.json")],
            linkerSettings: [.linkedFramework("AppKit")]
        ),
        .testTarget(
            name: "ModelDeckPresentationTests",
            dependencies: ["ModelDeckPresentation", "ModelDeckClient"],
            path: "Tests/ModelDeckPresentationTests"
        ),
        .target(
            name: "ModelDeckPlatform",
            dependencies: ["ModelDeckClient"],
            path: "Sources/ModelDeckPlatform",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("ApplicationServices"),
            ]
        ),
        .testTarget(
            name: "ModelDeckPlatformTests",
            dependencies: ["ModelDeckPlatform"],
            path: "Tests/ModelDeckPlatformTests"
        ),
        .executableTarget(
            name: "ModelDeckPanelDemo",
            dependencies: ["ModelDeckPresentation", "ModelDeckPlatform", "ModelDeckClient", "ModelDeckContracts"],
            path: "Sources/ModelDeckPanelDemo",
            exclude: ["README.md"],
            linkerSettings: [.linkedFramework("AppKit")]
        ),
        .executableTarget(
            name: "ModelDeck",
            dependencies: ["ModelDeckPresentation", "ModelDeckPlatform", "ModelDeckClient"],
            path: "Sources/ModelDeckApp",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("Security"),
                .linkedFramework("ApplicationServices"),
            ]
        ),
    ]
)
