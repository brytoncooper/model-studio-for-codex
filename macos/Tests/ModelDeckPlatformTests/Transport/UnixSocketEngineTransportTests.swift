import Darwin
import Foundation
import XCTest
@testable import ModelDeckPlatform

final class UnixSocketEngineTransportTests: XCTestCase {
    func testReceiveDecodesSingleFrame() throws {
        let fixture = try TempUnixStreamFixture()
        try fixture.start()
        defer { fixture.shutdown() }

        let transport = UnixSocketEngineTransport(socketPath: fixture.path)
        try transport.open()
        defer { transport.close() }

        XCTAssertTrue(fixture.waitForClientConnection(timeout: 1))
        let payload = Data("{\"ok\":true}".utf8)
        var encoded = payload
        encoded.append(0x0A)
        try fixture.writeToClient(encoded)

        let frame = try transport.receiveFrame()
        XCTAssertEqual(frame, payload)
    }

    func testCloseUnblocksBlockedReceiveWithoutHang() throws {
        let fixture = try TempUnixStreamFixture()
        try fixture.start()
        defer { fixture.shutdown() }

        let transport = UnixSocketEngineTransport(socketPath: fixture.path)
        try transport.open()

        XCTAssertTrue(fixture.waitForClientConnection(timeout: 1))

        let ioEntered = DispatchSemaphore(value: 0)
        transport.testingOnInFlightIOEntered = { ioEntered.signal() }

        let finished = DispatchSemaphore(value: 0)
        var receiveDescription: String?
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                _ = try transport.receiveFrame()
                XCTFail("expected receive to fail after close")
            } catch {
                receiveDescription = String(describing: error)
            }
            finished.signal()
        }

        XCTAssertEqual(ioEntered.wait(timeout: .now() + 1), .success)
        transport.close()

        XCTAssertEqual(finished.wait(timeout: .now() + 1), .success)
        XCTAssertEqual(receiveDescription, "engine transport closed")
    }

    func testOpenWaitsForCloseDrainBeforeInstallingNewSocket() throws {
        let fixture = try TempUnixStreamFixture()
        try fixture.start()
        defer { fixture.shutdown() }

        let transport = UnixSocketEngineTransport(socketPath: fixture.path)
        try transport.open()
        XCTAssertTrue(fixture.waitForClientConnection(timeout: 1))

        let ioEntered = DispatchSemaphore(value: 0)
        let drainStarted = DispatchSemaphore(value: 0)
        let releaseDrain = DispatchSemaphore(value: 0)
        let openWaitingForDrain = DispatchSemaphore(value: 0)
        transport.testingOnInFlightIOEntered = { ioEntered.signal() }
        transport.testingOnWaitingForCloseDrain = { openWaitingForDrain.signal() }
        transport.testingOnCloseDrainStarted = {
            drainStarted.signal()
            _ = releaseDrain.wait(timeout: .now() + 5)
        }

        // A reader blocked inside the transport keeps the close drain from finishing
        // on its own, so the drain window is controlled by this test instead of timing.
        DispatchQueue.global(qos: .userInitiated).async {
            do { _ = try transport.receiveFrame() } catch { }
        }

        XCTAssertEqual(ioEntered.wait(timeout: .now() + 1), .success)

        let closeFinished = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .userInitiated).async {
            transport.close()
            closeFinished.signal()
        }

        // close() is parked inside the drain, so the drain is provably in progress
        // before open() starts. Without this the open below can run either before
        // close() marks the drain or after the drain has already finished.
        XCTAssertEqual(drainStarted.wait(timeout: .now() + 1), .success)

        let openFinished = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                try transport.open()
            } catch {
                XCTFail("open after drain should succeed: \(error)")
            }
            openFinished.signal()
        }

        XCTAssertEqual(openWaitingForDrain.wait(timeout: .now() + 1), .success)
        XCTAssertEqual(transport.socketFileDescriptorForTesting(), -1)

        releaseDrain.signal()
        XCTAssertEqual(closeFinished.wait(timeout: .now() + 1), .success)
        XCTAssertTrue(fixture.acceptNextClient(timeout: 1))
        XCTAssertEqual(openFinished.wait(timeout: .now() + 1), .success)
        XCTAssertGreaterThanOrEqual(transport.socketFileDescriptorForTesting(), 0)

        transport.testingOnInFlightIOEntered = nil
        transport.testingOnWaitingForCloseDrain = nil
        transport.testingOnCloseDrainStarted = nil
        transport.close()
    }

    func testPeerDisconnectOnSendDoesNotTerminateProcessAndReturnsClosed() throws {
        let fixture = try TempUnixStreamFixture()
        try fixture.start()
        defer { fixture.shutdown() }

        let transport = UnixSocketEngineTransport(socketPath: fixture.path)
        try transport.open()
        defer { transport.close() }

        XCTAssertTrue(fixture.waitForClientConnection(timeout: 1))
        fixture.closeAcceptedClient()

        var frame = Data("ping".utf8)
        frame.append(0x0A)
        do {
            try transport.send(frame: frame)
            XCTFail("expected send to fail after peer disconnect")
        } catch {
            XCTAssertEqual(String(describing: error), "engine transport closed")
        }
    }

    func testOpenSetsNOSIGPIPEOnSocket() throws {
        let fixture = try TempUnixStreamFixture()
        try fixture.start()
        defer { fixture.shutdown() }

        let transport = UnixSocketEngineTransport(socketPath: fixture.path)
        try transport.open()
        defer { transport.close() }

        let fd = transport.socketFileDescriptorForTesting()
        XCTAssertGreaterThanOrEqual(fd, 0)
        XCTAssertEqual(UnixSocketEngineTransport.nosigpipeOptionValue(for: fd), 1)
    }
}

