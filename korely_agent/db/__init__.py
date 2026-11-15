"""Database package.

Re-exports the models so call sites can write `from ..db import AgentFact`
rather than reaching into `..db.models`. Nothing here builds an engine or reads
configuration: importing this must stay free.
"""
from .base import Base, get_db, get_engine, get_own_sessionmaker, get_sessionmaker
from .models import (
    AuditEvent,
    AgentApiKey,
    AgentBatchJob,
    AgentEntityAlias,
    AgentFact,
    AgentFactEndedBy,
    AgentForgetAudit,
    AgentIdempotencyKey,
    AgentLlmUsage,
    AgentMemory,
    AgentObjectLink,
    AgentProject,
    AgentRateLimit,
    AgentReadMark,
    AgentWebhookDelivery,
    AgentWebhookEndpoint,
    InstallSetting,
    InstallState,
    LoginAttempt,
    Operator,
    OperatorSession,
    PredicateAlias,
    PredicateCluster,
    Tenant,
)

__all__ = [
    "Base", "get_db", "get_engine", "get_own_sessionmaker", "get_sessionmaker",
    "AuditEvent", "AgentApiKey", "AgentBatchJob", "AgentEntityAlias", "AgentFact",
    "AgentFactEndedBy", "AgentForgetAudit", "AgentIdempotencyKey", "AgentLlmUsage", "AgentMemory", "AgentObjectLink",
    "AgentProject", "AgentRateLimit", "AgentReadMark",
    "AgentWebhookDelivery", "AgentWebhookEndpoint",
    "InstallSetting", "InstallState", "LoginAttempt", "Operator", "OperatorSession",
    "PredicateAlias", "PredicateCluster", "Tenant",
]

# The Cloud's tables (KORELY_MODE=hosted) are on the metadata wherever the
# Cloud's package is: there is one chain of migrations, and `alembic check` and
# the test schema must see every table that exists. An install of one's own
# leaves them empty and never reads them. A tree without the package (the
# public repository) has the tables from the same migrations and none of their
# models: alembic/env.py leaves them out of its comparison
# (core/extensions.cloud_tables_without_models).
from ..core import extensions as _extensions  # noqa: E402

_extensions.cloud("models")
