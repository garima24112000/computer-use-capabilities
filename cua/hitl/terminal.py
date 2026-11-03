"""TerminalApprover: asks the operator at the terminal before an action that needs approval."""

import sys
from collections.abc import Callable

from cua.policy.models import PolicyContext, PolicyDecision


class TerminalApprover:
    def __init__(self, ask: Callable[[str], str] = input, out=sys.stderr):
        self.ask = ask
        self.out = out

    def request_approval(self, decision: PolicyDecision, action_summary: str,
                         ctx: PolicyContext) -> bool:
        print(f"\n*** APPROVAL REQUIRED ({ctx.mode}, step {ctx.step_id}) ***", file=self.out)
        print(f"action : {action_summary}", file=self.out)  # already redacted by GuardedSurface
        print(f"risk   : {decision.risk} (rule {decision.rule_id}: {decision.reason})",
              file=self.out)
        try:
            answer = self.ask("approve? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")
