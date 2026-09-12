import Foundation

public enum EngineClientError: Error, Equatable, CustomStringConvertible {
    case invalidRendezvous(String)
    case missingCredential
    case rendezvousMismatch
    case negotiationFailed(String)
    case protocolError(String)
    case requestCancelled
    case unavailable(String)

    public var description: String {
        switch self {
        case .invalidRendezvous(let message): return message
        case .missingCredential: return "engine instance credential is unavailable"
        case .rendezvousMismatch: return "engine instance identity did not match the rendezvous descriptor"
        case .negotiationFailed(let message): return message
        case .protocolError(let message): return message
        case .requestCancelled: return "engine request was cancelled"
        case .unavailable(let message): return message
        }
    }
}
