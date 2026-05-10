// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

@testable import Plinth

/// A canned response the mock server will return for a matching request.
struct MockResponse: Sendable {
    let method: String
    /// Path including the leading `/`. Matched against the request's
    /// `URL.path`. Use `*` for "match anything".
    let path: String
    let statusCode: Int
    let body: Data
    let headers: [String: String]

    init(
        method: String,
        path: String,
        statusCode: Int = 200,
        body: Data = Data(),
        headers: [String: String] = ["Content-Type": "application/json"]
    ) {
        self.method = method.uppercased()
        self.path = path
        self.statusCode = statusCode
        self.body = body
        self.headers = headers
    }

    static func json(
        method: String,
        path: String,
        statusCode: Int = 200,
        json: String,
        headers: [String: String] = ["Content-Type": "application/json"]
    ) -> MockResponse {
        MockResponse(
            method: method,
            path: path,
            statusCode: statusCode,
            body: Data(json.utf8),
            headers: headers
        )
    }
}

/// A captured request (for assertions on what the SDK actually sent).
struct CapturedRequest: Sendable {
    let method: String
    let url: URL
    let body: Data?
    let headers: [String: String]
}

/// URLProtocol-based mock that intercepts every request sent through a
/// session whose `configuration.protocolClasses` includes this type.
///
/// The mutable state lives in a class-level `Box` so tests can program
/// queues of canned responses and inspect captured requests. Each test
/// must call ``reset()`` in `setUp` so state doesn't bleed between
/// tests in the same process.
final class MockURLProtocol: URLProtocol {
    /// Locked-down container for state shared across all `MockURLProtocol`
    /// instances. URLSession instantiates a fresh `MockURLProtocol` per
    /// request, so per-instance state would be lost.
    final class Box: @unchecked Sendable {
        private let lock = NSLock()
        private var responses: [MockResponse] = []
        private var captured: [CapturedRequest] = []

        func enqueue(_ response: MockResponse) {
            lock.lock(); defer { lock.unlock() }
            responses.append(response)
        }

        func capture(_ request: CapturedRequest) {
            lock.lock(); defer { lock.unlock() }
            captured.append(request)
        }

        func consume(for request: URLRequest) -> MockResponse? {
            lock.lock(); defer { lock.unlock() }
            let method = request.httpMethod?.uppercased() ?? "GET"
            let path = request.url?.path ?? ""
            // First exact (method, path) match wins.
            for i in 0..<responses.count {
                let response = responses[i]
                if response.method == method && (response.path == "*" || response.path == path) {
                    responses.remove(at: i)
                    return response
                }
            }
            return nil
        }

        func capturedRequests() -> [CapturedRequest] {
            lock.lock(); defer { lock.unlock() }
            return captured
        }

        func reset() {
            lock.lock(); defer { lock.unlock() }
            responses.removeAll()
            captured.removeAll()
        }
    }

    static let box = Box()

    static func reset() {
        box.reset()
    }

    static func enqueue(_ response: MockResponse) {
        box.enqueue(response)
    }

