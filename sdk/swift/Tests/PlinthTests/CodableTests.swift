// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

/// Unit tests for the wire-format models (no HTTP traffic).
final class CodableTests: XCTestCase {
    private let encoder = HTTPClient.makeEncoder()
    private let decoder = HTTPClient.makeDecoder()

    // MARK: - AnyCodableValue

    func testAnyCodableValueRoundTripsString() throws {
        let original = AnyCodableValue.string("hello")
        let data = try encoder.encode(original)
        let decoded = try decoder.decode(AnyCodableValue.self, from: data)
        XCTAssertEqual(decoded, .string("hello"))
    }

    func testAnyCodableValueRoundTripsArray() throws {
        let original: AnyCodableValue = .array([.int(1), .int(2), .string("three")])
        let data = try encoder.encode(original)
        let decoded = try decoder.decode(AnyCodableValue.self, from: data)
        XCTAssertEqual(decoded, .array([.int(1), .int(2), .string("three")]))
    }

    func testAnyCodableValueFromFoundation() {
        XCTAssertEqual(AnyCodableValue.from(nil), .null)
        XCTAssertEqual(AnyCodableValue.from(true), .bool(true))
        XCTAssertEqual(AnyCodableValue.from(42), .int(42))
        XCTAssertEqual(AnyCodableValue.from(3.5), .double(3.5))
        XCTAssertEqual(AnyCodableValue.from("hi"), .string("hi"))
    }

    func testAnyCodableValueConvenienceAccessors() {
        XCTAssertEqual(AnyCodableValue.string("x").stringValue, "x")
        XCTAssertEqual(AnyCodableValue.int(7).intValue, 7)
        XCTAssertEqual(AnyCodableValue.double(7.0).intValue, 7)
        XCTAssertEqual(AnyCodableValue.bool(true).boolValue, true)
        XCTAssertNil(AnyCodableValue.null.stringValue)
    }

    // MARK: - KVEntry

    func testKVEntryDecodesFromWireFormat() throws {
        let json = Data(TestFixtures.kvEntryJSON.utf8)
        let entry = try decoder.decode(KVEntry.self, from: json)
        XCTAssertEqual(entry.workspaceId, "ws_test_1")
        XCTAssertEqual(entry.key, "topic")
        XCTAssertEqual(entry.version, 1)
        XCTAssertEqual(entry.value, .string("renewable energy"))
    }

    func testWorkspaceRecordRoundTrip() throws {
        let json = Data(TestFixtures.workspaceJSON.utf8)
        let record = try decoder.decode(WorkspaceRecord.self, from: json)
        XCTAssertEqual(record.id, "ws_test_1")
        XCTAssertEqual(record.name, "my-research")
        let reencoded = try encoder.encode(record)
        let again = try decoder.decode(WorkspaceRecord.self, from: reencoded)
        XCTAssertEqual(again.id, record.id)
        XCTAssertEqual(again.name, record.name)
    }

    // MARK: - Path encoding

    func testEncodePathSegmentEscapesSlash() {
        XCTAssertEqual(encodePathSegment("a/b"), "a%2Fb")
    }

    func testEncodeFilePathPreservesSlash() {
        XCTAssertEqual(encodeFilePath("dir/sub/file.md"), "dir/sub/file.md")
    }

    func testEncodeFilePathEscapesSpaces() {
        XCTAssertEqual(encodeFilePath("dir/with space/file.md"), "dir/with%20space/file.md")
    }

    func testEncodeFilePathStripsLeadingSlash() {
        XCTAssertEqual(encodeFilePath("/a/b.md"), "a/b.md")
    }

    // MARK: - Error envelope

    func testErrorEnvelopeDecodes() throws {
        let json = Data("""
            {"error": {"code": "WORKSPACE_NOT_FOUND", "message": "no", "details": {"x": 1}}}
            """.utf8)
        let envelope = try decoder.decode(PlinthErrorEnvelope.self, from: json)
        XCTAssertEqual(envelope.code, "WORKSPACE_NOT_FOUND")
        XCTAssertEqual(envelope.message, "no")
        XCTAssertEqual(envelope.details?["x"]?.intValue, 1)
    }
}
