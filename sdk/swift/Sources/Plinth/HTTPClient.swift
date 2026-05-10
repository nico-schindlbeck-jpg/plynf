// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Internal request helper bound to a single Plinth service base URL.
/// The ``Plinth`` facade owns one per backing service (workspace,
/// gateway, identity).
///
/// Exposed `public` so tests / advanced callers can poke at endpoints
/// the SDK hasn't wrapped yet, but the recommended surface is the typed
/// sub-clients.
public struct HTTPClient: Sendable {
    /// Base URL with trailing slashes stripped. All paths passed to
    /// request methods must start with `/`.
    public let baseURL: URL

    /// Bearer token attached to every outgoing request.
    public let apiKey: String

    /// `User-Agent` header value. Defaults to `plinth-sdk-swift/0.1.0`.
    public let userAgent: String

    /// Per-request timeout in seconds.
    public let timeout: TimeInterval

    /// `URLSession` actually used for the request. Tests inject a
    /// session whose configuration registers a mock `URLProtocol`.
    public let session: URLSession

    /// JSON encoder used for request bodies. Configured with
    /// `convertToSnakeCase` + ISO8601 dates so the wire format matches
    /// the contract.
    public let encoder: JSONEncoder

    /// JSON decoder used for responses. Configured symmetrically with
    /// the encoder.
    public let decoder: JSONDecoder

    public init(
        baseURL: URL,
        apiKey: String,
        userAgent: String = "plinth-sdk-swift/0.1.0",
        timeout: TimeInterval = 30,
        session: URLSession = .shared
    ) {
        self.baseURL = HTTPClient.canonicalise(baseURL)
        self.apiKey = apiKey
        self.userAgent = userAgent
        self.timeout = timeout
        self.session = session
        self.encoder = HTTPClient.makeEncoder()
        self.decoder = HTTPClient.makeDecoder()
    }

    // MARK: - Encoder / decoder

