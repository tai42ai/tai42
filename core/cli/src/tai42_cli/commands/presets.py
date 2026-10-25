"""``tai presets`` — manage preset tools and their versions.

Thin wrappers over the ``/api/presets*`` routes. A preset whose ``base_tool`` is a
spec-runnable agent's run tool is an authored agent; ``create`` writes both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_records,
    emit_result,
    load_json_object_arg,
    load_text_arg,
    parse_extension_combos,
    parse_json_object,
    seg,
)

app = typer.Typer(
    name="presets",
    help="Manage preset tools and their versions.",
    no_args_is_help=True,
)

_EXTENSIONS_HELP = (
    "Extension combos as a JSON array of combos. Each combo is a non-empty array of elements, and an "
    'element is an extension name (\'"chain"\') or a {"name","config"} object binding config to it '
    '(e.g. \'[[{"name":"output_schema","config":{"schema":{"type":"object"}}}]]\').'
)
_EXTENSIONS_HELP_CLEARABLE = f"{_EXTENSIONS_HELP} '[]' clears them."

_KWARGS_FILE_HELP = (
    "Read the fixed kwargs JSON object from a file, or from stdin when the path is '-', instead of putting a "
    "secret on the command line (a value on argv leaks via ps and shell history). Mutually exclusive with --kwargs."
)

# The reference convention a fixed-kwargs value may use so the credential is never baked
# into the stored body: the server resolves the variable at run time.
_KWARGS_REFERENCE_SENTENCE = (
    "A string value written '!ENV ${VAR}' is a secret reference the server resolves from that "
    "environment variable at run time, storing only the reference."
)
_KWARGS_HELP = f"Baked fixed kwargs as a JSON object. {_KWARGS_REFERENCE_SENTENCE}"


_SCHEMA_HELP = "A JSON Schema object, or a template reference object."
_STATE_BINDING_HELP = 'The preset\'s state binding as a JSON object: {"states": [...]}.'
_INPUT_MAP_HELP = (
    "A jq program that turns the caller's input (.) into the base tool's arguments; $fixed holds the "
    "preset's fixed kwargs. Needs --input-schema."
)
_OUTPUT_MAP_HELP = "A jq program that reshapes the base tool's result (.) before the output schema is checked."
_MAP_TEMPLATE_HELP = 'The program as a template object (JSON), e.g. {"id": "…", "kwargs": {}}.'
_OBJECT_FILE_HELP = "Read the JSON object from a file, or from stdin when the path is '-'."
_TEXT_FILE_HELP = "Read the program text from a file, or from stdin when the path is '-'."
_CLEAR_HELP = "Remove the field on this version (sent as null)."

_OutputSchema = Annotated[str | None, typer.Option("--output-schema", help=_SCHEMA_HELP)]
_OutputSchemaFile = Annotated[str | None, typer.Option("--output-schema-file", help=_OBJECT_FILE_HELP)]
_InputSchema = Annotated[str | None, typer.Option("--input-schema", help=_SCHEMA_HELP)]
_InputSchemaFile = Annotated[str | None, typer.Option("--input-schema-file", help=_OBJECT_FILE_HELP)]
_StateBinding = Annotated[str | None, typer.Option("--state-binding", help=_STATE_BINDING_HELP)]
_StateBindingFile = Annotated[str | None, typer.Option("--state-binding-file", help=_OBJECT_FILE_HELP)]
_InputMap = Annotated[str | None, typer.Option("--input-map", help=_INPUT_MAP_HELP)]
_InputMapFile = Annotated[str | None, typer.Option("--input-map-file", help=_TEXT_FILE_HELP)]
_InputMapTemplate = Annotated[str | None, typer.Option("--input-map-template", help=_MAP_TEMPLATE_HELP)]
_OutputMap = Annotated[str | None, typer.Option("--output-map", help=_OUTPUT_MAP_HELP)]
_OutputMapFile = Annotated[str | None, typer.Option("--output-map-file", help=_TEXT_FILE_HELP)]
_OutputMapTemplate = Annotated[str | None, typer.Option("--output-map-template", help=_MAP_TEMPLATE_HELP)]
_ClearOutputSchema = Annotated[bool, typer.Option("--clear-output-schema", help=_CLEAR_HELP)]
_ClearInputSchema = Annotated[bool, typer.Option("--clear-input-schema", help=_CLEAR_HELP)]
_ClearStateBinding = Annotated[bool, typer.Option("--clear-state-binding", help=_CLEAR_HELP)]
_ClearInputMap = Annotated[bool, typer.Option("--clear-input-map", help=_CLEAR_HELP)]
_ClearOutputMap = Annotated[bool, typer.Option("--clear-output-map", help=_CLEAR_HELP)]


@dataclass(frozen=True)
class _BodyFieldOptions:
    """The raw option values of the schema, state-binding and map body fields."""

    output_schema: str | None = None
    output_schema_file: str | None = None
    input_schema: str | None = None
    input_schema_file: str | None = None
    state_binding: str | None = None
    state_binding_file: str | None = None
    input_map: str | None = None
    input_map_file: str | None = None
    input_map_template: str | None = None
    output_map: str | None = None
    output_map_file: str | None = None
    output_map_template: str | None = None

    def stdin_sources(self) -> list[str | None]:
        return [
            self.output_schema_file,
            self.input_schema_file,
            self.state_binding_file,
            self.input_map_file,
            self.output_map_file,
        ]

    def given(self) -> dict[str, bool]:
        """Whether each body field was given in any of its forms, keyed by the field."""
        return {
            "output_schema": self.output_schema is not None or self.output_schema_file is not None,
            "input_schema": self.input_schema is not None or self.input_schema_file is not None,
            "state_binding": self.state_binding is not None or self.state_binding_file is not None,
            "input_map": any(v is not None for v in (self.input_map, self.input_map_file, self.input_map_template)),
            "output_map": any(v is not None for v in (self.output_map, self.output_map_file, self.output_map_template)),
        }


def _map_arg(inline: str | None, file: str | None, template: str | None, *, option: str) -> str | dict[str, Any] | None:
    """A map's program: its text (inline or a file) as a string, or its template as an object."""
    if sum(value is not None for value in (inline, file, template)) > 1:
        raise typer.BadParameter(
            f"give only one of {option}, {option}-file or {option}-template", param_hint=f"{option}-template"
        )
    if template is not None:
        return parse_json_object(template, param_hint=f"{option}-template")
    return load_text_arg(inline, file, param_hint=option, file_param_hint=f"{option}-file")


