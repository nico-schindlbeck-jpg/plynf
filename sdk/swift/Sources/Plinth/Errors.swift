// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation

/// Stable string identifiers for every Plinth error code emitted by the
/// services. Mirrors the maps in the Python, TypeScript, and Go SDKs.
///
/// These are exposed as a namespace of `String` constants so callers can
/// match against ``PlinthError/code`` without depending on the specific
/// case enum.
public enum PlinthErrorCode {
    // 400 — validation
    public static let invalidArguments = "INVALID_ARGUMENTS"
    public static let schemaViolation = "SCHEMA_VIOLATION"

    // 401 — auth
    public static let unauthorized = "UNAUTHORIZED"
    public static let invalidToken = "INVALID_TOKEN"
    public static let tokenExpired = "TOKEN_EXPIRED"
    public static let tokenRevoked = "TOKEN_REVOKED"

    // 404 — not found
    public static let workspaceNotFound = "WORKSPACE_NOT_FOUND"
    public static let keyNotFound = "KEY_NOT_FOUND"
    public static let fileNotFound = "FILE_NOT_FOUND"
    public static let snapshotNotFound = "SNAPSHOT_NOT_FOUND"
    public static let branchNotFound = "BRANCH_NOT_FOUND"
    public static let toolNotFound = "TOOL_NOT_FOUND"
    public static let signingKeyNotFound = "SIGNING_KEY_NOT_FOUND"

    // 429 — rate limits / cost caps
    public static let rateLimited = "RATE_LIMITED"
    public static let costCapExceeded = "COST_CAP_EXCEEDED"

    // 5xx / client-side
    public static let toolInvocationFailed = "TOOL_INVOCATION_FAILED"
    public static let invalidConfig = "INVALID_CONFIG"
    public static let connectionError = "CONNECTION_ERROR"
    public static let internalError = "INTERNAL_ERROR"
    public static let identityNotConfigured = "IDENTITY_NOT_CONFIGURED"
}

/// Single error type returned by every Plinth SDK call.
///
/// The enum is structured so callers can branch on common conditions
/// without unpacking the full envelope, but every case carries the
/// underlying ``PlinthErrorEnvelope`` for callers that need richer
/// diagnostics (status code, raw body, retry hints).
///
/// ```swift
/// do {
///     try await ws.kv.get(key: "missing")
/// } catch PlinthError.keyNotFound {
///     // recover
/// } catch let PlinthError.server(_, _, message) {
///     print("server error: \(message)")
/// }
/// ```
public enum PlinthError: Error, LocalizedError, Equatable {
    /// The SDK was constructed with invalid configuration (missing API
    /// key, malformed URL, etc.). Surfaced eagerly so config mistakes
    /// don't sneak past as opaque HTTP errors at first call.
    case invalidConfig(String)

    /// 401 — the bearer token was rejected.
    case unauthorized(String)

    /// 404 with a recognised resource code.
    case notFound(code: String, message: String)

    /// Convenience cases for the most common 404 codes. The HTTP layer
    /// canonicalises the server response and routes well-known codes
    /// onto these so callers can write `catch .workspaceNotFound` etc.
    case workspaceNotFound
    case keyNotFound
    case fileNotFound
    case toolNotFound

    /// 429 — the gateway is throttling the caller. ``retryAfter`` is in
    /// seconds; nil when the server didn't include a hint.
    case rateLimited(retryAfter: TimeInterval?)

    /// 429 with a `COST_CAP_EXCEEDED` envelope.
    case quotaExceeded(quota: String)

    /// 4xx/5xx response that didn't fit the convenience cases above.
    case server(statusCode: Int, code: String, message: String)

    /// JSON decoding failed on a 2xx response — usually a schema drift.
    case decoding(String)

    /// Network / transport-level error before we got a status. The
    /// associated string is the underlying ``URLError``'s description.
    case transport(String)

    /// The caller asked for ``Plinth/identity`` but didn't pass an
    /// ``identityURL`` when constructing the client.
    case identityNotConfigured

    // MARK: - Surfaces -

    /// The stable string code from the underlying envelope, suitable for
    /// programmatic matching (e.g. logging dashboards).
    public var code: String {
        switch self {
        case .invalidConfig: return PlinthErrorCode.invalidConfig
        case .unauthorized: return PlinthErrorCode.unauthorized
        case .notFound(let code, _): return code
        case .workspaceNotFound: return PlinthErrorCode.workspaceNotFound
        case .keyNotFound: return PlinthErrorCode.keyNotFound
        case .fileNotFound: return PlinthErrorCode.fileNotFound
        case .toolNotFound: return PlinthErrorCode.toolNotFound
        case .rateLimited: return PlinthErrorCode.rateLimited
        case .quotaExceeded: return PlinthErrorCode.costCapExceeded
        case .server(_, let code, _): return code
        case .decoding: return PlinthErrorCode.internalError
        case .transport: return PlinthErrorCode.connectionError
        case .identityNotConfigured: return PlinthErrorCode.identityNotConfigured
        }
    }

