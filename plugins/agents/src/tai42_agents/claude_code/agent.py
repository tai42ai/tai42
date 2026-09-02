"""``claude_code`` as an :class:`Agent`: drive the real ``claude`` binary through the Claude
Agent SDK INSIDE a sandbox session over the versioned JSONL exec protocol.

The plugin server NEVER imports the SDK — the SDK lives in the session image and only the
runner payload (shipped as DATA, executed in-session) imports it. This module is the ADAPTER:
it acquires a sandbox session (:func:`require_sandbox`, the one raising chokepoint), authors a
hermetic workspace every turn, drives the runner, and maps its up-frames to contract stream
events. Park/resume ride the shared ``_internal/park`` machinery; the SDK's model cost is
emitted into the active trace (its model calls bypass the platform LLM seam).

The ``crash_resume`` setting (§A6) is DECLARED to the skeleton at registration as
``meta={"tai42/crash_resume": <setting>}`` on the run tool, threaded through the generic
``agents.agent(name, tags=..., meta=...)`` passthrough; the skeleton's run-dispatch seam reads
that key to decide whether to re-invoke a recycled detached run. The meta is captured ONCE at
registration (the setting is recycle-class, so a hot change re-registers and re-declares it) and
is sourced from the lightweight ``claude_code_crash_resume`` read, which needs ONLY that one env
var — so importing this module never requires the full ``ClaudeCodeSettings`` creds/image, whose
validation fires at run start (the first ``astream``/``run``), before any sandbox session.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from tai42_contract.access_control.context import get_current_user_id
from tai42_contract.agent import Agent
from tai42_contract.agent.events import (
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    StreamEvent,
    StructuredFinal,
    SuspendedFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.app import tai42_app
from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.interactions import (
    NestedParkOwnershipError,
    SuspendedInteraction,
    assert_park_adoptable,
    get_park_completion,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.monitoring.models import SpanKind
from tai42_contract.sandbox import (
    SandboxExecTimeoutError,
    SandboxSession,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_agents._internal.nested_dispatch import nested_tool_dispatch
from tai42_agents._internal.park import (
    AGENT_RESUME_TOOL_NAME,
    ParkIdentity,
    assert_park_capable,
    bind_resume_per_step,
    persist_park,
    register_agent_resume_tool,
    workspace_lease,
)
from tai42_agents._internal.park.index import compute_superstep_id
from tai42_agents._internal.park.lease import LEASE_HEADROOM_SECONDS
from tai42_agents._internal.reject import (
    reject_blank_memory_keys,
    reject_unhonored,
    reject_untitled_response_format,
)
from tai42_agents._internal.sandbox_util import build_policied_spec, workspace_key_for
from tai42_agents.claude_code.options import build_options_payload, credential_env_names
from tai42_agents.claude_code.payload import runner_payload_files
from tai42_agents.claude_code.protocol import (
    CLAUDE_AGENT_SDK_VERSION,
    AnswerFrame,
    AskFrame,
    EventFrame,
    FatalFrame,
    HelloFrame,
    ProtocolError,
    ResultFrame,
    StartFrame,
    StopFrame,
    ToolCallFrame,
    ToolResultFrame,
    dump_frame,
    parse_up_frame,
)
from tai42_agents.claude_code.settings import (
    ClaudeCodeSettings,
    ConnectionCred,
    StaticCred,
    claude_code_crash_resume,
    claude_code_settings,
)
from tai42_agents.claude_code.skills_sync import sync_skills, validate_name

AGENT_NAME: Final[str] = "claude_code"

# Short exec ceiling for the volume-authoring / scrub commands (reset, payload write, cred
# scrub) — distinct from the turn-scoped ``run_timeout_seconds`` the drive itself runs under.
_SHORT_EXEC_TIMEOUT: Final[float] = 60.0

# Workspace-relative paths inside the session volume (rooted at ``session.workspace_path``).
_SESSION_ID_PATH = ".runner/session_id"
_CREDS_DIR = ".claude-home/.creds"
_CLAUDE_CONFIG_DIR = "project/.claude"
_RUNNER_PAYLOAD_DIR = ".runner/payload"
# Crash-after-terminal idempotence records (§A3.8), one per resumed super-step, keyed by the
# ``compute_superstep_id`` of the resume's interaction ids. A resume drive writes its record on
# the clean terminal BEFORE reporting; a redelivered resume reads it and returns the SAME output
# without re-driving the SDK session. There is no LangGraph snapshot here, so this durable record
# IS the resume idempotence source (a durable-volume analogue of a checkpoint-snapshot guard).
_TERMINAL_DIR = ".runner/terminal"

# The two ABC ``run``/``astream`` parameters ``claude_code`` cannot honor, mapped to the reason
# named in the raised error (its keys define the unhonored set).
_UNHONORED_REASONS: dict[str, str] = {
    "tools": "live tool closures cannot cross the sandbox process boundary; grant tool_names instead",
    "presets": "its tool set is composed from tool_names, not presets, and it will not silently ignore one",
    "strategy": "the SDK applies no composition strategy and will not silently ignore one",
    "interrupt_on": "permission policy is plugin settings (the SDK auto-approves in-sandbox), not per-run",
    "recursion_limit": "LangGraph semantics do not apply to the SDK loop; use max_turns",
    "llm_provider": "claude_code is Anthropic by construction; the key comes from plugin settings only",
    "llm_kwargs": "the model is configured through plugin settings; a caller key is never accepted",
    "checkpoint_provider": "the SDK owns session state; there is no LangGraph checkpoint here",
    "store_provider": "the SDK owns session state; there is no LangGraph store here",
    "resume_checkpoint_id": "the SDK owns session state; there is no checkpoint to fork",
    "system_content_kwargs": "the system prompt is passed to the SDK verbatim, never built as a content block",
}
_UNHONORED_COLLECTION_PARAMS: frozenset[str] = frozenset({"tools", "presets"})


@contextlib.contextmanager
def _resume_continuation(threaded: bool) -> Iterator[None]:
    """Bind the agent-resume continuation for the duration of a THREADED drive.

    A platform ``ask_user(mode="async")`` — the agent's OWN async ask (``_park_async_ask``)
    OR one a proxied tool this drive runs raises — reads the bound continuation to stamp
    ``continuation_tool`` onto the parked interaction, so a later ``agent_resume`` re-enters
    this agent. Without it, the async ask refuses loudly ("async ask requires a resuming
    driver") and no park is produced. The resume tool name is bound ONLY when the run is
    threaded; otherwise ``None`` is bound — NOT a no-op. An ephemeral uuid4 workspace reaps and
    can never resume, so binding ``None`` SHADOWS any ambient resume continuation a park-capable
    caller left bound, so a non-threaded run nested under one cannot inherit it and mint a park
    it can never resume: its async ask refuses pre-persist. Mirrors the LangGraph driver's
    ``park_continuation``."""
    name = AGENT_RESUME_TOOL_NAME if threaded else None
    token = set_resume_continuation_tool(name)
    try:
        yield
    finally:
        reset_resume_continuation_tool(token)


class InlineSkillShape(BaseModel):
    """A skill authored inline on the tool face: a charset-valid name + a ``SKILL.md`` body."""

    model_config = ConfigDict(extra="forbid")
    name: str
    content: str = ""


class SubagentSpecShape(BaseModel):
    """A subagent the caller declares on the tool face — mapped to the SDK AgentDefinition."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    system_prompt: str = ""
    tool_names: list[str] = Field(default_factory=list)


class ClaudeCodeInput(BaseModel):
    """JSON tool-face parameters for ``claude_code``.

    SECURITY INVARIANT: ``thread_id`` is NOT a field — workspace identity must never be
    derivable from unauthenticated caller input. ``thread_id`` arrives ONLY as a trusted
    in-process ``run``/``astream`` kwarg (the conversation bridge), so a tool-face call always
    gets a fresh ephemeral workspace. ``extra="forbid"`` rejects any unknown key loudly.
    """

    model_config = ConfigDict(extra="forbid")

    user_message: str
    system_message: str = ""
    tool_names: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    inline_skills: list[InlineSkillShape] = Field(default_factory=list)
    response_format: dict[str, Any] | None = None
    max_turns: int | None = None
    subagents: list[SubagentSpecShape] = Field(default_factory=list)


# A parking agent binds the hidden ``agent_resume`` continuation from its OWN registration
# (§C4): a claude-only box must still bind it, or every async park strands. Per-epoch
# idempotent, so a combined box binds it exactly once.
register_agent_resume_tool()

# Per-worker live-session cache for DETERMINISTIC (threaded) workspaces, so a reused session
# keeps its create-time ``spec.env`` (the refresh channel is the per-turn bearer file, not a
# re-baked env). Ephemeral ``uuid4`` workspaces are never cached. The cross-worker mutex is the
# Redis workspace lease; this cache only avoids re-creating within one worker.
_LIVE_SESSIONS: dict[str, SandboxSession] = {}


class _BearerMaterial(BaseModel):
    """A refreshable connection cred to re-materialize as a credential-helper file each turn."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    env_name: str
    token: SecretStr | None
    headers: dict[str, SecretStr]


class _TerminalRecord(BaseModel):
    """The durable crash-after-terminal idempotence record (§A3.8) for one resumed super-step.

    Captures the exact terminal OUTPUT (a message ``text`` or a structured ``data``) plus the
    session id and usage; ``extra="forbid"`` so an in-session-forged record with stray keys fails
    validation and is treated as absent (the resume re-drives rather than honoring garbage)."""

    model_config = ConfigDict(extra="forbid")
    superstep_id: str
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    structured: bool
    text: str | None = None
    data: Any = None


class ClaudeCodeError(RuntimeError):
    """A loud, constant-message ``claude_code`` failure surfaced to the caller."""


@tai42_app.agents.agent(
    AGENT_NAME,
    tags={"agents", "coding", "claude"},
    meta={"tai42/crash_resume": claude_code_crash_resume()},
)
class ClaudeCodeAgent(Agent):
    tool_name: ClassVar[str] = AGENT_NAME
    tool_description: ClassVar[str] = (
        "Run Claude Code as a platform agent: it drives the real claude binary inside a "
        "sandbox over a versioned exec protocol. Grant tools by name (run under the caller's "
        "identity), skills, and subagents. With response_format set, returns a validated "
        "structured object and fails loudly if the agent produces none."
    )
    ToolInput: ClassVar[type[BaseModel]] = ClaudeCodeInput

    async def run(self, **kwargs: Any) -> Any:
        """Drive one turn and drain the stream to a value (the contract terminal rule)."""
        return await self._drain(self.astream(**kwargs), response_format=kwargs.get("response_format"))

    async def astream(
        self,
        *,
        user_message: str = "",
        system_message: str = "",
        tool_names: Sequence[str] = (),
        skills: Sequence[str] = (),
        inline_skills: Sequence[dict[str, Any] | InlineSkillShape] = (),
        response_format: dict[str, Any] | None = None,
        max_turns: int | None = None,
        subagents: Sequence[dict[str, Any] | SubagentSpecShape] = (),
        thread_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Drive one turn inside a sandbox session, yielding contract stream events.

        Resolves the sandbox (the one raising chokepoint), derives the workspace (deterministic
        for a threaded run, a fresh ``uuid4`` otherwise), acquires the session and — for a
        threaded run only — the cross-worker workspace lease, authors the hermetic workspace,
        and drives the runner. A sync ask is answered adapter-side; an async ask parks (or is
        refused loudly on an ephemeral run). The credential scrub runs on a TERMINAL exit only.
        """
        settings = claude_code_settings()
        reject_unhonored(
            f"{AGENT_NAME}.astream",
            kwargs,
            _UNHONORED_REASONS,
            collection_params=_UNHONORED_COLLECTION_PARAMS,
        )
        reject_blank_memory_keys(f"{AGENT_NAME}.astream", thread_id=thread_id, resume_checkpoint_id=None)
        reject_untitled_response_format(AGENT_NAME, response_format)

        skill_names = [validate_name("skill", name) for name in skills]
        inline = [s if isinstance(s, InlineSkillShape) else InlineSkillShape.model_validate(s) for s in inline_skills]
        for spec in inline:
            validate_name("inline skill", spec.name)
        subs = [s if isinstance(s, SubagentSpecShape) else SubagentSpecShape.model_validate(s) for s in subagents]
        for spec in subs:
            validate_name("subagent", spec.name)

        # FAIL-CLOSED: a non-empty tool_names with no bound execution identity is refused at the
        # door — a code-execution agent must not run tools the platform cannot entitlement-check.
        tools_list = list(tool_names)
        if tools_list and get_current_user_id() is None:
            raise ClaudeCodeError(
                "claude_code refuses tool_names on a door with no bound execution identity: a "
                "proxied tool call could not be entitlement-checked"
            )

        options_snapshot = {
            "user_message": user_message,
            "system_message": system_message,
            "tool_names": tools_list,
            "skills": skill_names,
            "inline_skills": [s.model_dump(mode="json") for s in inline],
            "response_format": response_format,
            "max_turns": max_turns,
            "subagents": [s.model_dump(mode="json") for s in subs],
        }

        async for event in self._drive_workspace(
            settings=settings,
            thread_id=thread_id,
            prompt={"text": user_message},
            options_snapshot=options_snapshot,
        ):
            yield event

    async def aresume_park(
        self,
        *,
        rebuild_kwargs: dict[str, Any],
        thread_id: str,
        resume_map: dict[str, dict[str, Any]],
    ) -> Any:
        """Re-drive a parked turn from its stored snapshot, feeding the human answers back.

        ``resume_map`` arrives NESTED ``{interrupt_id: {interaction_id: answer}}``; this agent
        set ``interrupt_id = interaction_id``, so it flattens to one ``{interaction_id: answer}``
        map. Resume is a turn like any other: it re-acquires the same workspace, re-authors
        ``.claude``/``.runner``, reads the persisted SDK session id, and drives to terminal
        under the SAME materialize+scrub path — the kit driver fires the stored completion tool.

        CRASH-AFTER-TERMINAL IDEMPOTENCE (§A3.8): the drive is keyed by the super-step's
        ``compute_superstep_id`` (over the SAME interaction ids the park persisted, so it is
        identically derivable here). A resume that reaches a clean terminal writes a durable
        ``.runner/terminal/<superstep_id>.json`` record BEFORE reporting; a redelivered resume
        (the winner crashed between the terminal and the index finalize) reads that record and
        re-produces the SAME output WITHOUT re-driving the SDK session — never a second model turn.
        """
        settings = claude_code_settings()
        snapshot = dict(rebuild_kwargs)
        snapshot.pop("thread_id", None)
        flat: dict[str, Any] = {}
        for answers in resume_map.values():
            flat.update(answers)
        # The super-step id over the resumed interaction ids — identical to the id the park
        # persisted (``persist_park`` computed it over the same interaction-id set), so a
        # redelivery keys the terminal record to the same name the terminal drive wrote.
        superstep_id = compute_superstep_id(flat.keys())
        options_snapshot = snapshot["options_snapshot"]
        events = [
            event
            async for event in self._drive_workspace(
                settings=settings,
                thread_id=thread_id,
                prompt={"text": "", "resume_answers": flat},
                options_snapshot=options_snapshot,
                terminal_key=superstep_id,
            )
        ]
        return await self._drain(_aiter(events), response_format=options_snapshot.get("response_format"))

    # --- drive orchestration ---------------------------------------------------------------

    async def _drive_workspace(
        self,
        *,
        settings: ClaudeCodeSettings,
        thread_id: str | None,
        prompt: dict[str, Any],
        options_snapshot: dict[str, Any],
        terminal_key: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        threaded = thread_id is not None
        workspace_key = workspace_key_for(AGENT_NAME, thread_id) if threaded else str(uuid4())

        spec_env, static_env_names, bearer = await self._resolve_creds(settings)
        model_env_name, _ = settings.model_credential()

        # The per-workspace Redis lease serializes threaded turns across workers (the volume is
        # not idempotent under concurrent drives). An ephemeral uuid4 workspace no other worker
        # can name takes NO lease and never touches Redis.
        async with contextlib.AsyncExitStack() as stack:
            if threaded:
                lease_ms = (settings.run_timeout_seconds + LEASE_HEADROOM_SECONDS) * 1000
                await stack.enter_async_context(workspace_lease(workspace_key, lease_ms=lease_ms))
            async for event in self._drive_session(
                settings=settings,
                thread_id=thread_id,
                workspace_key=workspace_key,
                spec_env=spec_env,
                static_env_names=static_env_names,
                model_env_name=model_env_name,
                bearer=bearer,
                prompt=prompt,
                options_snapshot=options_snapshot,
                terminal_key=terminal_key,
            ):
                yield event

    async def _drive_session(
        self,
        *,
        settings: ClaudeCodeSettings,
        thread_id: str | None,
        workspace_key: str,
        spec_env: dict[str, SecretStr],
        static_env_names: list[str],
        model_env_name: str,
        bearer: list[_BearerMaterial],
        prompt: dict[str, Any],
        options_snapshot: dict[str, Any],
        terminal_key: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        threaded = thread_id is not None
        durability = cast("Any", "persistent" if threaded else "ephemeral")
        sandbox = tai42_app.sandboxes.require_sandbox()
        spec, policy = build_policied_spec(
            image=settings.session_image,
            workspace_key=workspace_key,
            durability=durability,
            env=spec_env,
            ttl_seconds=settings.session_ttl_seconds,
            labels={"tai42.agent": AGENT_NAME, "tai42.thread": thread_id or ""},
            network_setting=settings.network,
        )
        cached = _LIVE_SESSIONS.get(workspace_key) if threaded else None
        if cached is not None:
            session = cached
            await session.touch()
        else:
            session = await sandbox.create_session(spec)
            if threaded:
                _LIVE_SESSIONS[workspace_key] = session

        ws = session.workspace_path
        park_suspended = False
        handle = None
        try:
            # §A3.8 crash-after-terminal idempotence: a redelivered resume whose winner already
            # drove this super-step to a clean terminal reattaches the SAME durable volume and
            # finds its record — re-produce the stored output and DO NOT re-drive the SDK session.
            # The credential scrub/redact still run in the finally (idempotent on an already-scrubbed
            # volume), keeping the no-residual invariant on this exit too.
            if terminal_key is not None:
                record = await self._read_terminal_record(session, terminal_key)
                if record is not None:
                    yield _event_from_terminal_record(record)
                    return

            await self._materialize(session, ws=ws, settings=settings, bearer=bearer, options_snapshot=options_snapshot)

            resume_id = await self._read_session_id(session) if threaded else None
            payload = self._build_payload(
                settings=settings,
                ws=ws,
                options_snapshot=options_snapshot,
                static_env_names=static_env_names,
                model_env_name=model_env_name,
                resume_id=resume_id,
            )
            start = StartFrame(
                options=payload,
                prompt=prompt,
                tool_names=options_snapshot["tool_names"],
                skills=options_snapshot["skills"],
            )
            handle = await session.exec_start(
                ["python", "-m", "tai_runner"],
                env={
                    "PYTHONPATH": SecretStr(f"{ws}/{_RUNNER_PAYLOAD_DIR}"),
                    "HOME": SecretStr(f"{ws}/.claude-home"),
                    "CLAUDE_CONFIG_DIR": SecretStr(f"{ws}/.claude-home"),
                },
                cwd=".runner",
                timeout_seconds=settings.run_timeout_seconds,
            )
            await handle.write_stdin(dump_frame(start))

            # Bind the resume continuation for the drive so a threaded run's async ask — the
            # agent's OWN (_park_async_ask) or a proxied tool's — can actually park+resume
            # (mirror the LangGraph driver's park_continuation). Threaded-only: an ephemeral run
            # cannot resume, so its async ask refuses loudly rather than binding. Bound around each
            # drive step, NOT in this generator's body: a ``with`` wrapping the ``yield`` would
            # leak the binding into the consumer's task (PEP 568) and strand it on an abandoned
            # stream.
            async for event, is_park in bind_resume_per_step(
                lambda: _resume_continuation(threaded),
                self._drive_runner(
                    handle=handle,
                    session=session,
                    settings=settings,
                    thread_id=thread_id,
                    resume_id=resume_id,
                    tool_names=options_snapshot["tool_names"],
                    options_snapshot=options_snapshot,
                    terminal_key=terminal_key,
                ),
            ):
                if is_park:
                    park_suspended = True
                yield event
        finally:
            # (i) kill the exec and AWAIT the runner's death before any volume-mutating cleanup.
            if handle is not None:
                await handle.kill()
                await _drain_handle(handle)
            # (ii) credential scrub + (iii) transcript redaction — TERMINAL exits only; a
            # park-suspend keeps the bearer file for the door-less expiry resume to reuse (§A3.9).
            if not park_suspended:
                await self._scrub_credentials(session, ws=ws)
                await self._redact_transcript(session, ws=ws, policy=policy, secrets=_secret_values(spec_env, bearer))
                if not threaded:
                    # An ephemeral session is not cached; destroy it so its volume is reaped now.
                    await session.destroy()

    async def _drive_runner(
        self,
        *,
        handle: Any,
        session: SandboxSession,
        settings: ClaudeCodeSettings,
        thread_id: str | None,
        resume_id: str | None,
        tool_names: list[str],
        options_snapshot: dict[str, Any],
        terminal_key: str | None = None,
    ) -> AsyncIterator[tuple[StreamEvent, bool]]:
        """Consume the runner's up-frames, mapping each to a contract event (paired with a
        park flag). Handles the hello version/session gate, sync asks, async parks, and proxied
        tool calls inline; a ``fatal`` or an error terminal raises loudly.

        On a clean terminal in a resume drive (``terminal_key`` set), the §A3.8 idempotence
        record is written BEFORE the terminal event is yielded, so a crash between here and the
        index finalize leaves a durable record a redelivery re-produces from."""
        allowlist = set(tool_names)
        text_parts: list[str] = []
        seen_hello = False
        async for frame in _iter_up_frames(handle):
            if isinstance(frame, HelloFrame):
                seen_hello = await self._on_hello(frame, session=session, thread_id=thread_id, resume_id=resume_id)
            elif isinstance(frame, EventFrame):
                event = _map_event(frame.event, text_parts)
                if event is not None:
                    yield event, False
            elif isinstance(frame, AskFrame):
                async for event, is_park in self._on_ask(
                    frame, handle=handle, thread_id=thread_id, settings=settings, options_snapshot=options_snapshot
                ):
                    yield event, is_park
                if frame.mode == "async" and thread_id is not None:
                    return  # parked: stop draining, the finally kills the runner
            elif isinstance(frame, ToolCallFrame):
                parked = await self._on_tool_call(frame, handle=handle, allowlist=allowlist, thread_id=thread_id)
                if parked is not None:
                    # The tool async-parked: record it into the durable index, stop the runner,
                    # and surface the suspended terminal — the same park tail the agent's own
                    # async ask takes. thread_id is not None here (a thread-less park was refused
                    # to the model inside _on_tool_call).
                    assert thread_id is not None
                    horizon = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
                    completion_tool, completion_context = get_park_completion()
                    identity = ParkIdentity(
                        agent_name=AGENT_NAME,
                        thread_id=thread_id,
                        rebuild_kwargs={"thread_id": thread_id, "options_snapshot": options_snapshot},
                        bind=True,
                        completion_tool=completion_tool,
                        completion_context=completion_context,
                        retention_bound=horizon,
                    )
                    assert_park_capable(identity, durable=True, retention_bound=horizon)
                    async for event in self._park_on_interaction(
                        parked, identity=identity, handle=handle, thread_id=thread_id, horizon=horizon
                    ):
                        yield event, True
                    return  # parked: stop draining, the finally kills the runner
            elif isinstance(frame, ResultFrame):
                self._emit_usage(frame, settings=settings)
                event = _terminal_event(frame, text_parts)
                # §A3.8: persist the durable terminal record BEFORE reporting, so a crash after
                # this point lets a redelivered resume re-produce the SAME output without re-driving.
                if terminal_key is not None:
                    await self._persist_terminal_record(session, terminal_key, frame, event)
                yield event, False
                return
            elif isinstance(frame, FatalFrame):
                raise ProtocolError(f"runner reported a fatal error: {frame.message}")
        if not seen_hello:
            raise ProtocolError("runner stream ended before the hello init frame")

    async def _on_hello(
        self, frame: HelloFrame, *, session: SandboxSession, thread_id: str | None, resume_id: str | None
    ) -> bool:
        if frame.sdk_version != CLAUDE_AGENT_SDK_VERSION:
            raise ProtocolError(
                f"runner claude_agent_sdk version {frame.sdk_version!r} != adapter pin {CLAUDE_AGENT_SDK_VERSION!r}"
            )
        if thread_id is not None:
            if resume_id is None:
                await self._persist_session_id(session, frame.session_id)
            elif frame.session_id != resume_id:
                raise ProtocolError(f"runner reported session id {frame.session_id!r} != resumed id {resume_id!r}")
        return True

    async def _on_ask(
        self,
        frame: AskFrame,
        *,
        handle: Any,
        thread_id: str | None,
        settings: ClaudeCodeSettings,
        options_snapshot: dict[str, Any],
    ) -> AsyncIterator[tuple[StreamEvent, bool]]:
        if frame.mode == "sync":
            answer = await tai42_app.interactions.ask_user(
                frame.question, answer_format=frame.answer_format, options=frame.options, mode="sync"
            )
            await handle.write_stdin(dump_frame(AnswerFrame(ask_id=frame.ask_id, answer=answer)))
            return
        # async ask
        if thread_id is None:
            # An ephemeral run's uuid4 workspace reaps, so an async park could never resume —
            # refuse loudly by returning a tool error to the model, never a silent unresumable park.
            await handle.write_stdin(
                dump_frame(
                    AnswerFrame(
                        ask_id=frame.ask_id,
                        answer="claude_code cannot async-park a tool-face (thread-less) run; ask synchronously",
                        is_error=True,
                    )
                )
            )
            return
        async for event in self._park_async_ask(
            frame, handle=handle, thread_id=thread_id, settings=settings, options_snapshot=options_snapshot
        ):
            yield event, True

    async def _park_async_ask(
        self,
        frame: AskFrame,
        *,
        handle: Any,
        thread_id: str,
        settings: ClaudeCodeSettings,
        options_snapshot: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        horizon = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
        completion_tool, completion_context = get_park_completion()
        identity = ParkIdentity(
            agent_name=AGENT_NAME,
            thread_id=thread_id,
            rebuild_kwargs={"thread_id": thread_id, "options_snapshot": options_snapshot},
            bind=True,
            completion_tool=completion_tool,
            completion_context=completion_context,
            retention_bound=horizon,
        )
        assert_park_capable(identity, durable=True, retention_bound=horizon)
        suspended = await tai42_app.interactions.ask_user(
            frame.question,
            answer_format=frame.answer_format,
            options=frame.options,
            mode="async",
            expiry_at=horizon,
        )
        assert isinstance(suspended, SuspendedInteraction)
        async for event in self._park_on_interaction(
            suspended, identity=identity, handle=handle, thread_id=thread_id, horizon=horizon
        ):
            yield event

    async def _park_on_interaction(
        self,
        suspended: SuspendedInteraction,
        *,
        identity: ParkIdentity,
        handle: Any,
        thread_id: str,
        horizon: datetime,
    ) -> AsyncIterator[StreamEvent]:
        """Record an already-created parked interaction into the durable index, stop the
        runner, and surface the suspended terminal. The shared park tail BOTH the agent's OWN
        async ask and a tool the agent drives that async-parks cross into the index through —
        each supplies its interaction, this persists + stops + suspends uniformly.

        ``horizon`` is this session's retention bound; the interaction's own deadline (bounded
        by the retention gate) is what the entry is keyed to, falling back to the horizon when
        the sentinel carried none."""
        interaction_id = suspended.interaction_id
        deadline = (suspended.expiry_at or horizon).isoformat()
        # interrupt_id == interaction_id (a one-ask park); persist BEFORE the stop/drain so an
        # instant human answer never waits on the reaper.
        await persist_park(identity, [(interaction_id, {interaction_id: deadline})])
        await handle.write_stdin(dump_frame(StopFrame(reason="park")))
        yield SuspendedFinal(interaction_ids=[interaction_id], thread_id=thread_id, expiry_at=deadline)

    async def _on_tool_call(
        self, frame: ToolCallFrame, *, handle: Any, allowlist: set[str], thread_id: str | None
    ) -> SuspendedInteraction | None:
        """Run one proxied tool call and write its result back to the runner. Returns the park
        sentinel when the tool async-parked (so the drive loop stops the runner and suspends),
        else ``None``.

        A tool that returns a :class:`SuspendedInteraction` async-parked its caller (a generic
        contract sentinel — this loop learns nothing of the tool's resume machinery). On a
        threaded run it is surfaced UP as a park, exactly as the agent's own async ask is; on a
        thread-less (ephemeral) run it can never be resumed, so it is refused loudly to the
        model as a tool error, mirroring the ephemeral async-ask refusal — never a silent
        unresumable park.

        A park this run does not OWN is refused the same way: a tool that drove a nested run
        (a flow, another agent) surfaces a park resumed on THAT run's path, so parking this
        session on it would suspend the session with nothing to resume it
        (:func:`assert_park_adoptable`). This seam claims a park from the SENTINEL only — it
        never reads a wire-form marker off a tool result — so the check here is the whole
        claim check for this agent; there is no second, content-shaped path into its index.

        The dispatch runs delivery-scoped and UNCHAINED: THIS agent owns the interaction and its
        deferred answer, so a parking driver reached through the tool must not capture the
        completion binding addressing that answer (see
        :mod:`~tai42_agents._internal.nested_dispatch`). It is not chained because this session
        resumes by feeding the runner the answer to the interaction its pending tool call is
        waiting on — a park on the CALL is not a shape its protocol can resume — so a nested
        run's park is refused here rather than waited on. The park surfaced up here is therefore
        the agent's own — raised outside the scoped call, and the ownership check above is what
        keeps that true."""
        if frame.tool_name not in allowlist:
            # A compromised session cannot widen its declared tool set — a loud protocol error.
            raise ProtocolError(
                f"runner requested tool {frame.tool_name!r} outside the granted allowlist {sorted(allowlist)}"
            )
        try:
            with nested_tool_dispatch():
                result = await tai42_app.tools.run_tool(frame.tool_name, frame.arguments)
        except Exception as exc:
            await handle.write_stdin(dump_frame(ToolResultFrame(call_id=frame.call_id, result=str(exc), is_error=True)))
            return None
        if isinstance(result, SuspendedInteraction):
            # Ownership first: a park this session could never claim is refused for THAT
            # reason on a thread-less run too, rather than being reported as a thread-less
            # limitation the operator might try to fix by giving the run a thread.
            try:
                assert_park_adoptable(
                    result.resume_owner, interaction_id=result.interaction_id, tool_name=frame.tool_name
                )
            except NestedParkOwnershipError as exc:
                await handle.write_stdin(
                    dump_frame(ToolResultFrame(call_id=frame.call_id, result=str(exc), is_error=True))
                )
                return None
            if thread_id is None:
                await handle.write_stdin(
                    dump_frame(
                        ToolResultFrame(
                            call_id=frame.call_id,
                            result="claude_code cannot async-park a tool-face (thread-less) run",
                            is_error=True,
                        )
                    )
                )
                return None
            return result
        await handle.write_stdin(dump_frame(ToolResultFrame(call_id=frame.call_id, result=result)))
        return None

    def _emit_usage(self, frame: ResultFrame, *, settings: ClaudeCodeSettings) -> None:
        """Emit the SDK-reported usage/cost into the ACTIVE trace (its model calls bypass the
        platform LLM seam). Guarded by ``current_trace_id`` and fail-safe by construction."""
        if frame.usage is None:
            return
        writer = tai42_app.monitoring.active.writer
        if writer.current_trace_id() is None:
            return
        with writer.start_span(name=f"{AGENT_NAME}.generation", kind=SpanKind.LLM, model=settings.model) as span:
            span.update(usage_details=frame.usage)

    # --- workspace authoring ---------------------------------------------------------------

    async def _materialize(
        self,
        session: SandboxSession,
        *,
        ws: str,
        settings: ClaudeCodeSettings,
        bearer: list[_BearerMaterial],
        options_snapshot: dict[str, Any],
    ) -> None:
        """Author the hermetic workspace for one turn (idempotent, re-run every turn)."""
        # RESET the adapter-owned config + payload trees so no agent-written file survives.
        await _exec_ok(session, ["rm", "-rf", f"{ws}/{_CLAUDE_CONFIG_DIR}", f"{ws}/{_RUNNER_PAYLOAD_DIR}"])
        await session.put_file(
            f"{_CLAUDE_CONFIG_DIR}/settings.json",
            json.dumps(self._settings_json(settings), indent=2).encode("utf-8"),
        )
        # RE-WRITE the bearer credential-helper files EVERY TURN from the fresh resolution.
        for material in bearer:
            await session.put_file(f"{_CREDS_DIR}/{material.env_name}", _bearer_file(material).encode("utf-8"))
        await sync_skills(
            session,
            skill_names=options_snapshot["skills"],
            inline_skills=options_snapshot["inline_skills"],
        )
        for name, content in runner_payload_files():
            await session.put_file(f"{_RUNNER_PAYLOAD_DIR}/{name}", content)

    def _settings_json(self, settings: ClaudeCodeSettings) -> dict[str, Any]:
        """The adapter-authored ``.claude/settings.json``: the permission floor, telemetry off,
        and the operator's ``hook_settings`` fragment (verbatim)."""
        doc: dict[str, Any] = {
            "permissions": {"defaultMode": "acceptEdits"},
            "env": {"DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1"},
        }
        if settings.hook_settings is not None:
            doc["hooks"] = settings.hook_settings
        return doc

    def _build_payload(
        self,
        *,
        settings: ClaudeCodeSettings,
        ws: str,
        options_snapshot: dict[str, Any],
        static_env_names: list[str],
        model_env_name: str,
        resume_id: str | None,
    ) -> dict[str, Any]:
        return build_options_payload(
            ws=ws,
            system_prompt=options_snapshot["system_message"],
            tool_names=options_snapshot["tool_names"],
            skills=options_snapshot["skills"],
            subagents=options_snapshot["subagents"],
            response_format=options_snapshot["response_format"],
            max_turns=options_snapshot["max_turns"] or settings.max_turns,
            max_budget_usd=settings.max_budget_usd,
            model=settings.model,
            secret_env_names=credential_env_names(model_env_name, static_env_names),
            session_id=None,
            resume=resume_id,
        )

    # --- session id persistence ------------------------------------------------------------

    async def _read_session_id(self, session: SandboxSession) -> str | None:
        try:
            raw = await session.get_file(_SESSION_ID_PATH)
        except Exception:
            return None
        try:
            record = json.loads(raw.decode("utf-8"))
            session_id = record["session_id"]
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
            raise ProtocolError("persisted .runner/session_id is malformed on a thread with prior turns") from exc
        if not isinstance(session_id, str) or not session_id:
            raise ProtocolError("persisted .runner/session_id is malformed on a thread with prior turns")
        return session_id

    async def _persist_session_id(self, session: SandboxSession, session_id: str) -> None:
        await session.put_file(_SESSION_ID_PATH, json.dumps({"session_id": session_id}).encode("utf-8"))

    # --- terminal idempotence record (§A3.8) ----------------------------------------------

    async def _read_terminal_record(self, session: SandboxSession, superstep_id: str) -> _TerminalRecord | None:
        """Read + schema-validate the durable terminal record for a resumed super-step, or
        ``None`` when there is none (the common first-drive case) or it does not validate.

        UNTRUSTED UNTIL VERIFIED: the in-session Bash can write under ``.runner``, so the record
        is schema-validated and its ``superstep_id`` must match the one being resumed before it is
        honored — a forged record only controls THIS thread's own output. A malformed or mismatched
        record is treated as absent, so the resume re-drives rather than returning garbage."""
        try:
            raw = await session.get_file(f"{_TERMINAL_DIR}/{superstep_id}.json")
        except Exception:
            return None
        try:
            record = _TerminalRecord.model_validate_json(raw)
        except (ValidationError, UnicodeDecodeError):
            return None
        if record.superstep_id != superstep_id:
            return None
        return record

    async def _persist_terminal_record(
        self, session: SandboxSession, superstep_id: str, frame: ResultFrame, event: StreamEvent
    ) -> None:
        """Write the durable terminal record for a resumed super-step BEFORE reporting the
        terminal, capturing the exact output plus the session id + usage for observability."""
        record = _TerminalRecord(
            superstep_id=superstep_id,
            session_id=frame.session_id,
            usage=frame.usage,
            structured=isinstance(event, StructuredFinal),
            text=event.text if isinstance(event, MessageFinal) else None,
            data=event.data if isinstance(event, StructuredFinal) else None,
        )
        await session.put_file(f"{_TERMINAL_DIR}/{superstep_id}.json", record.model_dump_json().encode("utf-8"))

    # --- credentials -----------------------------------------------------------------------

    async def _resolve_creds(
        self, settings: ClaudeCodeSettings
    ) -> tuple[dict[str, SecretStr], list[str], list[_BearerMaterial]]:
        """Resolve the session creds into ``(spec_env, static_env_names, bearer)``.

        The one model credential + every STATIC ``delivery="env"`` value ride ``spec.env`` (baked
        at create); every refreshable ``delivery="bearer"`` cred is materialized per-turn as a
        credential-helper file. A ``required`` connection resolving to nothing raises loudly.
        """
        model_env_name, model_secret = settings.model_credential()
        spec_env: dict[str, SecretStr] = {model_env_name: model_secret}
        static_env_names: list[str] = []
        bearer: list[_BearerMaterial] = []
        for cred in settings.creds:
            if isinstance(cred, StaticCred):
                spec_env[cred.env_name] = cred.value
                static_env_names.append(cred.env_name)
                continue
            assert isinstance(cred, ConnectionCred)
            resolved = await tai42_app.connectors.resolve_connection_auth(
                cred.connection_id, cred.provider_id, cred.sub_service
            )
            self._inject_connection_cred(cred, resolved, spec_env, static_env_names, bearer)
        return spec_env, static_env_names, bearer

    def _inject_connection_cred(
        self,
        cred: ConnectionCred,
        resolved: ResolvedConnectionAuth | None,
        spec_env: dict[str, SecretStr],
        static_env_names: list[str],
        bearer: list[_BearerMaterial],
    ) -> None:
        if resolved is None or (resolved.access_token is None and not resolved.env and not resolved.headers):
            if cred.required:
                raise ClaudeCodeError(
                    f"required connection cred {cred.env_name!r} resolved to nothing for the current caller"
                )
            return
        # Static transport-partitioned env is baked into spec.env regardless of delivery.
        for key, value in resolved.env.items():
            spec_env[key] = value
            static_env_names.append(key)
        if cred.delivery == "env":
            if resolved.access_token is not None:
                spec_env[cred.env_name] = resolved.access_token
                static_env_names.append(cred.env_name)
            return
        # delivery == "bearer": refreshable material re-written per turn as a helper file.
        bearer.append(_BearerMaterial(env_name=cred.env_name, token=resolved.access_token, headers=resolved.headers))

    async def _scrub_credentials(self, session: SandboxSession, *, ws: str) -> None:
        """Remove injected credential MATERIAL under ``.claude-home`` (TERMINAL exits only). An
        un-removable match is a loud error. The invariant: no injected credential material
        persists after a run reaches a TERMINAL state."""
        result = await session.exec(["rm", "-rf", f"{ws}/{_CREDS_DIR}"], timeout_seconds=_SHORT_EXEC_TIMEOUT)
        if result.exit_code != 0:
            raise ClaudeCodeError(f"claude_code credential scrub failed to remove {_CREDS_DIR}: {result.stderr}")

    async def _redact_transcript(self, session: SandboxSession, *, ws: str, policy: Any, secrets: list[str]) -> None:
        """When the platform ``scrub_transcript`` flag is ON, redact injected-credential VALUES
        from the KEPT session transcript CONTENT (distinct from the credential-FILE scrub above:
        this rewrites text, never deletes the transcript — resume still reads it). The knob OFF
        leaves the transcript verbatim (the stated env-credential residual stands)."""
        if not getattr(policy, "scrub_transcript", False) or not secrets:
            return
        script = (
            "import os,sys\n"
            "root=sys.argv[1]\n"
            "marks=sys.argv[2:]\n"
            "for dp,_,fns in os.walk(root):\n"
            "  for fn in fns:\n"
            "    p=os.path.join(dp,fn)\n"
            "    try:\n"
            "      t=open(p,encoding='utf-8').read()\n"
            "    except (OSError,UnicodeDecodeError):\n"
            "      continue\n"
            "    n=t\n"
            "    for m in marks:\n"
            "      n=n.replace(m,'[REDACTED]')\n"
            "    if n!=t:\n"
            "      open(p,'w',encoding='utf-8').write(n)\n"
        )
        result = await session.exec(
            ["python", "-c", script, f"{ws}/.claude-home", *secrets], timeout_seconds=_SHORT_EXEC_TIMEOUT
        )
        if result.exit_code != 0:
            raise ClaudeCodeError(f"claude_code transcript redaction failed: {result.stderr}")


# --- module helpers ------------------------------------------------------------------------


async def _aiter(items: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    for item in items:
        yield item


async def _exec_ok(session: SandboxSession, argv: list[str]) -> None:
    result = await session.exec(argv, timeout_seconds=_SHORT_EXEC_TIMEOUT)
    if result.exit_code != 0:
        raise ClaudeCodeError(f"workspace command {argv!r} failed ({result.exit_code}): {result.stderr}")


def _secret_values(spec_env: dict[str, SecretStr], bearer: list[_BearerMaterial]) -> list[str]:
    """Every injected secret STRING value (for transcript redaction): the baked ``spec.env``
    values plus each bearer token/header value."""
    values = [v.get_secret_value() for v in spec_env.values()]
    for material in bearer:
        if material.token is not None:
            values.append(material.token.get_secret_value())
        values.extend(v.get_secret_value() for v in material.headers.values())
    return [v for v in values if v]


def _bearer_file(material: _BearerMaterial) -> str:
    lines: list[str] = []
    if material.token is not None:
        lines.append(f"Authorization: Bearer {material.token.get_secret_value()}")
    for key, value in material.headers.items():
        lines.append(f"{key}: {value.get_secret_value()}")
    return "\n".join(lines) + "\n"


async def _iter_up_frames(handle: Any) -> AsyncIterator[Any]:
    """Yield parsed up-frames off the exec handle's byte stream, buffering whole JSON lines and
    ignoring stderr (diagnostics). A ``SandboxStreamExit`` ends the stream."""
    buffer = bytearray()
    async for chunk in handle.output:
        if isinstance(chunk, SandboxStreamExit):
            break
        assert isinstance(chunk, SandboxStreamChunk)
        if chunk.stream != "stdout":
            continue
        buffer.extend(chunk.data)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            text = line.decode("utf-8", "replace").strip()
            if text:
                yield parse_up_frame(text)


async def _drain_handle(handle: Any) -> None:
    """Await the killed handle's stream to completion so no runner code is still mid-drive."""
    try:
        async for _ in handle.output:
            pass
    except (SandboxExecTimeoutError, ProtocolError):
        pass


def _map_event(event: dict[str, Any], text_parts: list[str]) -> StreamEvent | None:
    """Map one runner ``event`` payload to a contract stream event (or ``None`` to skip)."""
    kind = event.get("kind")
    if kind == "text":
        text = event.get("text", "")
        text_parts.append(text)
        return MessageDelta(text=text)
    if kind == "thinking":
        text = event.get("text", "")
        if not text.strip():
            return None
        return ReasoningStep(text=text)
    if kind == "tool_use":
        return ToolCallStep(tool=event.get("name", ""), args=event.get("input", {}) or {}, call_id=event.get("id", ""))
    if kind == "tool_result":
        return ToolResultStep(
            tool="", call_id=event.get("id", ""), result=event.get("content"), is_error=bool(event.get("is_error"))
        )
    return None


def _event_from_terminal_record(record: _TerminalRecord) -> StreamEvent:
    """Reconstruct the terminal stream event from a §A3.8 record, mirroring ``_terminal_event``
    so a redelivered resume re-produces the SAME drained value the original terminal did."""
    if record.structured:
        return StructuredFinal(data=record.data)
    return MessageFinal(text=record.text or "")


def _terminal_event(frame: ResultFrame, text_parts: list[str]) -> StreamEvent:
    if frame.terminal_reason not in {"completed", "success"}:
        raise ProtocolError(f"runner terminated with reason {frame.terminal_reason!r} (subtype {frame.subtype!r})")
    if frame.is_structured and frame.result is not None:
        return StructuredFinal(data=frame.result)
    if isinstance(frame.result, str):
        return MessageFinal(text=frame.result)
    return MessageFinal(text="".join(text_parts))
