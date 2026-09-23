"""``claude_code`` as an :class:`Agent`: drive the real ``claude`` binary through the Claude Agent SDK.

Runs INSIDE a sandbox session over the versioned JSONL exec protocol.
The plugin server NEVER imports the SDK — the SDK lives in the session image and only the
runner payload (shipped as DATA, executed in-session) imports it. This module is the ADAPTER:
it acquires a sandbox session (:func:`require_sandbox`, the one raising chokepoint), authors a
hermetic workspace every turn, drives the runner, and maps its up-frames to contract stream
events. Park/resume ride the shared ``_internal/park`` machinery; the SDK's model cost is
emitted into the active trace (its model calls bypass the platform LLM seam).

The ``crash_resume`` setting is DECLARED to the skeleton at registration as
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
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final, cast
from uuid import uuid4

from pydantic import BaseModel, SecretStr
from tai42_contract.access_control.context import get_current_user_id
from tai42_contract.agent import Agent
from tai42_contract.agent.events import StreamEvent, SuspendedFinal
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    SuspendedInteraction,
    current_execution_identity,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)
from tai42_contract.monitoring.models import SpanKind
from tai42_contract.sandbox import SandboxSession
from tai42_contract.template import TemplatedText

from tai42_agents._internal.park import (
    AGENT_RESUME_TOOL_NAME,
    ParkIdentity,
    assert_park_capable,
    bind_resume_per_step,
    chain_routing_slots,
    persist_park,
    register_agent_resume_tool,
    workspace_lease,
)
from tai42_agents._internal.park.index import compute_superstep_id
from tai42_agents._internal.park.lease import LEASE_HEADROOM_SECONDS
from tai42_agents._internal.reject import (
    reject_blank_memory_keys,
    reject_unhonored,
    resolve_response_format,
)
from tai42_agents._internal.render import render_message
from tai42_agents._internal.sandbox_util import build_policied_spec, workspace_key_for
from tai42_agents.claude_code.credentials import (
    _BearerMaterial,
    _secret_values,
    redact_transcript,
    resolve_creds,
    scrub_credentials,
)
from tai42_agents.claude_code.errors import ClaudeCodeError
from tai42_agents.claude_code.frames import drain_handle, iter_up_frames, map_event, terminal_event
from tai42_agents.claude_code.inputs import ClaudeCodeInput, InlineSkillShape, SubagentSpecShape
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
    dump_frame,
)
from tai42_agents.claude_code.session_records import (
    event_from_terminal_record,
    persist_session_id,
    persist_terminal_record,
    read_session_id,
    read_terminal_record,
)
from tai42_agents.claude_code.settings import (
    ClaudeCodeSettings,
    claude_code_crash_resume,
    claude_code_settings,
)
from tai42_agents.claude_code.skills_sync import validate_name
from tai42_agents.claude_code.tool_call import run_proxied_tool_call
from tai42_agents.claude_code.workspace import _RUNNER_PAYLOAD_DIR, build_payload, materialize

AGENT_NAME: Final[str] = "claude_code"

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

    A platform ``ask(mode="async")`` — the agent's OWN async ask (``_park_async_ask``)
    OR one a proxied tool this drive runs raises — reads the bound continuation to stamp
    ``continuation_tool`` onto the parked interaction, so a later ``agent_resume`` re-enters
    this agent. Without it, the async ask refuses loudly ("async ask requires a resuming
    driver") and no park is produced. The resume tool name is bound ONLY when the run is
    threaded; otherwise ``None`` is bound — NOT a no-op. An ephemeral uuid4 workspace reaps and
    can never resume, so binding ``None`` SHADOWS any ambient resume continuation a park-capable
    caller left bound, so a non-threaded run nested under one cannot inherit it and mint a park
    it can never resume: its async ask refuses pre-persist. Mirrors the LangGraph driver's
    ``park_continuation``.
    """
    name = AGENT_RESUME_TOOL_NAME if threaded else None
    token = set_resume_continuation_tool(name)
    try:
        yield
    finally:
        reset_resume_continuation_tool(token)


# A parking agent binds the hidden ``agent_resume`` continuation from its OWN registration:
# a claude-only box must still bind it, or every async park strands. Per-epoch
# idempotent, so a combined box binds it exactly once.
register_agent_resume_tool()

