import XCTest
@testable import ModelDeckClient

final class EngineFrameCodecTests: XCTestCase {
    func testDecodeSingleFrame() throws {
        var buffer = Data("{\"ok\":true}\n".utf8)
        let frame = try EngineFrameCodec.decodeNextFrame(from: &buffer)
        XCTAssertEqual(frame, Data("{\"ok\":true}".utf8))
        XCTAssertTrue(buffer.isEmpty)
    }

    func testDecodeReturnsNilUntilNewline() throws {
        var buffer = Data("{\"ok\":true}".utf8)
        XCTAssertNil(try EngineFrameCodec.decodeNextFrame(from: &buffer))
        XCTAssertEqual(buffer, Data("{\"ok\":true}".utf8))
    }

    func testDecodeMultipleBufferedFramesLeavesEmptyBuffer() throws {
        var buffer = Data("{\"first\":1}\n{\"second\":2}\n".utf8)
        let first = try EngineFrameCodec.decodeNextFrame(from: &buffer)
        XCTAssertEqual(first, Data("{\"first\":1}".utf8))
        let second = try EngineFrameCodec.decodeNextFrame(from: &buffer)
        XCTAssertEqual(second, Data("{\"second\":2}".utf8))
        XCTAssertTrue(buffer.isEmpty)
        XCTAssertNil(try EngineFrameCodec.decodeNextFrame(from: &buffer))
    }

    func testDecodeRejectsIncompleteOversizedBufferWithoutNewline() {
        var buffer = Data(repeating: 0x41, count: EngineFrameCodec.maxFrameBytes + 1)
        XCTAssertThrowsError(try EngineFrameCodec.decodeNextFrame(from: &buffer)) { error in
            XCTAssertEqual(error as? EngineTransportError, .frameTooLarge(EngineFrameCodec.maxFrameBytes + 1))
        }
    }

    func testEncodeRejectsOversizedFrame() {
        let oversized = Data(repeating: 0x41, count: EngineFrameCodec.maxFrameBytes + 1)
        XCTAssertThrowsError(try EngineFrameCodec.encode(line: oversized)) { error in
            XCTAssertEqual(error as? EngineTransportError, .frameTooLarge(EngineFrameCodec.maxFrameBytes + 1))
        }
    }
}
