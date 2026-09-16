"""What an action knows about why it is running, fixed at the moment it was decided.

A queued action does not look anything up again later. The rule may be edited, the
dashboard may be pointed at another camera, and Sentry may have been disarmed and armed
again in the meantime. None of that changes which camera this photo is from or which
arming it belongs to. The worker compares `arm_epoch` with the current one and drops the
action if they differ.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionContext:
    trigger_id: str
    rule_id: str
    rule_name: str
    rule_revision: int
    arm_epoch: int
    zone: str | None = None
    origin: tuple[str, ...] = ()
    """What set it off: event identifiers for a satellite, `vision:<source>` for a camera."""
    sources: tuple[str, ...] = ()
    """Every source the actions will use, resolved when the trigger fired."""
    decided_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None

    @classmethod
    def new(cls, **fields) -> ActionContext:
        return cls(trigger_id=str(uuid.uuid4()), **fields)


__all__ = ["ActionContext"]
