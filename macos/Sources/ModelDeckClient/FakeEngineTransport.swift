import Foundation

public final class FakeEngineTransport: EngineTransport {
    private let lock = NSLock()
    private var opened = false
    private var outbound: [Data] = []
    private var inbound: [Data] = []
    private var receiveIndex = 0
    public var onSend: ((Data) -> Void)?

    public init(responses: [Data] = []) {
        inbound = responses
    }

    public func queueResponse(_ frame: Data) {
        lock.lock()
        inbound.append(frame)
        lock.unlock()
    }

    public func recordedFrames() -> [Data] {
        lock.lock()
        defer { lock.unlock() }
        return outbound
    }

    public func open() throws {
        lock.lock()
        opened = true
        lock.unlock()
    }

    public func send(frame: Data) throws {
        lock.lock()
        guard opened else { lock.unlock(); throw EngineTransportError.notConnected }
        outbound.append(frame)
        let handler = onSend
        lock.unlock()
        handler?(frame)
    }

    public func receiveFrame() throws -> Data {
        lock.lock()
        guard opened else { lock.unlock(); throw EngineTransportError.notConnected }
        if receiveIndex >= inbound.count {
            lock.unlock()
            throw EngineTransportError.closed
        }
        let frame = inbound[receiveIndex]
        receiveIndex += 1
        lock.unlock()
        return frame
    }

    public func close() {
        lock.lock()
        opened = false
        lock.unlock()
    }
}
