import AppKit
import ApplicationServices
import Foundation

public enum CompanionGeometry {
    public static func appKitFrame(position: CGPoint, size: CGSize, primaryTop: CGFloat) -> NSRect {
        NSRect(x: position.x, y: primaryTop - position.y - size.height, width: size.width, height: size.height)
    }
    public static func shouldTrack(enabled: Bool, trusted: Bool, activeBundle: String?, ownBundle: String,
                            hostAvailable: Bool, hidden: Bool, minimized: Bool) -> Bool {
        enabled && trusted && hostAvailable && !hidden && !minimized &&
            (activeBundle == "com.openai.codex" || activeBundle == ownBundle)
    }
}


public enum WindowReservation {
    public struct WriteReceipt {
        public let mutated: Bool
        public let complete: Bool
        public init(mutated: Bool, complete: Bool) {
            self.mutated = mutated
            self.complete = complete
        }
    }
    public struct Outcome {
        public let accepted: NSRect?
        public let pendingOwnedFrame: NSRect?
        public init(accepted: NSRect?, pendingOwnedFrame: NSRect?) {
            self.accepted = accepted
            self.pendingOwnedFrame = pendingOwnedFrame
        }
    }
    public static func matches(_ first: NSRect, _ second: NSRect) -> Bool {
        abs(first.minX - second.minX) <= 2 && abs(first.minY - second.minY) <= 2 &&
        abs(first.width - second.width) <= 2 && abs(first.height - second.height) <= 2
    }
    public static func target(current: NSRect, reserved: CGFloat, desired: CGFloat) -> NSRect? {
        let width = current.width + reserved - desired
        guard width > 0 else { return nil }
        return NSRect(x: current.minX, y: current.minY, width: width, height: current.height)
    }
    public static func couldBeOurResize(_ candidate: NSRect, current: NSRect, target: NSRect) -> Bool {
        abs(candidate.minX - current.minX) <= 2 && abs(candidate.minY - current.minY) <= 2 &&
        abs(candidate.height - current.height) <= 2 &&
        candidate.width >= min(current.width, target.width) - 2 &&
        candidate.width <= max(current.width, target.width) + 2
    }
    // An admitted transaction completes and records ownership even if cancellation arrives.
    // Cancellation prevents new transactions; serialized release handles any pending owned frame.
    public static func change(current: NSRect, reserved: CGFloat, desired: CGFloat,
                       read: () -> NSRect?, write: (NSRect) -> WriteReceipt) -> Outcome {
        guard let target = target(current: current, reserved: reserved, desired: desired),
              let before = read(), matches(before, current) else { return Outcome(accepted: nil, pendingOwnedFrame: nil) }
        let receipt = write(target)
        guard receipt.mutated else { return Outcome(accepted: nil, pendingOwnedFrame: nil) }
        guard let accepted = read() else { return Outcome(accepted: nil, pendingOwnedFrame: target) }
        if receipt.complete && matches(accepted, target) { return Outcome(accepted: accepted, pendingOwnedFrame: nil) }
        guard couldBeOurResize(accepted, current: current, target: target) else {
            return Outcome(accepted: nil, pendingOwnedFrame: nil)
        }
        if let stillCurrent = read(), matches(accepted, stillCurrent) {
            let rollback = write(current)
            if rollback.complete, let restored = read(), matches(restored, current) {
                return Outcome(accepted: nil, pendingOwnedFrame: nil)
            }
        }
        return Outcome(accepted: nil, pendingOwnedFrame: accepted)
    }
}

public final class TrackingGeneration {
    public init() {}
    private let lock = NSLock()
    private var value = 0
    public func advance() -> Int { lock.lock(); defer { lock.unlock() }; value += 1; return value }
    public func matches(_ candidate: Int) -> Bool { lock.lock(); defer { lock.unlock() }; return candidate == value }
}

// Shared by the AX tracker and synthetic fixtures; unresolved cleanup remains owned.
public final class WindowReservationRecovery<Window> {
    public struct Ownership {
        public let window: Window
        public let frame: NSRect
        public let reservedWidth: CGFloat
        public init(window: Window, frame: NSRect, reservedWidth: CGFloat) {
            self.window = window
            self.frame = frame
            self.reservedWidth = reservedWidth
        }
    }
    public enum ReleaseResult: Equatable { case released, manualChange, pending }
    private let sameWindow: (Window, Window) -> Bool
    public private(set) var ownership: Ownership?
    private var alternateOwnership: Ownership?
    public private(set) var cleanupPending = false
    private var failedWindow: Window?

    public init(sameWindow: @escaping (Window, Window) -> Bool) { self.sameWindow = sameWindow }

