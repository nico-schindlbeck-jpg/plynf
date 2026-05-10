// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

import Foundation

// Models in this file mirror the Pydantic + TypeScript types defined in
// CONTRACTS.md. Field names use Swift conventions (camelCase); the
// JSON encoder is configured for `convertFromSnakeCase` /
// `convertToSnakeCase` so we get the wire format for free.
//
// The `Workspace` wire model lives in Workspace.swift as
// ``WorkspaceRecord`` so the more ergonomic ``Workspace`` type (a
// handle bundling sub-clients) can carry the primary name.

// MARK: - KV entries

public struct KVEntry: Codable, Sendable, Equatable {
    public let workspaceId: String
    public let key: String
    public let value: AnyCodableValue
    public let version: Int
    public let createdAt: Date
    public let deleted: Bool
    public let branchId: String?

    public init(
        workspaceId: String,
        key: String,
        value: AnyCodableValue,
        version: Int,
        createdAt: Date,
        deleted: Bool = false,
        branchId: String? = nil
    ) {
        self.workspaceId = workspaceId
        self.key = key
        self.value = value
        self.version = version
        self.createdAt = createdAt
        self.deleted = deleted
        self.branchId = branchId
    }
}

public struct FileEntry: Codable, Sendable, Equatable {
    public let workspaceId: String
    public let path: String
    public let size: Int64
    public let sha256: String
    public let contentType: String
    public let version: Int
    public let createdAt: Date
    public let deleted: Bool
    public let branchId: String?

    public init(
        workspaceId: String,
        path: String,
        size: Int64,
        sha256: String,
        contentType: String,
        version: Int,
        createdAt: Date,
        deleted: Bool = false,
        branchId: String? = nil
    ) {
        self.workspaceId = workspaceId
        self.path = path
        self.size = size
        self.sha256 = sha256
        self.contentType = contentType
        self.version = version
        self.createdAt = createdAt
        self.deleted = deleted
        self.branchId = branchId
    }
}

// MARK: - Tools

public enum ToolTransport: String, Codable, Sendable {
    case http, stdio
}

public enum ToolSideEffects: String, Codable, Sendable {
    case none, read, write
}

public enum ToolAuthMethod: String, Codable, Sendable {
    case none
    case bearer
    case oauth2
}

public struct ToolRegistration: Codable, Sendable {
    public let toolId: String
    public let name: String
    public let description: String
    public let transport: ToolTransport
    public let endpoint: String
    public let inputSchema: [String: AnyCodableValue]
    public let outputSchema: [String: AnyCodableValue]
    public let idempotent: Bool?
    public let sideEffects: ToolSideEffects?
    public let cacheTtlSeconds: Int?
    public let authMethod: ToolAuthMethod?
    public let authConfig: [String: AnyCodableValue]?

    public init(
        toolId: String,
        name: String,
        description: String,
        transport: ToolTransport,
        endpoint: String,
        inputSchema: [String: AnyCodableValue] = [:],
        outputSchema: [String: AnyCodableValue] = [:],
        idempotent: Bool? = nil,
        sideEffects: ToolSideEffects? = nil,
        cacheTtlSeconds: Int? = nil,
        authMethod: ToolAuthMethod? = nil,
        authConfig: [String: AnyCodableValue]? = nil
    ) {
        self.toolId = toolId
        self.name = name
        self.description = description
        self.transport = transport
        self.endpoint = endpoint
        self.inputSchema = inputSchema
        self.outputSchema = outputSchema
        self.idempotent = idempotent
        self.sideEffects = sideEffects
        self.cacheTtlSeconds = cacheTtlSeconds
        self.authMethod = authMethod
        self.authConfig = authConfig
    }
}

public struct Tool: Codable, Sendable {
    public let toolId: String
    public let name: String
    public let description: String
    public let transport: ToolTransport
    public let endpoint: String
    public let inputSchema: [String: AnyCodableValue]
    public let outputSchema: [String: AnyCodableValue]
    public let idempotent: Bool?
    public let sideEffects: ToolSideEffects?
    public let cacheTtlSeconds: Int?
    public let createdAt: Date
    public let updatedAt: Date
}

public struct InvokeRequest: Codable, Sendable {
    public let toolId: String
    public let arguments: [String: AnyCodableValue]
    public let workspaceId: String?
    public let agentId: String?
    public let cache: Bool?
    public let idempotencyKey: String?

    public init(
        toolId: String,
        arguments: [String: AnyCodableValue],
        workspaceId: String? = nil,
        agentId: String? = nil,
        cache: Bool? = nil,
        idempotencyKey: String? = nil
    ) {
        self.toolId = toolId
        self.arguments = arguments
        self.workspaceId = workspaceId
        self.agentId = agentId
        self.cache = cache
        self.idempotencyKey = idempotencyKey
    }
}

public struct InvokeResponse: Codable, Sendable {
    public let toolId: String
    public let arguments: [String: AnyCodableValue]
    public let result: AnyCodableValue
    public let cached: Bool
    public let durationMs: Int
    public let auditId: String
    public let costEstimateUsd: Double

    public init(
        toolId: String,
        arguments: [String: AnyCodableValue],
        result: AnyCodableValue,
        cached: Bool,
        durationMs: Int,
        auditId: String,
        costEstimateUsd: Double
    ) {
        self.toolId = toolId
        self.arguments = arguments
        self.result = result
        self.cached = cached
        self.durationMs = durationMs
        self.auditId = auditId
        self.costEstimateUsd = costEstimateUsd
    }
}

