// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

final class IdentityTests: XCTestCase {
    override func setUp() {
        super.setUp()
        MockURLProtocol.reset()
    }

    func testIssueTokenReturnsResponse() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/tokens",
            statusCode: 201,
            json: TestFixtures.tokenIssueResponseJSON
        ))
        let response = try await client.identity.issueToken(
            agentID: "my-agent",
            scopes: ["tool:web.fetch:read"],
            ttlSeconds: 3600
        )
        XCTAssertEqual(response.jti, "jti_1")
        XCTAssertEqual(response.claims.agentId, "my-agent")
    }

    func testIssueTokenSendsScopesAndAgent() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/tokens",
            statusCode: 201,
            json: TestFixtures.tokenIssueResponseJSON
        ))
        _ = try await client.identity.issueToken(
            agentID: "my-agent",
            scopes: ["tool:web.fetch:read"],
            ttlSeconds: 3600
        )
        let body = MockURLProtocol.captured().first?.body
        let decoded = try JSONSerialization.jsonObject(with: body!) as! [String: Any]
        XCTAssertEqual(decoded["agent_id"] as? String, "my-agent")
        XCTAssertEqual(decoded["ttl_seconds"] as? Int, 3600)
        let scopes = decoded["scopes"] as! [String]
        XCTAssertEqual(scopes, ["tool:web.fetch:read"])
    }

    func testVerifyTokenReturnsClaims() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/tokens/verify",
            json: TestFixtures.tokenClaimsJSON
        ))
        let claims = try await client.identity.verifyToken("ey.fake.jwt")
        XCTAssertEqual(claims.agentId, "my-agent")
        XCTAssertEqual(claims.scopes, ["tool:web.fetch:read"])
    }

    func testVerifyTokenMapsUnauthorized() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(
            method: "POST",
            path: "/v1/tokens/verify",
            statusCode: 401,
            json: """
                {"error": {"code": "TOKEN_EXPIRED", "message": "token expired"}}
                """
        ))
        do {
            _ = try await client.identity.verifyToken("expired")
            XCTFail("expected throw")
        } catch PlinthError.unauthorized {
            // expected
        } catch {
            XCTFail("expected .unauthorized, got \(error)")
        }
    }

    func testRevokeTokenSendsDELETE() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(MockResponse(
            method: "DELETE",
            path: "/v1/tokens/jti_1/revoke",
            statusCode: 204,
            body: Data()
        ))
        try await client.identity.revokeToken(jti: "jti_1")
        XCTAssertEqual(MockURLProtocol.captured().first?.method, "DELETE")
    }
}
