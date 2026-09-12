import AppKit
import XCTest
@testable import ModelDeckPlatform

final class WindowReservationTests: XCTestCase {
    func testReservationRollbackAndGenerationRace() {
        var fake = NSRect(x: 0, y: 0, width: 1200, height: 800)
        let original = fake
        let write: (NSRect) -> WindowReservation.WriteReceipt = { frame in
            fake = frame
            return .init(mutated: true, complete: true)
        }
        let expanded = WindowReservation.change(current: fake, reserved: 0, desired: 600, read: { fake }, write: write)
        XCTAssertNotNil(expanded.accepted)
        let generation = TrackingGeneration()
        let token = generation.advance()
        _ = WindowReservation.change(current: original, reserved: 0, desired: 600, read: { fake }, write: { _ in
            XCTAssertTrue(generation.matches(token))
            return .init(mutated: false, complete: false)
        })
        let recovery = WindowReservationRecovery<Int>(sameWindow: { $0 == $1 })
        recovery.recordFailure(window: 1, current: original, priorReserved: 0, outcome: .init(accepted: nil, pendingOwnedFrame: fake))
        XCTAssertTrue(recovery.cleanupPending)
    }

    func testCompanionGeometryVisibility() {
        let own = "com.cooper.model-deck"
        XCTAssertTrue(CompanionGeometry.shouldTrack(enabled: true, trusted: true, activeBundle: own, ownBundle: own, hostAvailable: true, hidden: false, minimized: false))
        XCTAssertFalse(CompanionGeometry.shouldTrack(enabled: true, trusted: true, activeBundle: own, ownBundle: own, hostAvailable: true, hidden: false, minimized: true))
    }
}