    static func captured() -> [CapturedRequest] {
        box.capturedRequests()
    }

    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: config)
    }

    // MARK: - URLProtocol implementation

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        // Capture the body. URLProtocol drops `httpBody` for some
        // request types — but in tests where the session is the only
        // user, it's always populated.
        let captured = CapturedRequest(
            method: request.httpMethod?.uppercased() ?? "GET",
            url: request.url ?? URL(string: "about:blank")!,
            body: request.httpBody ?? request.httpBodyStream.flatMap { stream in
                stream.open()
                defer { stream.close() }
                var data = Data()
                let bufferSize = 1024
                let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
                defer { buffer.deallocate() }
                while stream.hasBytesAvailable {
                    let read = stream.read(buffer, maxLength: bufferSize)
                    if read <= 0 { break }
                    data.append(buffer, count: read)
                }
                return data
            },
            headers: request.allHTTPHeaderFields ?? [:]
        )
        MockURLProtocol.box.capture(captured)

        guard let response = MockURLProtocol.box.consume(for: request) else {
            let error = NSError(domain: "MockURLProtocol", code: 0, userInfo: [
                NSLocalizedDescriptionKey: "No canned response for \(captured.method) \(captured.url.path)"
            ])
            client?.urlProtocol(self, didFailWithError: error)
            return
        }
        let httpResponse = HTTPURLResponse(
            url: request.url ?? URL(string: "about:blank")!,
            statusCode: response.statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: response.headers
        )!
        client?.urlProtocol(self, didReceive: httpResponse, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

// MARK: - Test helpers

enum TestFixtures {
    static let workspaceJSON = """
        {
            "id": "ws_test_1",
            "name": "my-research",
            "created_at": "2026-05-01T10:00:00Z",
            "updated_at": "2026-05-01T10:00:00Z",
            "metadata": {}
        }
        """

    static let workspacesListJSON = """
        {
            "workspaces": [
                {
                    "id": "ws_test_1",
                    "name": "my-research",
                    "created_at": "2026-05-01T10:00:00Z",
                    "updated_at": "2026-05-01T10:00:00Z",
                    "metadata": {}
                }
            ]
        }
        """

    static let kvEntryJSON = """
        {
            "workspace_id": "ws_test_1",
            "key": "topic",
            "value": "renewable energy",
            "version": 1,
            "created_at": "2026-05-01T10:00:00Z",
            "deleted": false
        }
        """

    static let kvHistoryJSON = """
        {
            "versions": [
                {
                    "workspace_id": "ws_test_1",
                    "key": "topic",
                    "value": "fossil fuels",
                    "version": 1,
                    "created_at": "2026-05-01T09:00:00Z",
                    "deleted": false
                },
                {
                    "workspace_id": "ws_test_1",
                    "key": "topic",
                    "value": "renewable energy",
                    "version": 2,
                    "created_at": "2026-05-01T10:00:00Z",
                    "deleted": false
                }
            ]
        }
        """

    static let fileEntryJSON = """
        {
            "workspace_id": "ws_test_1",
            "path": "report.md",
            "size": 32,
            "sha256": "deadbeef",
            "content_type": "text/markdown",
            "version": 1,
            "created_at": "2026-05-01T10:00:00Z",
            "deleted": false
        }
        """

    static let invokeResponseJSON = """
        {
            "tool_id": "web.fetch",
            "arguments": {"url": "mock://example"},
            "result": {"content": "ok"},
            "cached": false,
            "duration_ms": 12,
            "audit_id": "evt_1",
            "cost_estimate_usd": 0.0
        }
        """

    static let tokenIssueResponseJSON = """
        {
            "token": "ey.fake.jwt",
            "jti": "jti_1",
            "expires_at": "2026-05-01T11:00:00Z",
            "claims": {
                "sub": "my-agent",
                "iss": "http://localhost:7425",
                "aud": "plinth",
                "iat": 1000,
                "exp": 4600,
                "jti": "jti_1",
                "agent_id": "my-agent",
                "tenant_id": "default",
                "scopes": ["tool:web.fetch:read"]
            }
        }
        """

    static let tokenClaimsJSON = """
        {
            "sub": "my-agent",
            "iss": "http://localhost:7425",
            "aud": "plinth",
            "iat": 1000,
            "exp": 4600,
            "jti": "jti_1",
            "agent_id": "my-agent",
            "tenant_id": "default",
            "scopes": ["tool:web.fetch:read"]
        }
        """
}

func makeClient() throws -> Plinth {
    return try Plinth(
        workspaceURL: "http://localhost:7421",
        gatewayURL: "http://localhost:7422",
        identityURL: "http://localhost:7425",
        apiKey: "local-dev",
        urlSession: MockURLProtocol.session()
    )
}
