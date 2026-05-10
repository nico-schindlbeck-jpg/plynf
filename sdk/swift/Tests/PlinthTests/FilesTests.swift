// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
import XCTest

@testable import Plinth

final class FilesTests: XCTestCase {
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

    func testWriteTextReturnsMetadata() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "PUT",
            path: "/v1/workspaces/ws_test_1/files/report.md",
            json: TestFixtures.fileEntryJSON
        ))
        let meta = try await ws.files.write(path: "report.md", text: "# Report\n")
        XCTAssertEqual(meta.path, "report.md")
        XCTAssertEqual(meta.version, 1)
    }

    func testWriteTextSendsUTF8Body() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "PUT",
            path: "/v1/workspaces/ws_test_1/files/report.md",
            json: TestFixtures.fileEntryJSON
        ))
        _ = try await ws.files.write(path: "report.md", text: "hello")
        let captured = MockURLProtocol.captured().first
        XCTAssertEqual(captured?.body, Data("hello".utf8))
        XCTAssertEqual(captured?.headers["Content-Type"], "text/plain; charset=utf-8")
    }

    func testReadReturnsBytes() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(MockResponse(
            method: "GET",
            path: "/v1/workspaces/ws_test_1/files/report.md",
            statusCode: 200,
            body: Data("# Report\n".utf8),
            headers: ["Content-Type": "text/markdown"]
        ))
        let data = try await ws.files.read(path: "report.md")
        XCTAssertEqual(String(data: data, encoding: .utf8), "# Report\n")
    }

    func testReadTextDecodesUTF8() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(MockResponse(
            method: "GET",
            path: "/v1/workspaces/ws_test_1/files/report.md",
            statusCode: 200,
            body: Data("hello".utf8),
            headers: ["Content-Type": "text/plain"]
        ))
        let text = try await ws.files.readText(path: "report.md")
        XCTAssertEqual(text, "hello")
    }

    func testReadMapsFileNotFound() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "GET",
            path: "/v1/workspaces/ws_test_1/files/missing.md",
            statusCode: 404,
            json: """
                {"error": {"code": "FILE_NOT_FOUND", "message": "nope"}}
                """
        ))
        do {
            _ = try await ws.files.read(path: "missing.md")
            XCTFail("expected throw")
        } catch PlinthError.fileNotFound {
            // expected
        } catch {
            XCTFail("expected .fileNotFound, got \(error)")
        }
    }

    func testNestedPathsAreEncodedSegmentWise() async throws {
        let ws = try makeWorkspace()
        MockURLProtocol.enqueue(.json(
            method: "PUT",
            path: "/v1/workspaces/ws_test_1/files/dir/sub/report.md",
            json: TestFixtures.fileEntryJSON
        ))
        _ = try await ws.files.write(path: "dir/sub/report.md", text: "x")
        let path = MockURLProtocol.captured().first?.url.path
        XCTAssertEqual(path, "/v1/workspaces/ws_test_1/files/dir/sub/report.md")
    }
}
