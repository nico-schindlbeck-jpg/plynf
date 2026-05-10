// swift-tools-version: 5.9
// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import PackageDescription

let package = Package(
    name: "Plinth",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(name: "Plinth", targets: ["Plinth"]),
    ],
    targets: [
        .target(
            name: "Plinth",
            path: "Sources/Plinth"
        ),
        .testTarget(
            name: "PlinthTests",
            dependencies: ["Plinth"],
            path: "Tests/PlinthTests"
        ),
    ]
)
