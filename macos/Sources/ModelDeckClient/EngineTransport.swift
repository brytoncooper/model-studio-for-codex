import Foundation

public protocol EngineTransport: AnyObject {
    func open() throws
    func send(frame: Data) throws
    func receiveFrame() throws -> Data
    func close()
}

public enum EngineTransportError: Error, Equatable, CustomStringConvertible {
    case notConnected
    case frameTooLarge(Int)
    case io(String)
    case closed

    public var description: String {
        switch self {
        case .notConnected: return "engine transport is not connected"
        case .frameTooLarge(let size): return "engine frame exceeds limit (\(size) bytes)"
        case .io(let message): return message
        case .closed: return "engine transport closed"
        }
    }
}
