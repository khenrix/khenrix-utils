"""Errors raised by the durable fanout artifact boundary."""
from __future__ import annotations


class FanoutError(RuntimeError):
    """Base class for failures in the fanout runtime."""


class PlanValidationError(FanoutError):
    """A versioned execution plan is malformed, ambiguous, or unsafe to schedule."""


class CompilerError(FanoutError):
    """An approved source plan or its CLI-authored draft is ambiguous or invalid."""


class ArtifactError(FanoutError):
    """Base class for artifact publication and consumption failures."""


class ArtifactPathError(ArtifactError):
    """An artifact name would escape the run root or follow a symlink."""


class ArtifactIntegrityError(ArtifactError):
    """Bytes read from storage do not match their durable reference."""


class ArtifactQuotaError(ArtifactError):
    """An artifact would exceed a configured storage limit."""


class ArtifactExistsError(ArtifactError):
    """An exclusive artifact publication found an existing name."""


class ProcessError(FanoutError):
    """Base class for fanout child-process boundary failures."""


class ProcessValidationError(ProcessError):
    """A process command or child environment violates the execution contract."""


class SlotTimeoutError(ProcessError):
    """No machine-wide executor slot became available before its deadline."""


class SlotCapacityConflictError(ProcessError):
    """A caller requested a capacity different from the slot root's fixed capacity."""


class ProviderError(FanoutError):
    """Base class for provider adapter and executor-contract failures."""


class UnsupportedExecutorError(ProviderError):
    """The requested identifier is not admitted to the executor registry."""


class ProviderProtocolError(ProviderError):
    """A provider's structured output is malformed or incomplete."""


class ProviderRequestError(ProviderError):
    """A caller supplied an invalid provider request."""


class SkillAdmissionError(FanoutError):
    """A requested skill tree or its delivery evidence is unsafe or inconsistent."""


class RunStateError(FanoutError):
    """A durable run journal, snapshot, or state transition is unsafe."""


class RunLockError(RunStateError):
    """Another controller already owns a run's exclusive controller lock."""


class RunAuthorizationError(RunStateError):
    """An owner-only run-state operation lacked the owner capability."""


class SchedulerError(FanoutError):
    """Base class for deterministic scheduler contract failures."""


class SchedulerConflictError(SchedulerError):
    """Durable scheduler state changed concurrently or has a stale revision."""


class SchedulerStateError(SchedulerError):
    """A scheduler transition, persisted state, or dependency result is invalid."""


class MemoryError(FanoutError):
    """Base class for explicit checkpoint exchange failures."""


class MemoryValidationError(MemoryError):
    """Checkpoint data or controller identity is malformed or unsafe."""


class MemoryTransportError(MemoryError):
    """The exact memory controller did not complete one bounded request."""


class MemoryProtocolError(MemoryError):
    """The controller response did not match its pinned canonical schema."""


class MemoryIntegrityError(MemoryError):
    """An exact observation did not authenticate the expected checkpoint."""


class MemoryEquivocationError(MemoryIntegrityError):
    """One checkpoint identity was bound to two different payload digests."""


class MemoryDurabilityError(MemoryError):
    """Checkpoint artifact or journal evidence could not be durably verified."""


class CollaborationError(FanoutError):
    """Base class for task-round barrier and peer-exchange failures."""


class CollaborationValidationError(CollaborationError):
    """A task packet, seat binding, policy, or peer packet is invalid."""


class CollaborationDurabilityError(CollaborationError):
    """Durable terminal, barrier, journal, or checkpoint evidence is unavailable."""


class RepositoryError(FanoutError):
    """Base class for immutable repository-baseline failures."""


class RepositoryValidationError(RepositoryError):
    """A repository, baseline, seat name, or workspace destination is invalid."""


class RepositoryIsolationError(RepositoryError):
    """A repository entry would escape the immutable, run-owned workspace."""


class RepositorySecretError(RepositoryError):
    """Baseline bytes appear to contain a credential and cannot be sent to a seat."""


class RepositoryQuotaError(RepositoryError):
    """A baseline would exceed its per-file or aggregate byte limits."""


class CandidateError(FanoutError):
    """Base class for immutable candidate-bundle and verifier failures."""


class CandidateValidationError(CandidateError):
    """A candidate manifest, delta, or verifier input is malformed or unsafe."""


class CandidateVerificationError(CandidateError):
    """A candidate cannot be materialized or checked against its immutable baseline."""


class LifecycleError(FanoutError):
    """Base class for candidate collection, handover, and cleanup failures."""


class HandoverError(LifecycleError):
    """A transactional caller-worktree handover was refused or rolled back."""


class GarbageCollectionError(LifecycleError):
    """An exact run-owned path cannot be proven safe to delete."""


class ExecutionError(FanoutError):
    """Base class for owner-controlled fanout execution failures."""


class ExecutionValidationError(ExecutionError):
    """Execution dependencies or durable state do not match the compiled run."""


class ExecutionPreflightError(ExecutionError):
    """An immutable provider, skill, memory, repository, owner, or budget check failed."""


class ExecutionConflictError(ExecutionError):
    """Durable execution state advanced concurrently or rolled back."""


class ExecutionPendingError(ExecutionError):
    """Owner reconciliation evidence is absent, invalid, or not yet verified."""
