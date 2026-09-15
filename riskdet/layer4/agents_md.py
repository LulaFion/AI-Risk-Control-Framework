"""Parse the Claude Code subagent definitions in `.claude/agents/*.md`.

These `.md` files are the SINGLE SOURCE OF TRUTH for each Layer-4 agent's
contract (the same files a human runs interactively). The Cloud Run driver
reuses them verbatim rather than re-expressing the prompts in Python, so the
container and the interactive workflow can never drift.

Each file is:

    ---
    name: player-investigator
    description: ...
    tools: Bash, PowerShell, Read, Grep, Glob, Write
    ---
    <system-prompt body>

We extract the frontmatter (name/description/tools) and the body. Tool names
are mapped to the Claude Agent SDK's tool vocabulary; `PowerShell` has no SDK
tool (the container is Linux) and is dropped -- `Bash` covers shell needs.
"""
from __future__ import annotations

import dataclasses
import pathlib

# Frontmatter tool label -> Claude Agent SDK tool name. PowerShell -> Bash
# (Linux container has no PowerShell; the shell need is identical). Unknown
# labels are dropped rather than guessed.
_TOOL_MAP = {
    "Bash": "Bash", "PowerShell": "Bash",
    "Read": "Read", "Write": "Write", "Edit": "Edit",
    "Grep": "Grep", "Glob": "Glob",
    "Task": "Task", "WebFetch": "WebFetch", "WebSearch": "WebSearch",
}


@dataclasses.dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    tools: tuple[str, ...]   # SDK tool names, de-duplicated, order-preserved
    body: str                # the system-prompt text (frontmatter stripped)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter dict, body). Tolerant of CRLF and missing fences."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm: dict[str, str] = {}
    body_start = len(lines)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        if ":" in lines[i]:
            k, _, v = lines[i].partition(":")
            fm[k.strip()] = v.strip()
    body = "\n".join(lines[body_start:]).strip()
    return fm, body


def _map_tools(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for part in raw.split(","):
        label = part.strip()
        mapped = _TOOL_MAP.get(label)
        if mapped and mapped not in out:
            out.append(mapped)
    return tuple(out)


def parse_agent(path: pathlib.Path) -> AgentSpec:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    name = fm.get("name") or path.stem
    return AgentSpec(
        name=name,
        description=fm.get("description", ""),
        tools=_map_tools(fm.get("tools", "")),
        body=body,
    )


def load_agents(agents_dir: pathlib.Path) -> dict[str, AgentSpec]:
    """Load every `*.md` under `agents_dir`, keyed by agent name."""
    agents_dir = pathlib.Path(agents_dir)
    if not agents_dir.is_dir():
        raise FileNotFoundError(
            f"agent definitions not found at {agents_dir} -- the Layer-4 driver "
            f"needs the .claude/agents/*.md files copied into the image")
    specs: dict[str, AgentSpec] = {}
    for md in sorted(agents_dir.glob("*.md")):
        spec = parse_agent(md)
        specs[spec.name] = spec
    if not specs:
        raise FileNotFoundError(f"no *.md agent definitions in {agents_dir}")
    return specs
