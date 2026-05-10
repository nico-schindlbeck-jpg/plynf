// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

final class WorkspaceTests: XCTestCase {
    override func setUp() {
        super.setUp()
        MockURLProtocol.reset()
    }

    func testWorkspaceReturnsExistingWhenFound() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces",
            json: TestFixtures.workspacesListJSON
        ))
        let ws = try await client.workspace(name: "my-research")
        XCTAssertEqual(ws.id, "ws_test_1")
        XCTAssertEqual(ws.name, "my-research")

        // Only one HTTP request — no POST should have been issued.
        XCTAssertEqual(MockURLProtocol.captured().count, 1)
    }

    func testWorkspaceCreatesWhenNotFound() async throws {
        let client = try makeClient()
        // First call: empty workspace list.
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces",
            json: """
                {"workspaces": []}
                """
        ))
        // Second call: POST create.
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/workspaces",
            statusCode: 201,
            json: TestFixtures.workspaceJSON
        ))
        let ws = try await client.workspace(name: "my-research")
        XCTAssertEqual(ws.id, "ws_test_1")

        let captured = MockURLProtocol.captured()
        XCTAssertEqual(captured.count, 2)
        XCTAssertEqual(captured[1].method, "POST")
    }

    func testWorkspaceCreatePostsNameInBody() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces",
            json: """
                {"workspaces": []}
                """
        ))
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/workspaces",
            statusCode: 201,
            json: TestFixtures.workspaceJSON
        ))
        _ = try await client.workspace(name: "my-research")
        let postBody = MockURLProtocol.captured()[1].body!
        let decoded = try JSONSerialization.jsonObject(with: postBody) as! [String: Any]
        XCTAssertEqual(decoded["name"] as? String, "my-research")
    }

    func testListWorkspacesParsesArray() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces",
            json: TestFixtures.workspacesListJSON
        ))
        let workspaces = try await client.listWorkspaces()
        XCTAssertEqual(workspaces.count, 1)
        XCTAssertEqual(workspaces.first?.id, "ws_test_1")
    }

    func testGetWorkspaceFetchesByID() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces/ws_test_1",
            json: TestFixtures.workspaceJSON
        ))
        let ws = try await client.getWorkspace(id: "ws_test_1")
        XCTAssertEqual(ws.id, "ws_test_1")
    }

    func testGetWorkspaceMapsNotFound() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces/ws_missing",
            statusCode: 404,
            json: """
                {"error": {"code": "WORKSPACE_NOT_FOUND", "message": "no such workspace"}}
                """
        ))
        do {
            _ = try await client.getWorkspace(id: "ws_missing")
            XCTFail("expected throw")
        } catch PlinthError.workspaceNotFound {
            // expected
        } catch {
            XCTFail("expected .workspaceNotFound, got \(error)")
        }
    }

    func testDeleteWorkspaceIssuesDELETE() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(MockResponse(
            method: "DELETE",
            path: "/v1/workspaces/ws_test_1",
            statusCode: 204,
            body: Data()
        ))
        try await client.deleteWorkspace(id: "ws_test_1")
        XCTAssertEqual(MockURLProtocol.captured().first?.method, "DELETE")
    }

    func testWorkspaceGetOrCreateTiebreakOnLatestUpdated() async throws {
        let client = try makeClient()
        // Two workspaces with the same name — the more recent updated_at
        // should win.
        let listJSON = """
            {
                "workspaces": [
                    {
                        "id": "ws_old",
                        "name": "dup",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "metadata": {}
                    },
                    {
                        "id": "ws_new",
                        "name": "dup",
                        "created_at": "2026-05-01T00:00:00Z",
                        "updated_at": "2026-05-01T00:00:00Z",
                        "metadata": {}
                    }
                ]
            }
            """
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces",
            json: listJSON
        ))
        let ws = try await client.workspace(name: "dup")
        XCTAssertEqual(ws.id, "ws_new")
    }
}
