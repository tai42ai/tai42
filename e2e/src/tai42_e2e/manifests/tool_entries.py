"""Tool and router manifest entries shared across the stack profiles."""

from __future__ import annotations

# The manifest title the probe tools load under; the prometheus extension stamps
# it as the ``title`` label, so metrics assertions reference this constant.
PROBE_TOOLS_TITLE = "e2e-probes"


# Toolbox extension branch modules whose import registers the WRAPPER/TRANSFORMER
# branches the suite attaches (prometheus_metrics/batch/proxy). The BACKEND
# branches (sync_task/...) register with the backend_module import, not here.
_EXTENSION_MODULES = [
    "tai42_toolbox.extensions.prometheus",
    "tai42_toolbox.extensions.batch",
    "tai42_toolbox.extensions.proxy",
]


# Router modules the suite drives — every HTTP route is opt-in at its module import.
# Curated stacks pin ``default_routers="none"`` alongside this list so their served
# surface stays exactly what they list, never ballooning to the skeleton's ``"all"``
# default set.
_CORE_ROUTERS = [
    "tai42_skeleton.routers.health",
    "tai42_skeleton.routers.metrics",
    "tai42_skeleton.routers.tools",
    "tai42_skeleton.routers.config",
    "tai42_skeleton.routers.manifest",
    "tai42_skeleton.routers.hooks",
    "tai42_skeleton.routers.tool_runs",
    "tai42_skeleton.routers.schedules",
    "tai42_skeleton.routers.interactions",
    "tai42_skeleton.routers.states",
    "tai42_skeleton.routers.sub_mcp",
    "tai42_skeleton.routers.tool_extensions",
    "tai42_skeleton.routers.presets",
    "tai42_skeleton.routers.tool_meta",
    "tai42_skeleton.routers.extensions",
    "tai42_skeleton.routers.templates",
    "tai42_skeleton.routers.storage",
    "tai42_skeleton.routers.backend",
    "tai42_skeleton.routers.sandbox",
]


def _probe_tools_entry(
    *, with_backend_branches: bool, with_schedule_branch: bool = False, with_monitor_branch: bool = False
) -> dict:
    """The SUT-side probe tools entry. ``with_backend_branches`` attaches the
    ``sync_task`` combos (needs a backend stack). ``with_schedule_branch`` attaches
    ``schedule_task`` to ``e2e_record`` — enabled only on ``build_schedule_stack``,
    never on the reload-heavy ``replicas`` profile (on celery a ``schedule_task``
    tool riding reload churn can leave the prefork pool unable to dispatch)."""
    extensions: dict[str, list[list[str]]] = {
        "e2e_echo": [["prometheus_metrics"]],
        "e2e_fail": [["prometheus_metrics"]],
        "e2e_http_probe": [["proxy"]],
    }
    if with_backend_branches:
        # Chain sync_task onto the prometheus-wrapped echo so the counter-wrapped tool
        # runs inside the backend worker, and attach it to the worker-info probe.
        extensions["e2e_echo"] = [["prometheus_metrics"], ["prometheus_metrics", "sync_task"]]
        extensions["e2e_worker_info"] = [["sync_task"]]
        # Branch the record-then-block probe so the worker-crash spec can start a run
        # observably in-flight in the worker, SIGKILL it, and read a bounded terminal.
        extensions["e2e_slow_task"] = [["sync_task"]]
        # Branch the drain probe (started → sleep → done) so the recycle-drain spec can read
        # whether an in-flight backend job DRAINED to completion across a recycle.
        extensions["e2e_drain_probe"] = [["sync_task"]]
        # Universal backend-execution door: run_tool_sync_task runs any registered tool
        # by name inside the backend worker. Its base is the FIXTURE ``run_tool`` dispatch
        # tool (the skeleton ``run_tool`` op is a tier-1 meta-executor blocked from the MCP
        # surface, so it has no projected base to wrap).
        extensions["run_tool"] = [["sync_task"]]
    if with_schedule_branch:
        # Branch e2e_record into e2e_record_schedule_task: a recurring schedule that runs
        # e2e_record each firing, so the scheduling spec reads periodicity off the channel.
        extensions["e2e_record"] = [["schedule_task"]]
        # Branch run_tool into run_tool_schedule_task: the scheduling analog of
        # run_tool_sync_task — schedules any tool by name (the sweep-schedulable leg).
        extensions["run_tool"] = [*extensions.get("run_tool", []), ["schedule_task"]]
    if with_monitor_branch:
        # Trace a standalone e2e_echo call as one TOOL span, giving the observability read
        # surface a run to serve back (build_monitoring_stack's langfuse records it).
        extensions["e2e_echo"] = [*extensions["e2e_echo"], ["monitor"]]
    return {"title": PROBE_TOOLS_TITLE, "module": "tai42_e2e_fixtures.tools", "extensions": extensions}