# Per-worker live-session cache for DETERMINISTIC (threaded) workspaces, so a reused session
# keeps its create-time ``spec.env`` (the refresh channel is the per-turn bearer file, not a
# re-baked env). Ephemeral ``uuid4`` workspaces are never cached. The cross-worker mutex is the
# Redis workspace lease; this cache only avoids re-creating within one worker.
_LIVE_SESSIONS: dict[str, SandboxSession] = {}


@tai42_app.agents.agent(
    AGENT_NAME,
    tags={"agents", "coding", "claude"},
    meta={"tai42/crash_resume": claude_code_crash_resume()},
)
class ClaudeCodeAgent(Agent):
    """The ``claude_code`` agent: drives the real ``claude`` binary in a sandbox over the exec protocol."""

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
        response_format = await resolve_response_format(AGENT_NAME, kwargs.get("response_format"))
        return await self._drain(self.astream(**kwargs), response_format=response_format)

    async def astream(
        self,
        *,
        user_message: TemplatedText | None = None,
        system_message: TemplatedText | None = None,
        tool_names: Sequence[str] = (),
        skills: Sequence[str] = (),
        inline_skills: Sequence[dict[str, Any] | InlineSkillShape] = (),
        response_format: TemplatedText | dict[str, Any] | None = None,
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
        response_format = await resolve_response_format(AGENT_NAME, response_format)

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

        rendered_user = await render_message(user_message, allow_empty=False, field="user_message")
        rendered_system = await render_message(system_message)
        subagent_defs = [
            {
                **s.model_dump(mode="json", exclude={"system_prompt"}),
                "system_prompt": await render_message(s.system_prompt),
            }
            for s in subs
        ]

        # The RENDERED texts are what the snapshot persists, so a resume re-drives the
        # same prompt, system prompt, and subagent prompts rather than re-rendering a
        # stored resource that may have changed underneath the parked turn.
        options_snapshot = {
            "user_message": rendered_user,
            "system_message": rendered_system,
            "tool_names": tools_list,
            "skills": skill_names,
            "inline_skills": [s.model_dump(mode="json") for s in inline],
            "response_format": response_format,
            "max_turns": max_turns,
            "subagents": subagent_defs,
        }

        async for event in self._drive_workspace(
            settings=settings,
            thread_id=thread_id,
            prompt={"text": rendered_user},
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

        CRASH-AFTER-TERMINAL IDEMPOTENCE: the drive is keyed by the super-step's
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

        spec_env, static_env_names, bearer = await resolve_creds(settings)
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
            # Crash-after-terminal idempotence: a redelivered resume whose winner already
            # drove this super-step to a clean terminal reattaches the SAME durable volume and
            # finds its record — re-produce the stored output and DO NOT re-drive the SDK session.
            # The credential scrub/redact still run in the finally (idempotent on an already-scrubbed
            # volume), keeping the no-residual invariant on this exit too.
            if terminal_key is not None:
                record = await read_terminal_record(session, terminal_key)
                if record is not None:
                    yield event_from_terminal_record(record)
                    return

            await materialize(session, ws=ws, settings=settings, bearer=bearer, options_snapshot=options_snapshot)

            resume_id = await read_session_id(session) if threaded else None
            payload = build_payload(
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
                await drain_handle(handle)
            # (ii) credential scrub + (iii) transcript redaction — TERMINAL exits only; a
            # park-suspend keeps the bearer file for the door-less expiry resume to reuse.
            if not park_suspended:
                await scrub_credentials(session, ws=ws)
                await redact_transcript(session, ws=ws, policy=policy, secrets=_secret_values(spec_env, bearer))
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
        """Consume the runner's up-frames, mapping each to a contract event (paired with a park flag).

        A thin per-frame dispatch. Handles the hello version/session gate, sync
        asks, async parks, and proxied tool calls inline; a ``fatal`` or an error terminal
        raises loudly.

        On a clean terminal in a resume drive (``terminal_key`` set), the idempotence
        record is written BEFORE the terminal event is yielded, so a crash between here and the
        index finalize leaves a durable record a redelivery re-produces from.
        """
        allowlist = set(tool_names)
        text_parts: list[str] = []
        seen_hello = False
        async for frame in iter_up_frames(handle):
            if isinstance(frame, HelloFrame):
                seen_hello = await self._on_hello(frame, session=session, thread_id=thread_id, resume_id=resume_id)
                continue
            parked = False
            async for event, is_park in self._project_frame(
                frame,
                handle=handle,
                session=session,
                settings=settings,
                thread_id=thread_id,
                allowlist=allowlist,
                options_snapshot=options_snapshot,
                text_parts=text_parts,
                terminal_key=terminal_key,
            ):
                if is_park:
                    parked = True
                yield event, is_park
            # A park (the agent's own async ask or a tool that parked) and the clean terminal
            # both stop the drive: the finally kills the runner. Every other frame drains on.
            if parked or isinstance(frame, ResultFrame):
                return
        if not seen_hello:
            raise ProtocolError("runner stream ended before the hello init frame")

    async def _project_frame(
        self,
        frame: Any,
        *,
        handle: Any,
        session: SandboxSession,
        settings: ClaudeCodeSettings,
        thread_id: str | None,
        allowlist: set[str],
        options_snapshot: dict[str, Any],
        text_parts: list[str],
        terminal_key: str | None,
    ) -> AsyncIterator[tuple[StreamEvent, bool]]:
        """Map one non-hello up-frame to its ``(event, is_park)`` pairs.

        An event frame's stream event, a sync/async ask, a proxied tool call (with its park tail),
        or the terminal. A ``fatal`` raises loudly.
        """
        if isinstance(frame, EventFrame):
            event = map_event(frame.event, text_parts)
            if event is not None:
                yield event, False
        elif isinstance(frame, AskFrame):
            async for pair in self._on_ask(
                frame, handle=handle, thread_id=thread_id, settings=settings, options_snapshot=options_snapshot
            ):
                yield pair
        elif isinstance(frame, ToolCallFrame):
            async for pair in self._on_tool_call_frame(
                frame,
                handle=handle,
                allowlist=allowlist,
                thread_id=thread_id,
                settings=settings,
                options_snapshot=options_snapshot,
            ):
                yield pair
        elif isinstance(frame, ResultFrame):
            event = await self._on_result_frame(
                frame, session=session, settings=settings, text_parts=text_parts, terminal_key=terminal_key
            )
            yield event, False
        elif isinstance(frame, FatalFrame):
            raise ProtocolError(f"runner reported a fatal error: {frame.message}")

    async def _on_tool_call_frame(
        self,
        frame: ToolCallFrame,
        *,
        handle: Any,
        allowlist: set[str],
        thread_id: str | None,
        settings: ClaudeCodeSettings,
        options_snapshot: dict[str, Any],
    ) -> AsyncIterator[tuple[StreamEvent, bool]]:
        """Run one proxied tool call and, when it async-parked, take the park tail.

        Builds the park identity, gates capability, and records it into the durable index — the same
        tail the agent's own async ask takes. Yields ``(event, True)`` for each surfaced park event;
        a tool that ran (or errored) to a plain result yields nothing.
        """
        parked = await run_proxied_tool_call(frame, handle=handle, allowlist=allowlist, thread_id=thread_id)
        if parked is None:
            return
        # The tool async-parked: record it into the durable index, stop the runner, and surface
        # the suspended terminal — the same park tail the agent's own async ask takes. thread_id
        # is not None here (a thread-less park was refused to the model inside run_proxied_tool_call).
        if thread_id is None:
            raise AssertionError
        horizon = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
        completion_tool, completion_context = chain_routing_slots()
        execution_identity, execution_fingerprint = current_execution_identity()
        identity = ParkIdentity(
            agent_name=AGENT_NAME,
            thread_id=thread_id,
            rebuild_kwargs={"thread_id": thread_id, "options_snapshot": options_snapshot},
            bind=True,
            completion_tool=completion_tool,
            completion_context=completion_context,
            retention_bound=horizon,
            execution_identity=execution_identity,
            execution_fingerprint=execution_fingerprint,
        )
        assert_park_capable(identity, durable=True, retention_bound=horizon)
        async for event in self._park_on_interaction(
            parked, identity=identity, handle=handle, thread_id=thread_id, horizon=horizon
        ):
            yield event, True

    async def _on_result_frame(
        self,
        frame: ResultFrame,
        *,
        session: SandboxSession,
        settings: ClaudeCodeSettings,
        text_parts: list[str],
        terminal_key: str | None,
    ) -> StreamEvent:
        """Emit the SDK usage and build the terminal event.

        In a resume drive (``terminal_key`` set) the durable terminal record is persisted BEFORE the
        caller reports it, so a crash after this point lets a redelivery re-produce the SAME output.
        """
        self._emit_usage(frame, settings=settings)
        event = terminal_event(frame, text_parts)
        if terminal_key is not None:
            await persist_terminal_record(session, terminal_key, frame, event)
        return event

    async def _on_hello(
        self, frame: HelloFrame, *, session: SandboxSession, thread_id: str | None, resume_id: str | None
    ) -> bool:
        if frame.sdk_version != CLAUDE_AGENT_SDK_VERSION:
            raise ProtocolError(
                f"runner claude_agent_sdk version {frame.sdk_version!r} != adapter pin {CLAUDE_AGENT_SDK_VERSION!r}"
            )
        if thread_id is not None:
            if resume_id is None:
                await persist_session_id(session, frame.session_id)
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
            answer = await tai42_app.interactions.ask(
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
        completion_tool, completion_context = chain_routing_slots()
        execution_identity, execution_fingerprint = current_execution_identity()
        identity = ParkIdentity(
            agent_name=AGENT_NAME,
            thread_id=thread_id,
            rebuild_kwargs={"thread_id": thread_id, "options_snapshot": options_snapshot},
            bind=True,
            completion_tool=completion_tool,
            completion_context=completion_context,
            retention_bound=horizon,
            execution_identity=execution_identity,
            execution_fingerprint=execution_fingerprint,
        )
        assert_park_capable(identity, durable=True, retention_bound=horizon)
        suspended = await tai42_app.interactions.ask(
            frame.question,
            answer_format=frame.answer_format,
            options=frame.options,
            mode="async",
            expiry_at=horizon,
        )
        if not (isinstance(suspended, SuspendedInteraction)):
            raise AssertionError  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
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
        """Record an already-created parked interaction into the durable index, stop the runner, and suspend.

        The shared park tail BOTH the agent's OWN
        async ask and a tool the agent drives that async-parks cross into the index through —
        each supplies its interaction, this persists + stops + suspends uniformly.

        ``horizon`` is this session's retention bound; the interaction's own deadline (bounded
        by the retention gate) is what the entry is keyed to, falling back to the horizon when
        the sentinel carried none.
        """
        interaction_id = suspended.interaction_id
        deadline = (suspended.expiry_at or horizon).isoformat()
        # interrupt_id == interaction_id (a one-ask park); persist BEFORE the stop/drain so an
        # instant human answer never waits on the reaper.
        await persist_park(identity, [(interaction_id, {interaction_id: deadline})])
        await handle.write_stdin(dump_frame(StopFrame(reason="park")))
        yield SuspendedFinal(
            interaction_ids=[interaction_id],
            thread_id=thread_id,
            # The caller subset the parked sentinel carried (``[id]`` for a ``to="caller"`` ask,
            # empty for a user ask), so the receipt partitions caller from user asks.
            caller_interaction_ids=suspended.caller_interaction_ids,
            expiry_at=deadline,
        )

    def _emit_usage(self, frame: ResultFrame, *, settings: ClaudeCodeSettings) -> None:
        """Emit the SDK-reported usage/cost into the ACTIVE trace (its model calls bypass the platform LLM seam).

        Guarded by ``current_trace_id`` and fail-safe by construction.
        """
        if frame.usage is None:
            return
        writer = tai42_app.monitoring.active.writer
        if writer.current_trace_id() is None:
            return
        with writer.start_span(name=f"{AGENT_NAME}.generation", kind=SpanKind.LLM, model=settings.model) as span:
            span.update(usage_details=frame.usage)


# --- module helpers ------------------------------------------------------------------------


async def _aiter(items: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    for item in items:
        yield item
