import Darwin
import Foundation
import ModelDeckClient

public final class UnixSocketEngineTransport: EngineTransport {
    private let socketPath: String
    private let lifecycle = NSCondition()
    private var socketFD: Int32 = -1
    private var readBuffer = Data()
    private var connectionEpoch: UInt64 = 0
    private var inFlightIO: Int = 0
    private var isDrainingClose = false
    private var closeDrainFD: Int32 = -1
    internal var testingOnInFlightIOEntered: (() -> Void)?
    internal var testingOnWaitingForCloseDrain: (() -> Void)?
    /// Invoked by `close()` once the drain has been marked as in progress and the lock
    /// released, but before the socket is shut down. Tests park here to hold the drain
    /// open long enough to observe a concurrent `open()` waiting on it.
    internal var testingOnCloseDrainStarted: (() -> Void)?

    public init(socketPath: String) {
        self.socketPath = socketPath
    }

    deinit { close() }

    public func open() throws {
        lifecycle.lock()
        while isDrainingClose {
            testingOnWaitingForCloseDrain?()
            lifecycle.wait()
        }
        if socketFD >= 0 {
            lifecycle.unlock()
            return
        }
        lifecycle.unlock()

        let created = socket(AF_UNIX, SOCK_STREAM, 0)
        guard created >= 0 else { throw EngineTransportError.io("could not create unix socket") }

        var nosigpipe: Int32 = 1
        let nosigpipeResult = setsockopt(
            created,
            SOL_SOCKET,
            SO_NOSIGPIPE,
            &nosigpipe,
            socklen_t(MemoryLayout<Int32>.size)
        )
        guard nosigpipeResult == 0 else {
            closeFD(created)
            throw EngineTransportError.io("could not set SO_NOSIGPIPE on unix socket")
        }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = socketPath.utf8CString
        guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            closeFD(created)
            throw EngineTransportError.io("unix socket path is too long")
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { dest in
                for (index, byte) in pathBytes.enumerated() { dest[index] = byte }
            }
        }
        let length = socklen_t(MemoryLayout<sockaddr_un>.size)
        let connected = withUnsafePointer(to: &addr) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sock in
                connect(created, sock, length)
            }
        }
        guard connected == 0 else {
            closeFD(created)
            throw EngineTransportError.io("could not connect to engine socket")
        }

        lifecycle.lock()
        while isDrainingClose {
            testingOnWaitingForCloseDrain?()
            lifecycle.wait()
        }
        if socketFD >= 0 {
            closeFD(created)
            lifecycle.unlock()
            return
        }
        socketFD = created
        lifecycle.unlock()
    }

    public func send(frame: Data) throws {
        let fd: Int32
        let epoch: UInt64
        lifecycle.lock()
        guard socketFD >= 0 else {
            lifecycle.unlock()
            throw EngineTransportError.notConnected
        }
        fd = socketFD
        epoch = connectionEpoch
        inFlightIO += 1
        testingOnInFlightIOEntered?()
        lifecycle.unlock()

        defer {
            endInFlightIO()
        }

        do {
            try writeAll(frame: frame, to: fd)
        } catch {
            throw mapWriteError(error)
        }

        lifecycle.lock()
        let stillValid = socketFD == fd && connectionEpoch == epoch
        lifecycle.unlock()
        guard stillValid else { throw EngineTransportError.closed }
    }

    public func receiveFrame() throws -> Data {
        while true {
            lifecycle.lock()
            if socketFD < 0 {
                lifecycle.unlock()
                throw EngineTransportError.notConnected
            }
            if let frame = try EngineFrameCodec.decodeNextFrame(from: &readBuffer) {
                lifecycle.unlock()
                return frame
            }
            let fd = socketFD
            let epoch = connectionEpoch
            inFlightIO += 1
            testingOnInFlightIOEntered?()
            lifecycle.unlock()

            defer {
                endInFlightIO()
            }

            var chunk = [UInt8](repeating: 0, count: 4096)
            let readCount: Int = chunk.withUnsafeMutableBytes { raw -> Int in
                guard let base = raw.baseAddress?.assumingMemoryBound(to: UInt8.self) else { return 0 }
                while true {
                    let result = Int(Darwin.read(fd, base, raw.count))
                    if result >= 0 || errno != EINTR {
                        return result
                    }
                }
            }

            lifecycle.lock()
            if connectionEpoch != epoch || socketFD != fd {
                lifecycle.unlock()
                throw EngineTransportError.closed
            }
            if readCount == 0 {
                lifecycle.unlock()
                throw EngineTransportError.closed
            }
            if readCount < 0 {
                lifecycle.unlock()
                throw EngineTransportError.io("engine socket read failed")
            }
            readBuffer.append(chunk, count: readCount)
            lifecycle.unlock()
        }
    }

    public func close() {
        lifecycle.lock()
        if isDrainingClose {
            while isDrainingClose {
                lifecycle.wait()
            }
            lifecycle.unlock()
            return
        }
        guard socketFD >= 0 else {
            lifecycle.unlock()
            return
        }

        let fdToDrain = socketFD
        socketFD = -1
        connectionEpoch += 1
        readBuffer.removeAll(keepingCapacity: false)
        closeDrainFD = fdToDrain
        isDrainingClose = true
        let drainStartedHook = testingOnCloseDrainStarted
        lifecycle.unlock()

        drainStartedHook?()

        _ = Darwin.shutdown(fdToDrain, Int32(SHUT_RDWR))

        lifecycle.lock()
        while inFlightIO > 0 {
            lifecycle.wait()
        }
        let fdToClose = closeDrainFD
        closeDrainFD = -1
        isDrainingClose = false
        lifecycle.broadcast()
        lifecycle.unlock()

        if fdToClose >= 0 {
            closeFD(fdToClose)
        }
    }

    internal func socketFileDescriptorForTesting() -> Int32 {
        lifecycle.lock()
        let value = socketFD
        lifecycle.unlock()
        return value
    }

    internal static func nosigpipeOptionValue(for socketFD: Int32) -> Int32? {
        var value: Int32 = 0
        var length = socklen_t(MemoryLayout<Int32>.size)
        let result = getsockopt(socketFD, SOL_SOCKET, SO_NOSIGPIPE, &value, &length)
        guard result == 0 else { return nil }
        return value
    }

    private func endInFlightIO() {
        lifecycle.lock()
        inFlightIO -= 1
        if inFlightIO == 0 {
            lifecycle.broadcast()
        }
        lifecycle.unlock()
    }

    private func writeAll(frame: Data, to fd: Int32) throws {
        try frame.withUnsafeBytes { raw in
            guard let base = raw.baseAddress?.assumingMemoryBound(to: UInt8.self) else { return }
            var sent = 0
            while sent < frame.count {
                var wrote = Int(Darwin.write(fd, base.advanced(by: sent), frame.count - sent))
                while wrote < 0 && errno == EINTR {
                    wrote = Int(Darwin.write(fd, base.advanced(by: sent), frame.count - sent))
                }
                if wrote <= 0 {
                    if errno == EPIPE {
                        throw EngineTransportError.closed
                    }
                    throw EngineTransportError.io("engine socket write failed")
                }
                sent += wrote
            }
        }
    }

    private func mapWriteError(_ error: Error) -> Error {
        if let transportError = error as? EngineTransportError {
            return transportError
        }
        return error
    }

    private func closeFD(_ value: Int32) {
        _ = Darwin.close(value)
    }
}
