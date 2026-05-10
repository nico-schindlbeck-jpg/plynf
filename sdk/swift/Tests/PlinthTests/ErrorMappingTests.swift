// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

final class ErrorMappingTests: XCTestCase {
    override func setUp() {
        super.setUp()
        MockURLProtocol.reset()
    }

    // MARK: - plinthErrorFromEnvelope

    func test401WithoutBodyMapsToUnauthorized() {
        let error = plinthErrorFromEnvelope(
            statusCode: 401,
            envelope: nil,
            retryAfterHeader: nil
        )
        guard case .unauthorized = error else {
            return XCTFail("expected .unauthorized, got \(error)")
        }
    }

    func test404WithFallbackCode() {
        let error = plinthErrorFromEnvelope(
            statusCode: 404,
            envelope: nil,
            retryAfterHeader: nil,
            fallbackNotFoundCode: PlinthErrorCode.workspaceNotFound
        )
        guard case .workspaceNotFound = error else {
            return XCTFail("expected .workspaceNotFound, got \(error)")
        }
    }

    func test429WithRetryAfterHeader() {
        let error = plinthErrorFromEnvelope(
            statusCode: 429,
            envelope: nil,
            retryAfterHeader: "12"
        )
        guard case .rateLimited(let retryAfter) = error else {
            return XCTFail("expected .rateLimited, got \(error)")
        }
        XCTAssertEqual(retryAfter, 12)
    }

    func test429WithEnvelopeDetailRetryAfter() {
        let env = PlinthErrorEnvelope(
            code: PlinthErrorCode.rateLimited,
            message: "slow down",
            details: ["retry_after_seconds": .double(5.5)]
        )
        let error = plinthErrorFromEnvelope(
            statusCode: 429,
            envelope: env,
            retryAfterHeader: nil
        )
        guard case .rateLimited(let retryAfter) = error else {
            return XCTFail("expected .rateLimited, got \(error)")
        }
        XCTAssertEqual(retryAfter, 5.5)
    }

    func test500FallsThroughToServer() {
        let env = PlinthErrorEnvelope(code: "BACKEND_DOWN", message: "kaboom")
        let error = plinthErrorFromEnvelope(
            statusCode: 500,
            envelope: env,
            retryAfterHeader: nil
        )
        guard case .server(let status, let code, let message) = error else {
            return XCTFail("expected .server, got \(error)")
        }
        XCTAssertEqual(status, 500)
        XCTAssertEqual(code, "BACKEND_DOWN")
        XCTAssertEqual(message, "kaboom")
    }

    // MARK: - End-to-end via mock server

    func test401EndToEndMapsThroughHTTPClient() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces",
            statusCode: 401,
            json: """
                {"error": {"code": "UNAUTHORIZED", "message": "bad token"}}
                """
        ))
        do {
            _ = try await client.listWorkspaces()
            XCTFail("expected throw")
        } catch PlinthError.unauthorized(let message) {
            XCTAssertTrue(message.contains("bad token"))
        } catch {
            XCTFail("expected .unauthorized, got \(error)")
        }
    }

    func testTransportErrorWhenNoCannedResponse() async throws {
        let client = try makeClient()
        // No response enqueued — MockURLProtocol returns an NSError.
        do {
            _ = try await client.listWorkspaces()
            XCTFail("expected throw")
        } catch PlinthError.transport {
            // expected
        } catch {
            XCTFail("expected .transport, got \(error)")
        }
    }
}
