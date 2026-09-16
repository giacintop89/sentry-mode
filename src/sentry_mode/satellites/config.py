"""Settings for the satellite side of the node, off until there is something to talk to."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SatellitesConfig(BaseModel):
    """Whether this node listens to other nodes at all, and what it does when one misbehaves.

    Everything the broker and the media path need arrives with the increments that use
    them. What matters now is that the section exists, defaults to off, and that a node
    started without the optional dependencies behaves exactly as it did before.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    journal_file: Path = Path(".local/satellites.sqlite3")
    fault_policy: Literal["isolated", "global"] = "isolated"