# The platform-seams fixture module: at import it registers a preset seed, a rename
# referee (holder + raising arms), and the invocation-seam probe tool. ``include`` names
# only the probe tool it exposes on the surface; the seed/referee registrations run on
# every (re)import regardless of the include filter.
_SEAMS_TOOLS_ENTRY = {
    "title": "e2e-seams",
    "module": "tai42_e2e_fixtures.seams",
    "include": ["e2e_invocation_probe"],
}


# The ask_user HITL builtin tool module. Other management ops (reload_config,
# reload_mcp, register_hook, templates, notify_user) project onto the MCP tool surface
# from the operations registry via ``api_tools`` (see ``_PROJECTED_API_TOOLS``).
_INTERACTIONS_ENTRY = {"title": "builtin-interactions", "module": "tai42_skeleton.tools.builtin.interactions"}


# The builtin subject-state tools (state_read / state_replace / state_merge /
# state_apply): the module registers all four, so no ``include`` filter is needed. They
# resolve their subject from the ambient door context and write through the
# ``tai42_app.states`` facet — a stack with the states component bound exercises the
# composed door→tool→store path; a stack without it sees the tools refuse 501 on call.
_STATES_TOOLS_ENTRY = {"title": "builtin-states", "module": "tai42_skeleton.tools.builtin.states"}


# Projects the management operations onto the MCP tool surface with the default curation:
# destructive ops exposed, the tier-1 ``run_tool`` blocked, tier-2 ``/api/auth/*`` ops
# default-excluded. Which ops project is scoped by the profile's mounted routers.
_PROJECTED_API_TOOLS = {"enabled": True}


def _builtin_entries() -> list[dict]:
    """The builtin ``tools[]`` entries a profile carries: ``builtin-interactions``
    (ask_user) and ``builtin-states`` (the four subject-state tools). Management ops
    project via ``api_tools`` instead."""
    return [_INTERACTIONS_ENTRY, _STATES_TOOLS_ENTRY]


def _toolbox_tools_entry() -> dict:
    return {
        "title": "toolbox",
        "module": "tai42_toolbox.tools.generate_uuid",
        "include": ["generate_uuid"],
    }


# Each toolbox tool lives in its own module (one tool per module), so a profile
# names the module and ``include``s the one tool it registers. ``request`` needs the
# ``http`` extra (already in the e2e env) and ``generate_embeddings`` the ``embeddings``
# extra — both fail LOUDLY at import when their extra is absent,
# so a stack carrying them refuses to boot rather than silently dropping the tool.
_TOOLBOX_EXTRA_TOOL_ENTRIES: list[dict] = [
    {"title": "toolbox-request", "module": "tai42_toolbox.tools.request", "include": ["request"]},
    {
        "title": "toolbox-embeddings",
        "module": "tai42_toolbox.tools.generate_embeddings",
        "include": ["generate_embeddings"],
    },
    {"title": "toolbox-pad-embeddings", "module": "tai42_toolbox.tools.pad_embeddings", "include": ["pad_embeddings"]},
    {
        "title": "toolbox-current-time",
        "module": "tai42_toolbox.tools.current_time_info",
        "include": ["current_time_info"],
    },
]


# Every agent in the reference package, one manifest entry each (own module, so
# ``include`` names the one agent that module registers). All run on the scripted LLM
# stub + local stack resources.
_AGENT_ENTRIES: list[dict] = [
    {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
    {"title": "tai-agents-deep", "module": "tai42_agents.langchain_deep_agent", "include": ["langchain_deep_agent"]},
    {"title": "tai-agents-refine", "module": "tai42_agents.refine_agent", "include": ["refine_agent"]},
    {"title": "tai-agents-voting", "module": "tai42_agents.voting_agent", "include": ["voting_agent"]},
    {
        "title": "tai-agents-retrieval",
        "module": "tai42_agents.retrieval_tools_agent",
        "include": ["retrieval_tools_agent"],
    },
    {"title": "tai-agents-vqa", "module": "tai42_agents.vqa_agent", "include": ["vqa_agent"]},
]
