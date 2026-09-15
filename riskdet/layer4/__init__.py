"""Layer 4 -- the agent team, driven against Vertex AI for Cloud Run.

Importing this package never imports the heavy `claude_agent_sdk`; that happens
lazily inside `run_layer4` so the deterministic image (and any environment
without the SDK) can still `import riskdet` and run the `dry_run` gate.
"""
from __future__ import annotations

from .runner import gate, run, run_layer4

__all__ = ["run", "run_layer4", "gate"]