def _body_fields(options: _BodyFieldOptions, *, kwargs_file: str | None) -> dict[str, Any]:
    """The given schema, state-binding and map fields as they go on the wire; an omitted one is absent."""
    if sum(source == "-" for source in (kwargs_file, *options.stdin_sources())) > 1:
        raise typer.BadParameter("only one option may read from stdin ('-')")
    values: dict[str, Any] = {
        "output_schema": load_json_object_arg(
            options.output_schema,
            options.output_schema_file,
            param_hint="--output-schema",
            file_param_hint="--output-schema-file",
        ),
        "input_schema": load_json_object_arg(
            options.input_schema,
            options.input_schema_file,
            param_hint="--input-schema",
            file_param_hint="--input-schema-file",
        ),
        "state_binding": load_json_object_arg(
            options.state_binding,
            options.state_binding_file,
            param_hint="--state-binding",
            file_param_hint="--state-binding-file",
        ),
        "input_map": _map_arg(
            options.input_map, options.input_map_file, options.input_map_template, option="--input-map"
        ),
        "output_map": _map_arg(
            options.output_map, options.output_map_file, options.output_map_template, option="--output-map"
        ),
    }
    return {field: value for field, value in values.items() if value is not None}


def _cleared_fields(options: _BodyFieldOptions, clears: dict[str, bool]) -> dict[str, None]:
    """The cleared fields, each sent as ``null``; a field given with its clear is a usage error."""
    given = options.given()
    for field, clear in clears.items():
        if clear and given[field]:
            option = "--" + field.replace("_", "-")
            raise typer.BadParameter(f"pass either {option} or --clear-{option[2:]}, not both")
    return {field: None for field, clear in clears.items() if clear}


