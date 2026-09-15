"""Layer 4 on Cloud Run -- drive the agent chain against Vertex AI.

WHAT THIS IS
    `riskdet run --as-of ...` (the deterministic Layers 1-3) writes
    out/candidates.jsonl + output/case_queue.md but does NOT investigate. This
    module is the LLM half: it gates the candidates through the case-state store,
    then runs the SAME `.claude/agents/*.md` subagents that a human runs
    interactively -- player-investigator -> skeptic per case, then
    report-composer once -- using the Claude Agent SDK on Vertex AI.

    Two Cloud Run jobs form the pipeline:
        riskdet-scan  (deterministic, no LLM)  ->  candidates.jsonl
        riskdet-l4    (this, LLM on Vertex)    ->  output/cases/*.md + digest

DECISION BOUNDARY (unchanged)
    The agents report evidence and uncertainty; they never enforce. Routing into
    this queue is deterministic (casestore, human_review tier), never an agent
    decision. Final decisions remain 100% human.

AUTH
    Vertex via ambient ADC (the runtime SA's metadata-server credentials) -- no
    API key. Env: CLAUDE_CODE_USE_VERTEX=1, ANTHROPIC_VERTEX_PROJECT_ID,
    CLOUD_ML_REGION. IAM: roles/aiplatform.user on the runtime SA.

LEAST PRIVILEGE
    The agents' contracts allow Bash, but an unattended job must not hand the
    model an open shell. Read/Write/Grep/Glob are pre-approved; every Bash and
    Task call is routed through `_ToolPolicy`, which default-DENIES and permits
    Bash only for `python -m riskdet ...` / riskdet-driven python and a small
    read-only utility set. Every decision is written to
    out/logs/l4_tool_audit.jsonl. The hard boundary underneath this is the
    runtime SA's own scoped IAM (read-only BigQuery + Cloud Logging, write only
    to its GCS prefix) plus container isolation.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
from pathlib import Path

from ..casestore import CaseStore
from ..config import Settings

log = logging.getLogger("riskdet.layer4")

# --- Bash command policy --------------------------------------------------- #
# Default-ALLOW for analysis/read-only; deny only genuinely destructive or
# network/exfiltration commands when they appear at COMMAND position (start of
# the line or a pipeline segment). Anchoring at command position avoids
# false-matching harmless substrings inside code -- e.g. the module `riskdet.bq`
# or `CostGuardedBQ` must NOT trip a `bq` rule (that bug made the agent thrash
# and time out). The real security boundary is the scoped runtime SA + container
# isolation + this audit trail; this policy just blocks obvious foot-guns.
_DENY_CMD = re.compile(
    r"(?:^|[\s;|&`(])(?:rm|rmdir|dd|mkfs|shred|chmod|chown|chgrp|"
    r"curl|wget|nc|ncat|netcat|ssh|scp|sftp|telnet|ftp|"
    r"pip|pip3|apt|apt-get|yum|dnf|apk|conda|npm|npx|"
    r"sudo|reboot|shutdown|mount|umount|crontab|systemctl|docker|gsutil)\b")


def _bash_allowed(command: str) -> tuple[bool, str]:
    cmd = (command or "").strip()
    if not cmd:
        return False, "empty command"
    if _DENY_CMD.search(cmd):
        return False, "destructive/network command not permitted"
    return True, "allowed (analysis/read-only; destructive+network denied)"


class _ToolPolicy:
    """A `can_use_tool` callback that enforces least privilege and audits."""

    def __init__(self, sdk, audit_path: Path, known_agents: set[str]):
        self._Allow = sdk.PermissionResultAllow
        self._Deny = sdk.PermissionResultDeny
        self._audit = audit_path
        self._known_agents = known_agents
        self._audit.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, tool: str, decision: str, reason: str, extra: dict):
        rec = {"tool": tool, "decision": decision, "reason": reason, **extra}
        with self._audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    async def __call__(self, tool_name, tool_input, context):
        if tool_name == "Bash":
            ok, reason = _bash_allowed(tool_input.get("command", ""))
            self._write("Bash", "allow" if ok else "deny", reason,
                        {"command": tool_input.get("command", "")[:400]})
            return self._Allow() if ok else self._Deny(message=reason)
        if tool_name == "Task":
            sub = tool_input.get("subagent_type") or tool_input.get("agent") or ""
            ok = (not sub) or (sub in self._known_agents)
            reason = "known subagent" if ok else f"unknown subagent {sub!r}"
            self._write("Task", "allow" if ok else "deny", reason,
                        {"subagent": sub})
            return self._Allow() if ok else self._Deny(message=reason)
        # Read/Write/Grep/Glob are pre-approved via allowed_tools and never reach
        # here; anything else that does is denied by default.
        self._write(tool_name, "deny", "tool not permitted for Layer 4", {})
        return self._Deny(message=f"{tool_name} is not permitted in this job")


# --- Vertex configuration -------------------------------------------------- #
def _vertex_env(cfg: Settings) -> dict[str, str]:
    project = (os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
               or os.environ.get("RISKDET_BQ_PROJECT") or cfg.bq_project)
    # us-central1 carries the full current Claude lineup on Vertex (verified) and
    # colocates with the Cloud Run job; us-east5 only has older models.
    region = (os.environ.get("CLOUD_ML_REGION")
              or os.environ.get("RISKDET_L4_REGION") or "us-central1")
    # Claude Code uses TWO models on Vertex: the primary (ANTHROPIC_MODEL, set via
    # options.model) AND a small/fast background model. If the background model is
    # not set + enabled, background steps 404. Default to Haiku 4.5.
    small = (os.environ.get("RISKDET_L4_SMALL_MODEL")
             or "claude-haiku-4-5@20251001")
    env = {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": project,
        "CLOUD_ML_REGION": region,
        "ANTHROPIC_SMALL_FAST_MODEL": small,     # legacy name, still honored
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": small,  # current alias-mapping name
        # sub-shells the agents launch reuse the same interpreter/root
        "RISKDET_ROOT": str(cfg.root),
        "PYTHONPATH": str(cfg.root),
    }
    # Pass through any per-model region pins / alias overrides the operator set
    # (e.g. VERTEX_REGION_CLAUDE_5_SONNET, ANTHROPIC_DEFAULT_OPUS_MODEL).
    for k, v in os.environ.items():
        if k.startswith("VERTEX_REGION_CLAUDE_") or k.startswith("ANTHROPIC_DEFAULT_"):
            env[k] = v
    return env


def _anthropic_env(cfg: Settings) -> dict[str, str]:
    """API-key backend: the public Anthropic API instead of Vertex. Reads
    ANTHROPIC_API_KEY from the environment (on Cloud Run inject it from Secret
    Manager, never plaintext). No Model Garden, no aiplatform.user."""
    small = (os.environ.get("RISKDET_L4_SMALL_MODEL")
             or "claude-haiku-4-5-20251001")
    env = {
        "CLAUDE_CODE_USE_VERTEX": "",             # force OFF (falsy) -> direct API
        "ANTHROPIC_SMALL_FAST_MODEL": small,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": small,
        "RISKDET_ROOT": str(cfg.root),
        "PYTHONPATH": str(cfg.root),
    }
    if os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    return env


def _resolve_paths_env(cfg: Settings) -> dict[str, str]:
    env = {}
    for k in ("RISKDET_OUT_DIR", "RISKDET_ARTIFACT_DIR", "RISKDET_LOG_PROJECT",
              "RISKDET_KEY_FILE", "RISKDET_LOG_KEY_FILE", "GOOGLE_CLOUD_PROJECT"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# --- the gate: candidates.jsonl -> case-state store ------------------------ #
def _read_candidates(cfg: Settings) -> list[dict]:
    p = cfg.paths.candidates_jsonl
    if not p.is_file():
        raise FileNotFoundError(
            f"{p} not found -- run `riskdet run --as-of ...` before layer4")
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _case_state_path(cfg: Settings) -> Path:
    return cfg.paths.out / "case_state.json"


def _read_text(p: Path) -> str:
    try:
        return Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


def _window(cfg: Settings, fallback: _dt.datetime) -> tuple[_dt.date, _dt.date]:
    """The scan window (min start, max end) from candidates.jsonl -- the dates
    report-composer uses to name the digest."""
    starts, ends = [], []
    for c in _read_candidates(cfg):
        if c.get("window_start"):
            starts.append(_dt.datetime.fromisoformat(c["window_start"]).date())
        if c.get("window_end"):
            ends.append(_dt.datetime.fromisoformat(c["window_end"]).date())
    return (min(starts) if starts else fallback.date(),
            max(ends) if ends else fallback.date())


def _load_dispositions(cfg: Settings) -> dict:
    """Human decisions the dashboard recorded, if any -- so an acknowledged
    case is not re-investigated. Optional; empty if the file is absent."""
    p = cfg.paths.out / "dispositions.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("dispositions.json unreadable -- ignoring")
    return {}


def gate(cfg: Settings, scan_id: str) -> CaseStore:
    """Ingest the latest scan's candidates through the persistent case-state
    store (idempotent per scan_id) and return the store. This is the step
    production `scan()` does not yet do; running it here keeps the L4 job
    self-contained regardless of scan wiring."""
    store = CaseStore(
        _case_state_path(cfg),
        min_persistence=int(os.environ.get("RISKDET_L4_MIN_PERSISTENCE", "1")),
        close_after_absent=int(os.environ.get("RISKDET_L4_CLOSE_ABSENT", "1")),
    )
    cands = _read_candidates(cfg)
    alerts = store.ingest(scan_id, cands, dispositions=_load_dispositions(cfg))
    store.save()
    log.info("gate: %d candidates ingested, %d alerts, %d in L4 queue",
             len(cands), len(alerts), len(store.layer4_queue()))
    return store


def _env_preamble(cfg: Settings) -> str:
    """Tell the agent EXACTLY where it is and where the files are. Without this
    the agents assume their local Windows layout (C:/... python, relative paths
    under the repo) and waste the whole run hunting the filesystem. Paths are the
    resolved absolutes, so this is correct on Cloud Run (/data mount) and locally."""
    p = cfg.paths
    return (
        "## RUNTIME ENVIRONMENT — READ THIS FIRST\n"
        "- Linux container. The Python interpreter is `python3` (there is NO "
        "`C:/...` path — ignore any Windows path your contract mentions).\n"
        "- The `riskdet` package is importable; run analysis as `python3 -m riskdet ...`\n"
        "  and pull logs with `python3 -m riskdet pull-logs --case-id ... --evidence ... --pad-min 2`\n"
        "  (±2 minutes around each round's game_time).\n"
        "- Use these EXACT absolute paths. Do NOT search the filesystem (`find /` is banned):\n"
        f"    candidates.jsonl : {p.candidates_jsonl}\n"
        f"    case_queue.md    : {p.case_queue}\n"
        f"    findings csv     : {p.findings_csv}\n"
        f"    write case file  : {p.cases_dir} / <case_id>.md\n"
        f"    logs dir         : {p.logs_dir}\n"
        "- Prefer the Read / Grep / Glob tools for reading & searching files (they are "
        "pre-approved and fast). Use Bash only for `python3 -m riskdet ...` analysis.\n"
        "- Cloud Logging has 30-day retention. If an evidence round's timestamp is "
        "older than 30 days it has AGED OUT — do NOT attempt `pull-logs` for it; "
        "note it as un-investigable in logs and move on. Only pull logs for rounds "
        "within the last 30 days.\n"
        "- BE DECISIVE AND EFFICIENT: aim to conclude your stage in ~10-12 turns. "
        "Read the candidate record, pull only in-retention top rounds, reach your "
        "conclusion. Do NOT re-read a file you already read or re-run an equivalent "
        "command — you have a hard turn cap and will be cut off if you loop.\n\n")


# --- one SDK stage --------------------------------------------------------- #
async def _run_stage(sdk, cfg, *, body: str, tools: tuple[str, ...],
                     prompt: str, policy, sub_agents: dict, env: dict,
                     model: str | None, max_turns: int,
                     max_budget_usd: float | None, label: str) -> dict:
    # Read/Write/Grep/Glob are pre-approved; Bash/Task fall through to `policy`.
    preapproved = [t for t in tools if t in ("Read", "Write", "Grep", "Glob")]
    opts = sdk.ClaudeAgentOptions(
        system_prompt=body,
        allowed_tools=preapproved,
        can_use_tool=policy,
        permission_mode="default",
        agents=sub_agents or None,
        cwd=str(cfg.root),
        env=env,
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        setting_sources=None,   # explicit config only; no hidden project settings
    )
    result_text, cost, turns, is_error, errors = "", None, None, False, None
    async for msg in sdk.query(prompt=prompt, options=opts):
        if isinstance(msg, sdk.ResultMessage):
            result_text = msg.result or ""
            cost = msg.total_cost_usd
            turns = msg.num_turns
            is_error = msg.is_error
            errors = msg.errors
    log.info("stage %s: turns=%s cost=$%s error=%s",
             label, turns, f"{cost:.4f}" if cost else cost, is_error)
    return {"label": label, "cost_usd": cost, "turns": turns,
            "is_error": is_error, "errors": errors,
            "result_tail": result_text[-500:]}


# --- orchestration --------------------------------------------------------- #
async def run_layer4(cfg: Settings, as_of: _dt.datetime, *,
                     max_cases: int = 0, dry_run: bool = False) -> dict:
    cfg.paths.ensure()
    scan_id = as_of.isoformat()
    store = gate(cfg, scan_id)
    queue = store.layer4_queue()
    if max_cases > 0:
        queue = queue[:max_cases]

    summary = {"scan_id": scan_id, "queued": len(store.layer4_queue()),
               "processed": 0, "cases": [], "total_cost_usd": 0.0}

    if dry_run:
        summary["cases"] = [s.case_id for s in queue]
        summary["note"] = "dry-run: gate only, no LLM calls"
        log.info("dry-run: %d cases would be investigated: %s",
                 len(queue), summary["cases"])
        return summary
    from .agents_md import load_agents  # noqa: PLC0415
    specs = load_agents(cfg.root / ".claude" / "agents")
    for req in ("player-investigator", "skeptic", "report-composer"):
        if req not in specs:
            raise KeyError(f"required agent {req!r} missing from .claude/agents")

    # The Claude Agent SDK is needed only to INVESTIGATE cases or to run the
    # Claude composer. A quiet day (0 human_review) with the Gemini composer
    # needs no SDK -- but a digest is STILL produced (a "quiet day" report).
    composer_backend = os.environ.get("RISKDET_L4_COMPOSER", "claude").lower()
    need_sdk = bool(queue) or composer_backend != "gemini"
    sdk = env = model = preamble = policy = None
    sub_agents = {}
    max_turns, max_budget = 20, None
    if need_sdk:
        import claude_agent_sdk as sdk  # noqa: PLC0415 -- optional heavy dep
        backend = os.environ.get("RISKDET_L4_BACKEND", "vertex").lower()
        if backend == "anthropic":
            env = {**_anthropic_env(cfg), **_resolve_paths_env(cfg)}
            model = os.environ.get("RISKDET_L4_MODEL") or "claude-sonnet-5"
        else:
            env = {**_vertex_env(cfg), **_resolve_paths_env(cfg)}
            model = os.environ.get("RISKDET_L4_MODEL") or None
        log.info("layer4 backend=%s model=%s", backend, model)
        preamble = _env_preamble(cfg)
        max_turns = int(os.environ.get("RISKDET_L4_MAX_TURNS", "20"))
        budget = os.environ.get("RISKDET_L4_BUDGET_USD", "1.0")
        max_budget = float(budget) if budget else None
        policy = _ToolPolicy(sdk, cfg.paths.logs_dir / "l4_tool_audit.jsonl",
                             known_agents=set(specs))
        if "query-analyst" in specs:
            qa = specs["query-analyst"]
            sub_agents["query-analyst"] = sdk.AgentDefinition(
                description=qa.description, prompt=qa.body, tools=list(qa.tools))

    if not queue:
        log.info("0 human_review this cycle -- composing a quiet-day digest only")

    inv, sk = specs["player-investigator"], specs["skeptic"]
    for st in queue:
        cid = st.case_id
        case_path = cfg.paths.case_file(cid)
        log.info("investigating %s", cid)
        inv_prompt = (
            f"Investigate case `{cid}` from the Layer-4 queue. Its full machine "
            f"record is one line in `{cfg.paths.candidates_jsonl}` (Grep for the "
            f"case_id); the roster is `{cfg.paths.case_queue}`. Follow your "
            f"contract and write your finding to `{case_path}`.")
        r_inv = await _run_stage(
            sdk, cfg, body=preamble + inv.body, tools=inv.tools + ("Task",),
            prompt=inv_prompt, policy=policy, sub_agents=sub_agents, env=env,
            model=model, max_turns=max_turns, max_budget_usd=max_budget,
            label=f"investigator:{cid}")

        sk_prompt = (
            f"Review the finding in `{case_path}`. Work through your attack "
            f"checklist and append your `Skeptic Review` section to that same "
            f"file, ending with a Verdict and Escalation gate line.")
        r_sk = await _run_stage(
            sdk, cfg, body=preamble + sk.body, tools=sk.tools + ("Task",),
            prompt=sk_prompt, policy=policy, sub_agents=sub_agents, env=env,
            model=model, max_turns=max_turns, max_budget_usd=max_budget,
            label=f"skeptic:{cid}")

        store.mark_investigated(cid, scan_id)
        store.save()
        for r in (r_inv, r_sk):
            summary["total_cost_usd"] += r.get("cost_usd") or 0.0
        summary["cases"].append(
            {"case_id": cid, "investigator": r_inv, "skeptic": r_sk})
        summary["processed"] += 1

    # Report-composer once, over everything investigated this cycle. The digest
    # is pure assembly (no tools), so it can run on Gemini via Vertex instead of
    # Claude -- cheaper and no Model Garden step. Investigator/skeptic stay Claude.
    composer_backend = os.environ.get("RISKDET_L4_COMPOSER", "claude").lower()
    if composer_backend == "gemini":
        r_comp = _compose_with_gemini(cfg, specs, _window(cfg, as_of))
    else:
        comp = specs["report-composer"]
        comp_prompt = (
            "Compose the daily executive digest. Read `output/case_queue.md` for "
            "the roster and window, every file under `output/cases/`, and any "
            "`Verdict:` lines under `output/data_quality/`. Write "
            "`output/reports/daily_digest_<start>_<end>.md` per your contract.")
        r_comp = await _run_stage(
            sdk, cfg, body=comp.body, tools=comp.tools, prompt=comp_prompt,
            policy=policy, sub_agents={}, env=env, model=model,
            max_turns=max_turns, max_budget_usd=max_budget,
            label="report-composer")
        summary["total_cost_usd"] += r_comp.get("cost_usd") or 0.0
    summary["report_composer"] = r_comp

    _write_run_log(cfg, summary)
    synced = _sync_gcs(cfg)
    if synced:
        summary["gcs_synced"] = synced
    return summary


def _write_run_log(cfg: Settings, summary: dict) -> None:
    p = cfg.paths.logs_dir / f"l4_run_{summary['scan_id'].replace(':', '')}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("layer4 run log -> %s (total $%.4f, %d cases)",
             p, summary["total_cost_usd"], summary["processed"])


def _sync_gcs(cfg: Settings) -> str | None:
    """Upload the agent-written artifacts + case state to GCS so the dashboard
    and humans can read them. No-op unless RISKDET_GCS_BUCKET is set."""
    bucket = os.environ.get("RISKDET_GCS_BUCKET")
    if not bucket:
        return None
    try:
        from google.cloud import storage  # noqa: PLC0415
    except ImportError:
        log.warning("RISKDET_GCS_BUCKET set but google-cloud-storage not "
                    "installed -- skipping upload")
        return None
    prefix = os.environ.get("RISKDET_GCS_PREFIX", "riskdet").strip("/")
    client = storage.Client()
    bkt = client.bucket(bucket)
    roots = [(cfg.paths.cases_dir, "output/cases"),
             (cfg.paths.reports_dir, "output/reports"),
             (cfg.paths.logs_dir, "out/logs")]
    n = 0
    for root, rel in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file():
                blob = f"{prefix}/{rel}/{f.relative_to(root).as_posix()}"
                bkt.blob(blob).upload_from_filename(str(f))
                n += 1
    state = _case_state_path(cfg)
    if state.is_file():
        bkt.blob(f"{prefix}/out/case_state.json").upload_from_filename(str(state))
        n += 1
    dest = f"gs://{bucket}/{prefix}"
    log.info("synced %d files -> %s", n, dest)
    return dest


def _compose_with_gemini(cfg: Settings, specs: dict,
                         window: tuple[_dt.date, _dt.date]) -> dict:
    """Run the report/digest stage on Gemini via Vertex instead of Claude.

    The composer's job is faithful assembly of already-investigated case files
    -- pure text generation, no tool loop -- so it fits a single Gemini call.
    We reuse the report-composer.md contract as the instructions and inline all
    inputs (the model has no file tools here). gemini-3.7-flash is served from
    the `global` location, not a region."""
    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    project = (os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
               or os.environ.get("RISKDET_BQ_PROJECT") or cfg.bq_project)
    location = os.environ.get("RISKDET_L4_GEMINI_LOCATION") or "global"
    model = os.environ.get("RISKDET_L4_GEMINI_MODEL") or "gemini-3.7-flash"
    client = genai.Client(vertexai=True, project=project, location=location)

    cases = "".join(
        f"\n\n===== FILE: {f.name} =====\n{_read_text(f)}"
        for f in sorted(cfg.paths.cases_dir.glob("*.md")))
    dq = "".join(
        f"\n\n===== DQ: {f.name} =====\n{_read_text(f)}"
        for f in sorted(cfg.paths.dq_dir.glob("*.md"))) if cfg.paths.dq_dir.is_dir() else ""

    prompt = (
        specs["report-composer"].body
        + "\n\n## OPERATING MODE\nYou are a single text-generation call, NOT an "
          "agent with tools. Every input is inlined below; do not claim to read "
          "files and do not invent facts absent from them. Output ONLY the final "
          "digest markdown, nothing else.\n"
        + f"\n---- output/case_queue.md ----\n{_read_text(cfg.paths.case_queue)}\n"
        + f"\n---- output/cases/*.md ----\n{cases or '(none)'}\n"
        + (f"\n---- output/data_quality/*.md ----\n{dq}\n" if dq else ""))

    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=8192,
                                           temperature=0.2))
    text = resp.text or ""
    # Name the digest by the single SCAN DATE (window end) so the dashboard's
    # per-day lookup (daily_digest_<date>.md) finds it. This is a daily report.
    out = cfg.paths.reports_dir / f"daily_digest_{window[1]}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    usage = getattr(resp, "usage_metadata", None)
    tokens = getattr(usage, "total_token_count", None) if usage else None
    log.info("gemini composer: %s (%s) -> %s (%d chars, %s tokens)",
             model, location, out, len(text), tokens)
    return {"label": "report-composer(gemini)", "model": model,
            "location": location, "digest": str(out), "chars": len(text),
            "tokens": tokens, "is_error": not text}


def run(cfg: Settings, as_of: _dt.datetime, *, max_cases: int = 0,
        dry_run: bool = False) -> dict:
    """Sync entry point for the CLI."""
    return asyncio.run(run_layer4(cfg, as_of, max_cases=max_cases,
                                  dry_run=dry_run))
