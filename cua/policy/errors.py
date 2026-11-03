"""Policy errors: raised before the surface is touched, carrying the decision that stopped it."""

from cua.policy.models import PolicyDecision


class PolicyError(Exception):
    def __init__(self, decision: PolicyDecision, summary: str):
        self.decision = decision
        self.summary = summary
        super().__init__(self._message())

    def _message(self) -> str:
        raise NotImplementedError


class PolicyBlocked(PolicyError):
    def _message(self) -> str:
        return (f"expected: {self.summary} to be permitted; observed: blocked by "
                f"{self.decision.rule_id} ({self.decision.reason})")


class ApprovalDenied(PolicyError):
    def _message(self) -> str:
        return (f"expected: approval for {self.decision.risk} action {self.summary}; observed: "
                f"approval denied (rule {self.decision.rule_id}: {self.decision.reason})")


class SecretMissing(Exception):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"expected: environment variable {name} for a secret template; "
                         "observed: not set")
