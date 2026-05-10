// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

final class ToolsTests: XCTestCase {
    override func setUp() {
        super.setUp()
        MockURLProtocol.reset()
    }

    func testInvokeReturnsResult() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/invoke",
            json: TestFixtures.invokeResponseJSON
        ))
        let result = try await client.tools.invoke(
            toolID: "web.fetch",
            arguments: ["url": "mock://example"]
        )
        XCTAssertEqual(result.toolId, "web.fetch")
        XCTAssertFalse(result.cached)
        XCTAssertEqual(result.auditId, "evt_1")
    }

    func testInvokePostsArgumentsInBody() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/invoke",
            json: TestFixtures.invokeResponseJSON
        ))
        _ = try await client.tools.invoke(
            toolID: "web.fetch",
            arguments: ["url": "mock://example"],
            options: .init(workspaceID: "ws_test_1", agentID: "my-agent")
        )
        let body = MockURLProtocol.captured().first?.body
        let decoded = try JSONSerialization.jsonObject(with: body!) as! [String: Any]
        XCTAssertEqual(decoded["tool_id"] as? String, "web.fetch")
        XCTAssertEqual(decoded["workspace_id"] as? String, "ws_test_1")
        XCTAssertEqual(decoded["agent_id"] as? String, "my-agent")
        let args = decoded["arguments"] as! [String: Any]
        XCTAssertEqual(args["url"] as? String, "mock://example")
    }

    func testInvokeMapsToolNotFound() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/invoke",
            statusCode: 404,
            json: """
                {"error": {"code": "TOOL_NOT_FOUND", "message": "unknown tool"}}
                """
        ))
        do {
            _ = try await client.tools.invoke(toolID: "missing", arguments: [:])
            XCTFail("expected throw")
        } catch PlinthError.toolNotFound {
            // expected
        } catch {
            XCTFail("expected .toolNotFound, got \(error)")
        }
    }

    func testInvokeMapsRateLimitedWithRetryAfter() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(MockResponse(
            method: "POST",
            path: "/v1/invoke",
            statusCode: 429,
            body: Data("""
                {"error": {"code": "RATE_LIMITED", "message": "slow down", "details": {"retry_after_seconds": 7.5}}}
                """.utf8),
            headers: ["Content-Type": "application/json", "Retry-After": "7"]
        ))
        do {
            _ = try await client.tools.invoke(toolID: "web.fetch", arguments: [:])
            XCTFail("expected throw")
        } catch PlinthError.rateLimited(let retryAfter) {
            XCTAssertEqual(retryAfter, 7.5)
        } catch {
            XCTFail("expected .rateLimited, got \(error)")
        }
    }

    func testListToolsDecodes() async throws {
        let client = try makeClient()
        let listJSON = """
            {
                "tools": [
                    {
                        "tool_id": "web.fetch",
                        "name": "Web Fetch",
                        "description": "Fetch a URL",
                        "transport": "http",
                        "endpoint": "http://mock/tools/web.fetch",
                        "input_schema": {},
                        "output_schema": {},
                        "created_at": "2026-05-01T10:00:00Z",
                        "updated_at": "2026-05-01T10:00:00Z"
                    }
                ]
            }
            """
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/tools",
            json: listJSON
        ))
        let tools = try await client.tools.list()
        XCTAssertEqual(tools.count, 1)
        XCTAssertEqual(tools.first?.toolId, "web.fetch")
    }

    func testInvokeWithAnyDictionaryConvertsArguments() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/invoke",
            json: TestFixtures.invokeResponseJSON
        ))
        _ = try await client.tools.invoke(
            toolID: "web.fetch",
            arguments: ["url": "mock://x", "count": 5] as [String: Any]
        )
        let body = MockURLProtocol.captured().first?.body
        let decoded = try JSONSerialization.jsonObject(with: body!) as! [String: Any]
        let args = decoded["arguments"] as! [String: Any]
        XCTAssertEqual(args["url"] as? String, "mock://x")
        XCTAssertEqual((args["count"] as? NSNumber)?.intValue, 5)
    }
}