    public var errorDescription: String? {
        switch self {
        case .invalidConfig(let message):
            return "Plinth invalid config: \(message)"
        case .unauthorized(let message):
            return "Plinth unauthorized: \(message)"
        case .notFound(let code, let message):
            return "Plinth not found [\(code)]: \(message)"
        case .workspaceNotFound:
            return "Plinth workspace not found"
        case .keyNotFound:
            return "Plinth KV key not found"
        case .fileNotFound:
            return "Plinth file not found"
        case .toolNotFound:
            return "Plinth tool not found"
        case .rateLimited(let retryAfter):
            if let retryAfter = retryAfter {
                return "Plinth rate limited (retry after \(retryAfter)s)"
            }
            return "Plinth rate limited"
        case .quotaExceeded(let quota):
            return "Plinth quota exceeded: \(quota)"
        case .server(let status, let code, let message):
            return "Plinth server error (\(status), \(code)): \(message)"
        case .decoding(let message):
            return "Plinth response decoding failed: \(message)"
        case .transport(let message):
            return "Plinth transport error: \(message)"
        case .identityNotConfigured:
            return "Plinth identity service not configured: pass identityURL to Plinth(...)"
        }
    }
}

/// Decoded form of the standard `{ "error": { ... } }` envelope returned
/// by every Plinth service.
///
/// Exposed publicly so callers who want to introspect raw server output
/// (e.g. for logging) can do so without re-parsing.
public struct PlinthErrorEnvelope: Decodable, Sendable, Equatable {
    public let code: String
    public let message: String
    public let details: [String: AnyCodableValue]?

    private enum CodingKeys: String, CodingKey {
        case error
    }

    private enum InnerKeys: String, CodingKey {
        case code, message, details
    }

    public init(code: String, message: String, details: [String: AnyCodableValue]? = nil) {
        self.code = code
        self.message = message
        self.details = details
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let inner = try container.nestedContainer(keyedBy: InnerKeys.self, forKey: .error)
        self.code = try inner.decode(String.self, forKey: .code)
        self.message = try inner.decodeIfPresent(String.self, forKey: .message) ?? ""
        self.details = try inner.decodeIfPresent([String: AnyCodableValue].self, forKey: .details)
    }
}

/// Maps a (status, code) pair plus the raw envelope onto the most
/// specific ``PlinthError`` case the SDK exposes.
///
/// The HTTP layer calls this from ``HTTPClient/handleResponse``; pulled
/// out as a free function so it can be unit-tested in isolation.
@inlinable
public func plinthErrorFromEnvelope(
    statusCode: Int,
    envelope: PlinthErrorEnvelope?,
    retryAfterHeader: String?,
    fallbackNotFoundCode: String? = nil
) -> PlinthError {
    let envelopeCode = envelope?.code
    let envelopeMessage = envelope?.message ?? ""

    // Determine canonical code: envelope > 404-fallback > status map.
    var code = envelopeCode ?? ""
    if code.isEmpty {
        if statusCode == 404, let fallback = fallbackNotFoundCode {
            code = fallback
        } else if let mapped = statusToCode[statusCode] {
            code = mapped
        } else {
            code = PlinthErrorCode.internalError
        }
    }

    let message = envelopeMessage.isEmpty
        ? (HTTPURLResponse.localizedString(forStatusCode: statusCode))
        : envelopeMessage

    switch code {
    case PlinthErrorCode.workspaceNotFound: return .workspaceNotFound
    case PlinthErrorCode.keyNotFound: return .keyNotFound
    case PlinthErrorCode.fileNotFound: return .fileNotFound
    case PlinthErrorCode.toolNotFound: return .toolNotFound
    case PlinthErrorCode.unauthorized,
         PlinthErrorCode.invalidToken,
         PlinthErrorCode.tokenExpired,
         PlinthErrorCode.tokenRevoked:
        return .unauthorized(message)
    case PlinthErrorCode.rateLimited:
        return .rateLimited(retryAfter: parseRetryAfter(retryAfterHeader, envelope: envelope))
    case PlinthErrorCode.costCapExceeded:
        let quota = (envelope?.details?["limit_type"]?.stringValue) ?? "cost"
        return .quotaExceeded(quota: quota)
    default:
        if statusCode == 404 {
            return .notFound(code: code, message: message)
        }
        return .server(statusCode: statusCode, code: code, message: message)
    }
}

@usableFromInline
let statusToCode: [Int: String] = [
    400: PlinthErrorCode.invalidArguments,
    401: PlinthErrorCode.unauthorized,
    429: PlinthErrorCode.rateLimited,
]

@usableFromInline
func parseRetryAfter(_ header: String?, envelope: PlinthErrorEnvelope?) -> TimeInterval? {
    if let raw = envelope?.details?["retry_after_seconds"]?.doubleValue {
        return raw
    }
    if let header = header, let v = Double(header) {
        return v
    }
    return nil
}