    /// Build a `JSONEncoder` configured for the Plinth wire format.
    public static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    /// Build a `JSONDecoder` configured for the Plinth wire format.
    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        // ISO8601 with fractional seconds is what the services emit.
        // Use a custom strategy so we accept both forms.
        decoder.dateDecodingStrategy = .custom { dec in
            let container = try dec.singleValueContainer()
            let raw = try container.decode(String.self)
            if let parsed = iso8601WithFractional.date(from: raw) {
                return parsed
            }
            if let parsed = iso8601Plain.date(from: raw) {
                return parsed
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Invalid ISO8601 date: \(raw)"
            )
        }
        return decoder
    }

    // MARK: - Verb helpers (typed)

    /// GET → decoded JSON.
    public func getJSON<T: Decodable>(
        _ path: String,
        query: [String: String?]? = nil,
        notFoundCode: String? = nil
    ) async throws -> T {
        let data = try await requestData(
            method: "GET",
            path: path,
            query: query,
            body: nil,
            contentType: nil,
            notFoundCode: notFoundCode
        )
        return try decode(data)
    }

    /// GET → raw bytes (e.g. file downloads).
    public func getData(
        _ path: String,
        query: [String: String?]? = nil,
        notFoundCode: String? = nil
    ) async throws -> Data {
        return try await requestData(
            method: "GET",
            path: path,
            query: query,
            body: nil,
            contentType: nil,
            notFoundCode: notFoundCode
        )
    }

    /// POST JSON body → decoded JSON response.
    public func postJSON<R: Encodable, T: Decodable>(
        _ path: String,
        body: R,
        query: [String: String?]? = nil,
        notFoundCode: String? = nil
    ) async throws -> T {
        let data = try await requestData(
            method: "POST",
            path: path,
            query: query,
            body: try encoder.encode(body),
            contentType: "application/json",
            notFoundCode: notFoundCode
        )
        return try decode(data)
    }

    /// PUT JSON body → decoded JSON response.
    public func putJSON<R: Encodable, T: Decodable>(
        _ path: String,
        body: R,
        query: [String: String?]? = nil,
        notFoundCode: String? = nil
    ) async throws -> T {
        let data = try await requestData(
            method: "PUT",
            path: path,
            query: query,
            body: try encoder.encode(body),
            contentType: "application/json",
            notFoundCode: notFoundCode
        )
        return try decode(data)
    }

    /// PUT raw bytes (e.g. file upload) → decoded JSON response.
    public func putRaw<T: Decodable>(
        _ path: String,
        body: Data,
        contentType: String,
        query: [String: String?]? = nil,
        notFoundCode: String? = nil
    ) async throws -> T {
        let data = try await requestData(
            method: "PUT",
            path: path,
            query: query,
            body: body,
            contentType: contentType,
            notFoundCode: notFoundCode
        )
        return try decode(data)
    }

    /// DELETE (discards response body).
    public func delete(
        _ path: String,
        query: [String: String?]? = nil,
        notFoundCode: String? = nil
    ) async throws {
        _ = try await requestData(
            method: "DELETE",
            path: path,
            query: query,
            body: nil,
            contentType: nil,
            notFoundCode: notFoundCode
        )
    }

    // MARK: - Core request

    /// Perform a request and return the raw response body.
    ///
    /// Non-2xx responses are mapped to ``PlinthError`` before this
    /// returns. The caller never sees a raw `HTTPURLResponse`.
    public func requestData(
        method: String,
        path: String,
        query: [String: String?]? = nil,
        body: Data? = nil,
        contentType: String? = nil,
        notFoundCode: String? = nil
    ) async throws -> Data {
        let url = try buildURL(path: path, query: query)
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json, application/octet-stream", forHTTPHeaderField: "Accept")
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        if let body = body {
            request.httpBody = body
            request.setValue(contentType ?? "application/octet-stream", forHTTPHeaderField: "Content-Type")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.dataForRequest(request)
        } catch let error as URLError {
            throw PlinthError.transport(error.localizedDescription)
        } catch {
            throw PlinthError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw PlinthError.transport("Non-HTTP response from \(url)")
        }

        if (200..<300).contains(http.statusCode) {
            return data
        }

        // Best-effort decode of the standard error envelope.
        let envelope = try? decoder.decode(PlinthErrorEnvelope.self, from: data)
        let retryAfter = http.value(forHTTPHeaderField: "Retry-After")
        throw plinthErrorFromEnvelope(
            statusCode: http.statusCode,
            envelope: envelope,
            retryAfterHeader: retryAfter,
            fallbackNotFoundCode: notFoundCode
        )
    }

    // MARK: - Internal helpers

    func decode<T: Decodable>(_ data: Data) throws -> T {
        // Allow empty body when the type is `EmptyResponse`.
        if T.self == EmptyResponse.self {
            // swiftlint:disable:next force_cast
            return EmptyResponse() as! T
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw PlinthError.decoding(error.localizedDescription)
        }
    }

    func buildURL(path: String, query: [String: String?]?) throws -> URL {
        let normalisedPath = path.hasPrefix("/") ? path : "/\(path)"
        guard var components = URLComponents(
            url: baseURL.appendingPathComponent(normalisedPath.trimmingCharacters(in: ["/"])),
            resolvingAgainstBaseURL: false
        ) else {
            throw PlinthError.transport("Invalid URL: \(baseURL.absoluteString)\(normalisedPath)")
        }
        // URLComponents.appendingPathComponent percent-encodes "/" — we
        // pre-encoded the path segments ourselves via path-helpers, so
        // restore the raw "/" separators that should pass through.
        components.percentEncodedPath = baseURL.path + normalisedPath
        if let query = query {
            var items: [URLQueryItem] = []
            for (key, value) in query {
                guard let value = value, !value.isEmpty else { continue }
                items.append(URLQueryItem(name: key, value: value))
            }
            if !items.isEmpty {
                components.queryItems = items
            }
        }
        guard let url = components.url else {
            throw PlinthError.transport("Failed to construct URL")
        }
        return url
    }

    static func canonicalise(_ url: URL) -> URL {
        let raw = url.absoluteString
        let trimmed = raw.hasSuffix("/") ? String(raw.dropLast()) : raw
        return URL(string: trimmed) ?? url
    }
}

/// Sentinel type for void-returning decoder paths.
public struct EmptyResponse: Decodable, Sendable {
    public init() {}
}

// MARK: - URLSession async adapter

extension URLSession {
    /// `data(for:)` is `async` on Apple platforms (iOS 15+, macOS 12+),
    /// but on Linux (`FoundationNetworking`) it's still callback-based.
    /// This shim picks the right implementation at compile time.
    func dataForRequest(_ request: URLRequest) async throws -> (Data, URLResponse) {
        #if canImport(FoundationNetworking)
        return try await withCheckedThrowingContinuation { continuation in
            let task = self.dataTask(with: request) { data, response, error in
                if let error = error {
                    continuation.resume(throwing: error)
                    return
                }
                guard let data = data, let response = response else {
                    continuation.resume(throwing: URLError(.badServerResponse))
                    return
                }
                continuation.resume(returning: (data, response))
            }
            task.resume()
        }
        #else
        return try await self.data(for: request)
        #endif
    }
}

// MARK: - Date parsing

@usableFromInline
let iso8601WithFractional: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

@usableFromInline
let iso8601Plain: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

// MARK: - Path encoding helpers

/// Percent-encode a single path segment (KV keys, workspace IDs, tool
/// IDs, etc.) so embedded `/` characters don't become path separators.
@inlinable
public func encodePathSegment(_ s: String) -> String {
    return s.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(.init(charactersIn: "/"))) ?? s
}

/// Percent-encode a file path while preserving `/` as the segment
/// separator.
@inlinable
public func encodeFilePath(_ p: String) -> String {
    let trimmed = p.hasPrefix("/") ? String(p.dropFirst()) : p
    return trimmed
        .split(separator: "/", omittingEmptySubsequences: false)
        .map { encodePathSegment(String($0)) }
        .joined(separator: "/")
}
