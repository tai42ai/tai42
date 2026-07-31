"""The ``output_schema`` tool extension (TRANSFORMER kind).

Branches a tool into an ``<tool>_output_schema`` variant that forces the tool's advertised OUTPUT
schema to a caller-supplied JSON Schema (config ``"schema"`` key) and validates every result
against it. The config-accepting factory takes the four-argument ``(func, name, description,
config)`` form. The variant advertises the schema through the return-annotation channel and at
runtime validates the result, returning it unchanged on a match and raising loudly (with a
JSON-path) on a mismatch. The configured schema is meta-schema-checked at bind time, failing loudly.
"""

import inspect
from typing import Any

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.data import snake_to_pascal
from tai42_kit.utils.data.json_schema_util import (
    check_json_schema,
    json_schema_to_pydantic_model,
)

from tai42_toolbox._internal.extensions.output_schema_validator import enforce_output_schema


def _configured_schema(config: dict[str, Any]) -> dict[str, Any]:
    """The JSON Schema dict from the config, validated at bind time.

    Raises loudly and distinctly on a missing ``"schema"`` key (``ValueError``) or a ``"schema"``
    that is not valid draft-2020-12 (from the kit validator).
    """
    try:
        schema = config["schema"]
    except (KeyError, TypeError):
        raise ValueError(
            "the 'output_schema' tool extension requires a 'schema' key in its config carrying a JSON Schema dict"
        ) from None

    check_json_schema(schema)
    return schema


@tai42_app.extensions.extension(kind=ExtensionKind.TRANSFORMER, name="output_schema")
def output_schema(func, orig_name, orig_desc, config):
    """Branch ``func`` into an ``<orig_name>_output_schema`` variant that advertises
    and validates its output against the configured JSON Schema."""
    schema = _configured_schema(config)

    new_name = f"{orig_name}_{output_schema.__name__}"
    output_model = json_schema_to_pydantic_model(schema, f"{snake_to_pascal(orig_name)}Output")

    # Force the return annotation to the configured schema so the branch advertises it.
    composed_sig = inspect.signature(func).replace(return_annotation=output_model)

    async def func_impl(*args: Any, **kwargs: Any) -> Any:
        return await enforce_output_schema(func, args, kwargs, schema)

    description = f"""Output-schema extension for '{orig_name}'.
Calls the original tool and validates its result against a fixed JSON Schema, raising on any mismatch.

Original doc:
{orig_desc}
"""

    return create_function(
        func_signature=composed_sig,
        func_impl=func_impl,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=description,
    )
