// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package dev.plinth.sdk

/**
 * A per-workspace handle. Bundles the cached server [record] plus
 * typed sub-clients ([kv], [files]).
 *
 * Safe to share across coroutines: every sub-client holds a reference
 * to the same shared [HttpClient].
 */
class Workspace internal constructor(
    /** Cached snapshot of the workspace at the time this handle was built. */
    val record: WorkspaceRecord,
    private val http: HttpClient,
) {
    /** Stable workspace ID (e.g. `ws_…`). */
    val id: String get() = record.id

    /** Human-readable workspace name. */
    val name: String get() = record.name

    /** Versioned key-value store for this workspace. */
    val kv: KV = KV(http, record.id)

    /** Versioned file store for this workspace. */
    val files: Files = Files(http, record.id)
}
