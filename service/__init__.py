"""HTTP service boundary for the Agent platform.

The service exposes ``AgentApplicationService`` over HTTP so a third-party
system can submit questions, stream events, query Runs/Threads/Checkpoints and
resume an interrupted run. Runtime, Compiler and Tool Registry logic is not
duplicated here: every request is delegated to the same application service the
CLI uses.
"""

from service.api import ServiceError
from service.app import AgentService, AgentHTTPServer, create_server
from service.config import ServiceConfigurationError, ServiceSettings, Tenant

__all__ = [
    "AgentHTTPServer",
    "AgentService",
    "ServiceConfigurationError",
    "ServiceError",
    "ServiceSettings",
    "Tenant",
    "create_server",
]