    public func owns(_ window: Window) -> Bool { ownership.map { sameWindow($0.window, window) } ?? false }
    public func blocksPolling(_ window: Window) -> Bool {
        cleanupPending || (failedWindow.map { sameWindow($0, window) } ?? false)
    }
    public func recordSuccess(window: Window, frame: NSRect, reserved: CGFloat) {
        ownership = Ownership(window: window, frame: frame, reservedWidth: reserved)
        alternateOwnership = nil
        cleanupPending = false
        failedWindow = nil
    }
    public func recordFailure(window: Window, current: NSRect, priorReserved: CGFloat, outcome: WindowReservation.Outcome) {
        failedWindow = window
        if let pending = outcome.pendingOwnedFrame {
            ownership = Ownership(window: window, frame: pending,
                                  reservedWidth: priorReserved + current.width - pending.width)
            alternateOwnership = Ownership(window: window, frame: current, reservedWidth: priorReserved)
            cleanupPending = true
        }
    }
    private func clearOwnership() {
        ownership = nil
        alternateOwnership = nil
        cleanupPending = false
        failedWindow = nil
    }

    @discardableResult
    public func release(read: (Window) -> NSRect?, write: (Window, NSRect) -> WindowReservation.WriteReceipt,
                 allowed: () -> Bool) -> ReleaseResult {
        guard let owned = ownership else { clearOwnership(); return .released }
        guard allowed(), let current = read(owned.window) else {
            cleanupPending = true
            return .pending
        }
        let baseline: Ownership
        if WindowReservation.matches(current, owned.frame) { baseline = owned }
        else if let alternate = alternateOwnership, WindowReservation.matches(current, alternate.frame) { baseline = alternate }
        else if alternateOwnership != nil {
            // A failed readback leaves multiple possible owned states; never guess that cleanup succeeded.
            cleanupPending = true
            return .pending
        } else {
            // A verified ownership frame was changed manually; relinquish without writing a stale restore.
            clearOwnership()
            return .manualChange
        }
        guard allowed() else { cleanupPending = true; return .pending }
        if abs(baseline.reservedWidth) <= 2 { clearOwnership(); return .released }
        let outcome = WindowReservation.change(current: current, reserved: baseline.reservedWidth, desired: 0,
            read: { read(baseline.window) }, write: { write(baseline.window, $0) })
        if outcome.accepted != nil { clearOwnership(); return .released }
        if let pending = outcome.pendingOwnedFrame {
            ownership = Ownership(window: baseline.window, frame: pending,
                                  reservedWidth: baseline.reservedWidth + current.width - pending.width)
            alternateOwnership = Ownership(window: baseline.window, frame: current, reservedWidth: baseline.reservedWidth)
        } else {
            ownership = baseline
            alternateOwnership = nil
        }
        cleanupPending = true
        failedWindow = baseline.window
        return .pending
    }
}

// Created only after the new opt-in and system permission. No text, title, child, or pixel reads.
public final class HostWindowTracker {
    public init() {}
    public struct Update {
        public let frame: NSRect?
        public let reservedWidth: CGFloat
        public let error: String?
        public init(frame: NSRect?, reservedWidth: CGFloat, error: String?) {
            self.frame = frame
            self.reservedWidth = reservedWidth
            self.error = error
        }
    }
    private var focusedWindow: AXUIElement?
    private var focusedPID: pid_t?
    private let recovery = WindowReservationRecovery<AXUIElement>(sameWindow: { CFEqual($0, $1) })
    private var reservedWidth: CGFloat { recovery.ownership?.reservedWidth ?? 0 }

