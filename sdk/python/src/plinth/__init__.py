# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Plinth — the agent-native runtime SDK.

This is the public API surface. Most users only need::

    from plinth import Plinth

    client = Plinth(api_key="local-dev")
    ws = client.workspace("my-task")

For error handling, the typed exception classes are also exported here.
"""

from __future__ import annotations

from .agent import AgentContext
from .channels import ChannelsProxy
from .client import Plinth
from .exceptions import (
    TransactionFailed,
    TransactionInvalidStatus,
    TransactionNotFound,
)
from .exceptions import (
    BranchNotFound,
    ChannelNotFound,
    CostCapExceeded,
    FileNotFound,
    InvalidArguments,
    InvalidStepName,
    InvalidToken,
    InvalidWorkflowStep,
    KeyNotFound,
    LLMError,
    LLMProviderError,
    LLMProviderNotConfigured,
    LLMProviderNotInstalled,
    LLMRateLimited,
    LLMRetryExhausted,
    LeaseConflict,
    LeaseNotHeld,
    LockConflict,
    LockNotFound,
    LockNotHeld,
    MessageNotFound,
    NoHandlerError,
    NotFoundError,
    PlinthError,
    RateLimited,
    SchemaViolation,
    SnapshotNotFound,
    TokenExpired,
    TokenRevoked,
    ToolInvocationError,
    ToolNotFound,
    Unauthorized,
    ValidationError,
    WorkerNotFound,
    WorkflowNotFound,
    WorkflowStepNotFound,
    WorkspaceNotFound,
)
from .llm import LLMClient, LLMProvider
from .identity import (
    IdentityClient,
    TokenClaims,
    TokenInfo,
    TokenIssueResponse,
)
from .models import (
    AgentLimits,
    AuditEvent,
    Branch,
    Channel,
    ChannelMessage,
    ChannelSchema,
    CompensationSpec,
    DiffResult,
    DryRunResponse,
    FileEntry,
    DLQEntry,
    InvokeRequest,
    InvokeResponse,
    KVEntry,
    LLMMessage,
    LLMResponse,
    LLMStreamChunk,
    Lease,
    LimitsStatus,
    Lock,
    MergeResult,
    ReplayBatchResult,
    ResumeInfo,
    RevocationEntry,
    RevocationList,
    SchemaCheckResult,
    SigningKey,
    Snapshot,
    Tenant,
    Tool,
    ToolRegistration,
    Transaction,
    TransactionCall,
    TransactionResult,
    Worker,
    Workflow,
    WorkflowStep,
)
from .models import (
    Workspace as WorkspaceModel,
)
from .tools import ToolGateway as Tools
from .transactions import TransactionBuilder, TransactionsClient
from .workers import WorkersClient
from .workflow_runtime import HandlerContext, WorkflowRuntime
from .workflows import WorkflowHandle, WorkflowsProxy
from .workspace import FilesProxy, KVProxy, LocksProxy, SnapshotProxy, Workspace

__version__ = "0.3.0"

__all__ = [
    # Models
    "AgentLimits",
    "AgentContext",
    "AuditEvent",
    "Branch",
    # Exceptions
    "BranchNotFound",
    "Channel",
    "ChannelMessage",
    "ChannelNotFound",
    "ChannelSchema",
    "ChannelsProxy",
    "CompensationSpec",
    "CostCapExceeded",
    "DLQEntry",
    "DiffResult",
    "DryRunResponse",
    "FileEntry",
    "FileNotFound",
    "FilesProxy",
    # v0.5 — durable workflow executor
    "HandlerContext",
    # Identity
    "IdentityClient",
    "InvalidArguments",
    "InvalidStepName",
    "InvalidToken",
    "InvalidWorkflowStep",
    "InvokeRequest",
    "InvokeResponse",
    "KVEntry",
    "KVProxy",
    "KeyNotFound",
    # v1.2 — LLM layer
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "LLMProviderNotConfigured",
    "LLMProviderNotInstalled",
    "LLMRateLimited",
    "LLMResponse",
    "LLMRetryExhausted",
    "LLMStreamChunk",
    "Lease",
    "LeaseConflict",
    "LeaseNotHeld",
    "LimitsStatus",
    "Lock",
    "LockConflict",
    "LockNotFound",
    "LockNotHeld",
    "LocksProxy",
    "MergeResult",
    "MessageNotFound",
    "NoHandlerError",
    "NotFoundError",
    # Top-level facades
    "Plinth",
    "PlinthError",
    "RateLimited",
    "ReplayBatchResult",
    "ResumeInfo",
    "RevocationEntry",
    "RevocationList",
    "SchemaCheckResult",
    "SchemaViolation",
    "SigningKey",
    "Snapshot",
    "SnapshotNotFound",
    "SnapshotProxy",
    "Tenant",
    "TokenClaims",
    "TokenExpired",
    "TokenInfo",
    "TokenIssueResponse",
    "TokenRevoked",
    "Tool",
    "ToolInvocationError",
    "ToolNotFound",
    "ToolRegistration",
    "Tools",
    "Transaction",
    "TransactionBuilder",
    "TransactionCall",
    "TransactionFailed",
    "TransactionInvalidStatus",
    "TransactionNotFound",
    "TransactionResult",
    "TransactionsClient",
    "Unauthorized",
    "ValidationError",
    "Worker",
    "WorkerNotFound",
    "WorkersClient",
    "Workflow",
    "WorkflowHandle",
    "WorkflowNotFound",
    "WorkflowRuntime",
    "WorkflowStep",
    "WorkflowStepNotFound",
    "WorkflowsProxy",
    "Workspace",
    "WorkspaceModel",
    "WorkspaceNotFound",
    "__version__",
]