// MARK: - Identity

public struct TokenIssueRequest: Codable, Sendable {
    public let agentId: String
    public let tenantId: String?
    public let scopes: [String]
    public let workspaceId: String?
    public let ttlSeconds: Int?
    public let metadata: [String: AnyCodableValue]?

    public init(
        agentId: String,
        tenantId: String? = nil,
        scopes: [String],
        workspaceId: String? = nil,
        ttlSeconds: Int? = nil,
        metadata: [String: AnyCodableValue]? = nil
    ) {
        self.agentId = agentId
        self.tenantId = tenantId
        self.scopes = scopes
        self.workspaceId = workspaceId
        self.ttlSeconds = ttlSeconds
        self.metadata = metadata
    }
}

public struct TokenClaims: Codable, Sendable {
    public let sub: String
    public let iss: String
    public let aud: String
    public let iat: Int64
    public let exp: Int64
    public let jti: String
    public let agentId: String
    public let tenantId: String
    public let workspaceId: String?
    public let scopes: [String]
    public let rateLimit: [String: AnyCodableValue]?
}

public struct TokenIssueResponse: Codable, Sendable {
    public let token: String
    public let jti: String
    public let expiresAt: Date
    public let claims: TokenClaims
}

public struct TokenInfo: Codable, Sendable {
    public let jti: String
    public let agentId: String
    public let tenantId: String
    public let issuedAt: Date
    public let expiresAt: Date
    public let revoked: Bool
    public let revokedAt: Date?
    public let metadata: [String: AnyCodableValue]?
}

// MARK: - List response envelopes

struct KVHistoryResponse: Decodable, Sendable {
    let versions: [KVEntry]
}

struct KVListResponse: Decodable, Sendable {
    let entries: [KVEntry]
}

struct FilesListResponse: Decodable, Sendable {
    let files: [FileEntry]
}

struct ToolsListResponse: Decodable, Sendable {
    let tools: [Tool]
}

// MARK: - AnyCodableValue

/// A type-erased wrapper for arbitrary JSON-encodable values.
///
/// Used in places where the wire schema is `Any` (KV values, tool
/// arguments, metadata blobs). Conforms to `Codable` so it round-trips
/// through `JSONEncoder`/`JSONDecoder` without surprises.
///
/// - Note: Conforms to `Sendable` since the wrapped storage uses only
///   value-type primitives (`String`, `Int64`, `Double`, `Bool`,
///   `[AnyCodableValue]`, `[String: AnyCodableValue]`).
public enum AnyCodableValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    case int(Int64)
    case double(Double)
    case string(String)
    case array([AnyCodableValue])
    case object([String: AnyCodableValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let v = try? container.decode(Bool.self) {
            self = .bool(v)
        } else if let v = try? container.decode(Int64.self) {
            self = .int(v)
        } else if let v = try? container.decode(Double.self) {
            self = .double(v)
        } else if let v = try? container.decode(String.self) {
            self = .string(v)
        } else if let v = try? container.decode([AnyCodableValue].self) {
            self = .array(v)
        } else if let v = try? container.decode([String: AnyCodableValue].self) {
            self = .object(v)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let v): try container.encode(v)
        case .int(let v): try container.encode(v)
        case .double(let v): try container.encode(v)
        case .string(let v): try container.encode(v)
        case .array(let v): try container.encode(v)
        case .object(let v): try container.encode(v)
        }
    }

    /// Convenience: extract a `String` when the value is one, else nil.
    public var stringValue: String? {
        if case .string(let v) = self { return v }
        return nil
    }

    /// Convenience: extract a numeric value as `Double`, supporting both
    /// int and double cases.
    public var doubleValue: Double? {
        switch self {
        case .int(let v): return Double(v)
        case .double(let v): return v
        default: return nil
        }
    }

    /// Convenience: extract an `Int` from int or double-with-no-frac.
    public var intValue: Int? {
        switch self {
        case .int(let v): return Int(v)
        case .double(let v) where v.rounded() == v: return Int(v)
        default: return nil
        }
    }

    /// Convenience: extract a `Bool` when the value is one, else nil.
    public var boolValue: Bool? {
        if case .bool(let v) = self { return v }
        return nil
    }

    /// Convenience: extract a `[String: AnyCodableValue]` object form.
    public var objectValue: [String: AnyCodableValue]? {
        if case .object(let v) = self { return v }
        return nil
    }

    /// Build from a Foundation `Any` value. Returns nil if the value's
    /// type isn't representable in JSON.
    public static func from(_ value: Any?) -> AnyCodableValue {
        guard let value = value else { return .null }
        switch value {
        case let v as Bool: return .bool(v)
        case let v as Int: return .int(Int64(v))
        case let v as Int64: return .int(v)
        case let v as Double: return .double(v)
        case let v as Float: return .double(Double(v))
        case let v as String: return .string(v)
        case let v as [Any]: return .array(v.map(AnyCodableValue.from))
        case let v as [String: Any]:
            var dict: [String: AnyCodableValue] = [:]
            for (k, val) in v {
                dict[k] = .from(val)
            }
            return .object(dict)
        case let v as AnyCodableValue: return v
        default: return .null
        }
    }
}