    private func value(_ element: AXUIElement, _ attribute: CFString) -> CFTypeRef? {
        var result: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &result) == .success else { return nil }
        return result
    }

    private func read(_ window: AXUIElement, primaryTop: CGFloat) -> NSRect? {
        AXUIElementSetMessagingTimeout(window, 0.08)
        guard value(window, kAXRoleAttribute as CFString) as? String == kAXWindowRole,
              value(window, kAXSubroleAttribute as CFString) as? String == kAXStandardWindowSubrole,
              let minimized = value(window, kAXMinimizedAttribute as CFString) as? Bool, !minimized,
              let positionValue = value(window, kAXPositionAttribute as CFString),
              let sizeValue = value(window, kAXSizeAttribute as CFString),
              CFGetTypeID(positionValue) == AXValueGetTypeID(), CFGetTypeID(sizeValue) == AXValueGetTypeID() else { return nil }
        var position = CGPoint.zero, size = CGSize.zero
        guard AXValueGetValue(positionValue as! AXValue, .cgPoint, &position),
              AXValueGetValue(sizeValue as! AXValue, .cgSize, &size),
              position.x.isFinite, position.y.isFinite, size.width.isFinite, size.height.isFinite,
              size.width > 0, size.height > 0 else { return nil }
        return CompanionGeometry.appKitFrame(position: position, size: size, primaryTop: primaryTop)
    }

    private func write(_ frame: NSRect, window: AXUIElement, primaryTop: CGFloat) -> WindowReservation.WriteReceipt {
        guard AXIsProcessTrusted() else { return .init(mutated: false, complete: false) }
        var size = frame.size
        guard let sizeValue = AXValueCreate(.cgSize, &size),
              AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, sizeValue) == .success else { return .init(mutated: false, complete: false) }
        var position = CGPoint(x: frame.minX, y: primaryTop - frame.maxY)
        guard let positionValue = AXValueCreate(.cgPoint, &position) else { return .init(mutated: true, complete: false) }
        return .init(mutated: true, complete: AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, positionValue) == .success)
    }

    @discardableResult
    public func release(primaryTop: CGFloat, allowed: () -> Bool) -> WindowReservationRecovery<AXUIElement>.ReleaseResult {
        let result = recovery.release(read: { self.read($0, primaryTop: primaryTop) },
            write: { self.write($1, window: $0, primaryTop: primaryTop) }, allowed: allowed)
        if result != .pending { focusedWindow = nil; focusedPID = nil }
        return result
    }

    public func update(pid: pid_t, refreshFocus: Bool, primaryTop: CGFloat, desiredWidth: CGFloat,
                explicitResize: Bool, allowed: () -> Bool) -> Update {
        guard allowed() && AXIsProcessTrusted() else { return Update(frame: nil, reservedWidth: 0, error: nil) }
        if refreshFocus || focusedPID != pid {
            let application = AXUIElementCreateApplication(pid)
            AXUIElementSetMessagingTimeout(application, 0.08)
            guard let focused = value(application, kAXFocusedWindowAttribute as CFString),
                  CFGetTypeID(focused) == AXUIElementGetTypeID() else { return Update(frame: nil, reservedWidth: reservedWidth, error: nil) }
            focusedWindow = (focused as! AXUIElement)
            focusedPID = pid
        }
        guard let window = focusedWindow, let current = read(window, primaryTop: primaryTop) else {
            return Update(frame: nil, reservedWidth: reservedWidth, error: nil)
        }
        if recovery.blocksPolling(window) && !explicitResize {
            return Update(frame: nil, reservedWidth: reservedWidth,
                          error: "Window attachment or cleanup is unresolved. Retry explicitly; no attachment is claimed and polling will not resize the window.")
        }
        let newWindow = !recovery.owns(window)
        if newWindow {
            if recovery.ownership != nil {
                guard release(primaryTop: primaryTop, allowed: allowed) != .pending else {
                    return Update(frame: nil, reservedWidth: reservedWidth,
                                  error: "The previous window’s reserved space could not be released. It remains tracked for cleanup; the new window was not changed.")
                }
                focusedWindow = window
                focusedPID = pid
            }
        } else if recovery.cleanupPending {
            guard release(primaryTop: primaryTop, allowed: allowed) != .pending else {
                return Update(frame: nil, reservedWidth: reservedWidth, error: "Reserved-space cleanup is still unresolved. No attachment is claimed.")
            }
            focusedWindow = window
            focusedPID = pid
            // Cleanup changed the host frame. Wait for a fresh explicit update rather than resizing stale geometry.
            recovery.recordFailure(window: window, current: current, priorReserved: 0,
                                   outcome: .init(accepted: nil, pendingOwnedFrame: nil))
            return Update(frame: nil, reservedWidth: 0, error: "Cleanup completed. Enable window following again to attach from the current window size.")
        }
        if newWindow || explicitResize {
            guard allowed() else { return Update(frame: nil, reservedWidth: reservedWidth, error: nil) }
            let priorReserved = newWindow ? 0 : reservedWidth
            let outcome = WindowReservation.change(current: current, reserved: priorReserved,
                desired: desiredWidth, read: { self.read(window, primaryTop: primaryTop) },
                write: { self.write($0, window: window, primaryTop: primaryTop) })
            guard let accepted = outcome.accepted else {
                recovery.recordFailure(window: window, current: current, priorReserved: priorReserved, outcome: outcome)
                return Update(frame: nil, reservedWidth: reservedWidth,
                              error: "ChatGPT/Codex did not accept the required window size. Any partial change was rolled back when safe. Enlarge the window and retry; no sidebar attachment is claimed.")
            }
            recovery.recordSuccess(window: window, frame: accepted, reserved: desiredWidth)
            return Update(frame: accepted, reservedWidth: reservedWidth, error: nil)
        }
        // Manual moves/resizes are followed, never corrected by the polling loop.
        return Update(frame: current, reservedWidth: reservedWidth, error: nil)
    }
}