private final class TempUnixStreamFixture {
    let path: String
    private var listenFD: Int32 = -1
    private var acceptedFD: Int32 = -1
    private let workQueue = DispatchQueue(label: "TempUnixStreamFixture")
    private let clientConnected = DispatchSemaphore(value: 0)

    private static var maxUnixPathLength: Int {
        MemoryLayout.size(ofValue: sockaddr_un().sun_path)
    }

    init() throws {
        let shortID = UUID().uuidString.prefix(8)
        let candidate = "/private/tmp/mdt-\(getpid())-\(shortID).sock"
        guard candidate.utf8CString.count <= Self.maxUnixPathLength else {
            throw NSError(domain: "TempUnixStreamFixture", code: 2)
        }
        path = candidate
    }

    func start() throws {
        listenFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listenFD >= 0 else {
            throw NSError(domain: "TempUnixStreamFixture", code: 1)
        }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = path.utf8CString
        guard pathBytes.count <= Self.maxUnixPathLength else {
            throw NSError(domain: "TempUnixStreamFixture", code: 2)
        }
        _ = unlink(path)
        withUnsafeMutablePointer(to: &addr.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { dest in
                for (index, byte) in pathBytes.enumerated() { dest[index] = byte }
            }
        }

        let bindResult = withUnsafePointer(to: &addr) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sock in
                bind(listenFD, sock, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bindResult == 0 else {
            throw NSError(domain: "TempUnixStreamFixture", code: 3)
        }
        guard listen(listenFD, 1) == 0 else {
            throw NSError(domain: "TempUnixStreamFixture", code: 4)
        }

        workQueue.async { [weak self] in
            guard let self else { return }
            var clientAddr = sockaddr_un()
            var addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
            let accepted = withUnsafeMutablePointer(to: &clientAddr) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sock in
                    accept(self.listenFD, sock, &addrLen)
                }
            }
            if accepted >= 0 {
                self.acceptedFD = accepted
                self.clientConnected.signal()
            }
        }
    }

    func waitForClientConnection(timeout: TimeInterval) -> Bool {
        clientConnected.wait(timeout: .now() + timeout) == .success
    }


    func acceptNextClient(timeout: TimeInterval) -> Bool {
        let accepted = DispatchSemaphore(value: 0)
        workQueue.async { [weak self] in
            guard let self else { return }
            var clientAddr = sockaddr_un()
            var addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
            let next = withUnsafeMutablePointer(to: &clientAddr) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sock in
                    accept(self.listenFD, sock, &addrLen)
                }
            }
            if next >= 0 {
                if self.acceptedFD >= 0 {
                    _ = Darwin.close(self.acceptedFD)
                }
                self.acceptedFD = next
            }
            accepted.signal()
        }
        return accepted.wait(timeout: .now() + timeout) == .success
    }

    func writeToClient(_ data: Data) throws {
        guard acceptedFD >= 0 else {
            throw NSError(domain: "TempUnixStreamFixture", code: 5)
        }
        try data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress?.assumingMemoryBound(to: UInt8.self) else { return }
            var sent = 0
            while sent < data.count {
                let wrote = Int(Darwin.write(acceptedFD, base.advanced(by: sent), data.count - sent))
                guard wrote > 0 else {
                    throw NSError(domain: "TempUnixStreamFixture", code: 6)
                }
                sent += wrote
            }
        }
    }

    func closeAcceptedClient() {
        if acceptedFD >= 0 {
            _ = Darwin.shutdown(acceptedFD, Int32(SHUT_RDWR))
            _ = Darwin.close(acceptedFD)
            acceptedFD = -1
        }
    }

    func shutdown() {
        closeAcceptedClient()
        if listenFD >= 0 {
            _ = Darwin.shutdown(listenFD, Int32(SHUT_RDWR))
            _ = Darwin.close(listenFD)
            listenFD = -1
        }
        _ = unlink(path)
    }

    deinit {
        shutdown()
    }
}
