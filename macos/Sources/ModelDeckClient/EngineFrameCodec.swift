import Foundation

public enum EngineFrameCodec {
    public static let maxFrameBytes = 1_048_576

    public static func encode(line: Data) throws -> Data {
        if line.count > maxFrameBytes {
            throw EngineTransportError.frameTooLarge(line.count)
        }
        var payload = line
        payload.append(0x0A)
        return payload
    }

    public static func decodeNextFrame(from buffer: inout Data) throws -> Data? {
        guard let newline = buffer.firstIndex(of: 0x0A) else {
            if buffer.count > maxFrameBytes {
                throw EngineTransportError.frameTooLarge(buffer.count)
            }
            return nil
        }
        let frame = Data(buffer[..<newline])
        let endExclusive = buffer.index(after: newline)
        buffer.removeSubrange(buffer.startIndex..<endExclusive)
        if frame.count > maxFrameBytes {
            throw EngineTransportError.frameTooLarge(frame.count)
        }
        return frame
    }
}
