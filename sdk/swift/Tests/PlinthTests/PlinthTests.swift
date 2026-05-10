// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

final class PlinthInitTests: XCTestCase {
    override func setUp() {
        super.setUp()
        MockURLProtocol.reset()
    }

    // MARK: - Init validation

    func testInitRequiresAPIKey() {
        XCTAssertThrowsError(try Plinth(apiKey: "")) { error in
            guard case .invalidConfig(let message) = error as? PlinthError else {
                return XCTFail("expected .invalidConfig, got \(error)")
            }
            XCTAssertTrue(message.lowercased().contains("apikey"))
        }
    }

    func testInitRejectsMalformedWorkspaceURL() {
        XCTAssertThrowsError(
            try Plinth(workspaceURL: "not a url", apiKey: "x")
        ) { error in
            guard case .invalidConfig = error as? PlinthError else {
                return XCTFail("expected .invalidConfig, got \(error)")
            }
        }
    }

    func testInitAcceptsValidDefaults() throws {
        let client = try Plinth(apiKey: "local-dev")
        XCTAssertNil(client.identityHTTP, "identity HTTP should be nil when not configured")
    }

    func testInitWiresIdentityWhenURLPresent() throws {
        let client = try Plinth(
            identityURL: "http://localhost:7425",
            apiKey: "local-dev"
        )
        XCTAssertNotNil(client.identityHTTP)
    }

    func testIdentityAccessorThrowsWhenNotConfigured() throws {
        let client = try Plinth(apiKey: "local-dev")
        XCTAssertThrowsError(try client.identity) { error in
            XCTAssertEqual((error as? PlinthError)?.code, PlinthErrorCode.identityNotConfigured)
        }
    }

    // MARK: - Authorization header

    func testRequestsCarryBearerHeader() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(method: "GET", path: "/v1/workspaces", json: TestFixtures.workspacesListJSON))
        _ = try await client.listWorkspaces()
        let captured = MockURLProtocol.captured()
        XCTAssertEqual(captured.first?.headers["Authorization"], "Bearer local-dev")
    }

    func testRequestsCarryUserAgent() async throws {
        let client = try makeClient()
        MockURLProtocol.enqueue(.json(method: "GET", path: "/v1/workspaces", json: TestFixtures.workspacesListJSON))
        _ = try await client.listWorkspaces()
        let captured = MockURLProtocol.captured()
        XCTAssertEqual(captured.first?.headers["User-Agent"], PlinthDefaults.userAgent)
    }
}