@app.command("list")
@covers(("GET", "/api/presets"))
def list_presets(ctx: typer.Context) -> None:
    """List the store-backed (versioned) presets.

    Example: ``tai presets list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/presets")
    emit_records(ctx_obj, data, route=("GET", "/api/presets"))


@app.command("get")
@covers(("GET", "/api/presets/{name}"))
def get_preset(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Preset name.")]) -> None:
    """Get a store-backed preset's record and active body.

    Example: ``tai presets get my_preset``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/presets/{seg(name)}")
    emit_result(ctx_obj, data)


@app.command("create")
@covers(("POST", "/api/presets"))
def create_preset(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset name.")],
    base_tool: Annotated[str, typer.Option("--base-tool", help="The registered non-preset tool the preset wraps.")],
    description: Annotated[
        str, typer.Option("--description", help="The preset's LLM-facing description (required, non-empty).")
    ],
    kwargs: Annotated[str | None, typer.Option("--kwargs", help=_KWARGS_HELP)] = None,
    kwargs_file: Annotated[str | None, typer.Option("--kwargs-file", help=_KWARGS_FILE_HELP)] = None,
    extensions: Annotated[str | None, typer.Option("--extensions", help=_EXTENSIONS_HELP)] = None,
    output_schema: _OutputSchema = None,
    output_schema_file: _OutputSchemaFile = None,
    input_schema: _InputSchema = None,
    input_schema_file: _InputSchemaFile = None,
    state_binding: _StateBinding = None,
    state_binding_file: _StateBindingFile = None,
    input_map: _InputMap = None,
    input_map_file: _InputMapFile = None,
    input_map_template: _InputMapTemplate = None,
    output_map: _OutputMap = None,
    output_map_file: _OutputMapFile = None,
    output_map_template: _OutputMapTemplate = None,
) -> None:
    """Create a versioned preset (or an authored agent, when the base tool is an agent).

    Example: ``tai presets create greet --base-tool echo --description 'Greet a user' --kwargs '{"prefix":"hi"}'``

    Example: ``tai presets create g --base-tool echo --description d --input-schema-file s.json --input-map-file m.jq``
    """
    ctx_obj = app_context(ctx)
    fields = _body_fields(
        _BodyFieldOptions(
            output_schema,
            output_schema_file,
            input_schema,
            input_schema_file,
            state_binding,
            state_binding_file,
            input_map,
            input_map_file,
            input_map_template,
            output_map,
            output_map_file,
            output_map_template,
        ),
        kwargs_file=kwargs_file,
    )
    fixed_kwargs = load_json_object_arg(kwargs, kwargs_file, param_hint="--kwargs", file_param_hint="--kwargs-file")
    body: dict = {
        "name": name,
        "base_tool": base_tool,
        "description": description,
        "fixed_kwargs": fixed_kwargs if fixed_kwargs is not None else {},
    }
    if extensions is not None:
        body["extensions"] = parse_extension_combos(extensions, param_hint="--extensions")
    body.update(fields)
    with ctx_obj.client() as client:
        data = client.post("/api/presets", json=body)
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("DELETE", "/api/presets/{name}"))
def delete_preset(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Preset name.")]) -> None:
    """Delete a preset.

    Example: ``tai presets delete my_preset``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/presets/{seg(name)}")
    emit_result(ctx_obj, data)


@app.command("versions")
@covers(("GET", "/api/presets/{name}/versions"))
def list_versions(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Preset name.")]) -> None:
    """List a preset's version history.

    Example: ``tai presets versions my_preset``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/presets/{seg(name)}/versions")
    emit_records(ctx_obj, data, route=("GET", "/api/presets/{name}/versions"))


@app.command("get-version")
@covers(("GET", "/api/presets/{name}/versions/{version}"))
def get_version(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset name.")],
    version: Annotated[int, typer.Argument(help="Version number.")],
) -> None:
    """Get a specific preset version.

    Example: ``tai presets get-version my_preset 3``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/presets/{seg(name)}/versions/{seg(version)}")
    emit_result(ctx_obj, data)


@app.command("save-version")
@covers(("POST", "/api/presets/{name}/versions"))
def save_version(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset name.")],
    kwargs: Annotated[
        str | None,
        typer.Option(
            "--kwargs",
            help="New fixed kwargs (JSON object); omit to carry forward. " + _KWARGS_REFERENCE_SENTENCE,
        ),
    ] = None,
    kwargs_file: Annotated[str | None, typer.Option("--kwargs-file", help=_KWARGS_FILE_HELP)] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="New description (non-empty); omit to carry the active one forward."),
    ] = None,
    extensions: Annotated[str | None, typer.Option("--extensions", help=_EXTENSIONS_HELP_CLEARABLE)] = None,
    output_schema: _OutputSchema = None,
    output_schema_file: _OutputSchemaFile = None,
    input_schema: _InputSchema = None,
    input_schema_file: _InputSchemaFile = None,
    state_binding: _StateBinding = None,
    state_binding_file: _StateBindingFile = None,
    input_map: _InputMap = None,
    input_map_file: _InputMapFile = None,
    input_map_template: _InputMapTemplate = None,
    output_map: _OutputMap = None,
    output_map_file: _OutputMapFile = None,
    output_map_template: _OutputMapTemplate = None,
    clear_output_schema: _ClearOutputSchema = False,
    clear_input_schema: _ClearInputSchema = False,
    clear_state_binding: _ClearStateBinding = False,
    clear_input_map: _ClearInputMap = False,
    clear_output_map: _ClearOutputMap = False,
) -> None:
    """Save a new preset version.

    Omitted fields carry forward; ``--extensions '[]'`` sends the explicit clear sentinel and each
    ``--clear-*`` option sends its field as ``null``.

    Example: ``tai presets save-version my_preset --kwargs '{"n":2}'``

    Example: ``tai presets save-version greet --output-schema-file schema.json --clear-output-map``
    """
    ctx_obj = app_context(ctx)
    options = _BodyFieldOptions(
        output_schema,
        output_schema_file,
        input_schema,
        input_schema_file,
        state_binding,
        state_binding_file,
        input_map,
        input_map_file,
        input_map_template,
        output_map,
        output_map_file,
        output_map_template,
    )
    cleared = _cleared_fields(
        options,
        {
            "output_schema": clear_output_schema,
            "input_schema": clear_input_schema,
            "state_binding": clear_state_binding,
            "input_map": clear_input_map,
            "output_map": clear_output_map,
        },
    )
    fields = _body_fields(options, kwargs_file=kwargs_file)
    body: dict = {}
    fixed_kwargs = load_json_object_arg(kwargs, kwargs_file, param_hint="--kwargs", file_param_hint="--kwargs-file")
    if fixed_kwargs is not None:
        body["fixed_kwargs"] = fixed_kwargs
    if description is not None:
        body["description"] = description
    if extensions is not None:
        body["extensions"] = parse_extension_combos(extensions, param_hint="--extensions")
    body.update(fields)
    body.update(cleared)
    if not body:
        raise typer.BadParameter(
            "provide at least one of --kwargs, --description, --extensions, --output-schema, --input-schema, "
            "--state-binding, --input-map, --output-map (or their -file / -template forms), or a --clear-* option"
        )
    with ctx_obj.client() as client:
        data = client.post(f"/api/presets/{seg(name)}/versions", json=body)
    emit_result(ctx_obj, data)


@app.command("rollback")
@covers(("POST", "/api/presets/{name}/rollback"))
def rollback_preset(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset name.")],
    version: Annotated[int, typer.Argument(help="Target version to make active.")],
) -> None:
    """Roll a preset back to a prior version.

    Example: ``tai presets rollback my_preset 2``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/presets/{seg(name)}/rollback", json={"version": version})
    emit_result(ctx_obj, data)


@app.command("rename")
@covers(("POST", "/api/presets/{name}/rename"))
def rename_preset(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Current preset name.")],
    new_name: Annotated[str, typer.Argument(help="New preset name.")],
) -> None:
    """Rename a preset — its name is its live tool name, so the tool is rebound.

    Example: ``tai presets rename old_name new_name``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/presets/{seg(name)}/rename", json={"new_name": new_name})
    emit_result(ctx_obj, data)


@app.command("referees")
@covers(("GET", "/api/presets/{name}/referees"))
def preset_referees(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset name.")],
    action: Annotated[
        Literal["rename", "delete"],
        typer.Option("--action", help="The action to preview: rename or delete."),
    ],
) -> None:
    """List what holds this preset: everything the given action would strand, one per row.

    Example: ``tai presets referees my_preset --action rename``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/presets/{seg(name)}/referees", params={"action": action})
    emit_records(ctx_obj, data, route=("GET", "/api/presets/{name}/referees"))


@app.command("validate")
@covers(("POST", "/api/presets/validate"))
def validate_preset(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset name.")],
    base_tool: Annotated[
        str | None, typer.Option("--base-tool", help="The base tool (required for a new preset).")
    ] = None,
    kwargs: Annotated[str | None, typer.Option("--kwargs", help=_KWARGS_HELP)] = None,
    kwargs_file: Annotated[str | None, typer.Option("--kwargs-file", help=_KWARGS_FILE_HELP)] = None,
    description: Annotated[
        str | None, typer.Option("--description", help="The preset's LLM-facing description.")
    ] = None,
    extensions: Annotated[str | None, typer.Option("--extensions", help=_EXTENSIONS_HELP)] = None,
    output_schema: _OutputSchema = None,
    output_schema_file: _OutputSchemaFile = None,
    input_schema: _InputSchema = None,
    input_schema_file: _InputSchemaFile = None,
    state_binding: _StateBinding = None,
    state_binding_file: _StateBindingFile = None,
    input_map: _InputMap = None,
    input_map_file: _InputMapFile = None,
    input_map_template: _InputMapTemplate = None,
    output_map: _OutputMap = None,
    output_map_file: _OutputMapFile = None,
    output_map_template: _OutputMapTemplate = None,
    clear_output_schema: _ClearOutputSchema = False,
    clear_input_schema: _ClearInputSchema = False,
    clear_state_binding: _ClearStateBinding = False,
    clear_input_map: _ClearInputMap = False,
    clear_output_map: _ClearOutputMap = False,
) -> None:
    """Dry-run a preset draft without writing anything.

    Reports whether it would be accepted as a create (a new name) or a new version (an existing name).
    On an existing name each ``--clear-*`` option checks the draft with that field removed.

    Example: ``tai presets validate greet --base-tool echo --description 'Greet' --kwargs '{"prefix":"hi"}'``

    Example: ``tai presets validate greet --output-map '{answer: .}' --clear-input-map``
    """
    ctx_obj = app_context(ctx)
    options = _BodyFieldOptions(
        output_schema,
        output_schema_file,
        input_schema,
        input_schema_file,
        state_binding,
        state_binding_file,
        input_map,
        input_map_file,
        input_map_template,
        output_map,
        output_map_file,
        output_map_template,
    )
    cleared = _cleared_fields(
        options,
        {
            "output_schema": clear_output_schema,
            "input_schema": clear_input_schema,
            "state_binding": clear_state_binding,
            "input_map": clear_input_map,
            "output_map": clear_output_map,
        },
    )
    fields = _body_fields(options, kwargs_file=kwargs_file)
    body: dict = {"name": name}
    if base_tool is not None:
        body["base_tool"] = base_tool
    fixed_kwargs = load_json_object_arg(kwargs, kwargs_file, param_hint="--kwargs", file_param_hint="--kwargs-file")
    if fixed_kwargs is not None:
        body["fixed_kwargs"] = fixed_kwargs
    if description is not None:
        body["description"] = description
    if extensions is not None:
        body["extensions"] = parse_extension_combos(extensions, param_hint="--extensions")
    body.update(fields)
    body.update(cleared)
    with ctx_obj.client() as client:
        data = client.post("/api/presets/validate", json=body)
    emit_result(ctx_obj, data)


@app.command("set-version-tags")
@covers(("PUT", "/api/presets/{name}/versions/{version}/tags"))
def set_version_tags(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset name.")],
    version: Annotated[int, typer.Argument(help="Version number.")],
    tags: Annotated[list[str] | None, typer.Argument(help="The tags to set; none clears them to [].")] = None,
) -> None:
    """Replace a preset version's tags (labels only — no rebind).

    Zero tag arguments clears them to ``[]``.

    Example: ``tai presets set-version-tags my_preset 2 stable reviewed``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.put(f"/api/presets/{seg(name)}/versions/{seg(version)}/tags", json={"tags": list(tags or [])})
    emit_result(ctx_obj, data)
