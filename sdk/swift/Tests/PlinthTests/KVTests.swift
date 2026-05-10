// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

final class KVTests: XCTestCase {
    override func setUp() {
        super.setUp()
        MockURLProtocol.reset()
    }

    private func makeWorkspace() throws -> Workspace {
        let record = WorkspaceRecord(
            id: "ws_test_1",
            name: "my-research",
            createdAt: Date(timeIntervalSince1970: 1_700_000_000),
            updatedAt: Date(timeIntervalSince1970: 1_700_000_000)
        )
        let client = try makeClient()
        return Workspace(record: record, http: client.workspaceHTTP)
    }

    func testSetReturnsEntry() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "PUT",
            path: "/v1/workspaces/ws_test_1/kv/topic",
            json: TestFixtures.kvEntryJSON
        ))
        let entry = try await ws.kv.set(key: "topic", value: "renewable energy")
        XCTAssertEqual(entry.key, "topic")
        XCTAssertEqual(entry.version, 1)
    }

    func testSetSendsValueWrapper() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "PUT",
            path: "/v1/workspaces/ws_test_1/kv/topic",
            json: TestFixtures.kvEntryJSON
        ))
        _ = try await ws.kv.set(key: "topic", value: "renewable energy")
        let body = MockURLProtocol.captured().first?.body
        let decoded = try JSONSerialization.jsonObject(with: body!) as! [String: Any]
        XCTAssertEqual(decoded["value"] as? String, "renewable energy")
    }

    func testGetTypedString() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces/ws_test_1/kv/topic",
            json: TestFixtures.kvEntryJSON
        ))
        let value: String = try await ws.kv.get(key: "topic")
        XCTAssertEqual(value, "renewable energy")
    }

    func testGetMapsKeyNotFound() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces/ws_test_1/kv/missing",
            statusCode: 404,
            json: """
                {"error": {"code": "KEY_NOT_FOUND", "message": "nope"}}
                """
        ))
        do {
            let _: String = try await ws.kv.get(key: "missing")
            XCTFail("expected throw")
        } catch PlinthError.keyNotFound {
            // expected
        } catch {
            XCTFail("expected .keyNotFound, got \(error)")
        }
    }

    func testHistoryReturnsVersions() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces/ws_test_1/kv/topic/history",
            json: TestFixtures.kvHistoryJSON
        ))
        let versions = try await ws.kv.history(key: "topic")
        XCTAssertEqual(versions.count, 2)
        XCTAssertEqual(versions[0].version, 1)
        XCTAssertEqual(versions[1].version, 2)
    }

    func testKeysWithSpecialCharsArePathEncoded() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "PUT",
            path: "/v1/workspaces/ws_test_1/kv/key%20with%20space",
            json: TestFixtures.kvEntryJSON
        ))
        _ = try await ws.kv.set(key: "key with space", value: "x")
        let url = MockURLProtocol.captured().first?.url.path
        XCTAssertEqual(url, "/v1/workspaces/ws_test_1/kv/key%20with%20space")
    }

    func testDeleteSendsDELETE() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(MockResponse(
            method: "DELETE",
            path: "/v1/workspaces/ws_test_1/kv/topic",
            statusCode: 204,
            body: Data()
        ))
        try await ws.kv.delete(key: "topic")
        XCTAssertEqual(MockURLProtocol.captured().first?.method, "DELETE")
    }
}
