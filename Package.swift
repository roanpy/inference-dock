// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ModelDispatchMenu",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "ModelDispatchMenu",
            path: "apps/ModelDispatchMenu/Sources/ModelDispatchMenu"
        )
    ]
)
