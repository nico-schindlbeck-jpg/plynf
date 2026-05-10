// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

// Branches API surface.
//
// The branch lifecycle (create, list, merge, delete, scoped writes via
// WithBranch) is exposed as methods on *WorkspaceClient — see
// workspace.go — because that's how callers reason about it ("create a
// branch on this workspace, write through this branch view, merge it
// back").
//
// This file exists as the spec'd home for a future BranchesProxy if we
// later want to mirror the Python SDK's `ws.branches.create(...)`
// shape. For v0.1 the WorkspaceClient methods are sufficient and idiomatic.
