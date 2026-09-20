"""The api_tools include/exclude list-edit door and its name-list editor."""

from __future__ import annotations

from typing import Any

from tai42_contract.app.responses import ApplyResponse

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations._broadcast import apply_response, translate_orphan_env_write

from .models import ApiToolsListsUpdate


def _edit_name_list(current: list[Any], add: list[str], remove: list[str], field: str) -> list[str]:
    """The edited ``api_tools`` include/exclude list.

    A name in ``add`` already present is refused (``ValueError`` naming it); a name in
    ``remove`` absent is refused (``LookupError`` naming it). Order-stable: kept names
    first, additions appended. Pure / re-runnable: builds a fresh list from the arguments.
    """
    names = [str(n) for n in current]
    present = set(names)
    already = sorted(n for n in add if n in present)
    if already:
        raise ValueError(f"api_tools {field}: already present: {already}")
    absent = sorted(n for n in remove if n not in present)
    if absent:
        raise LookupError(f"api_tools {field}: not present: {absent}")
    dropped = set(remove)
    return [n for n in names if n not in dropped] + list(add)


@operation(
    summary="Add/remove names on the api_tools include/exclude lists and hot-reload",
    tags=["manifest"],
    authority_changing=True,
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
    request_model=ApiToolsListsUpdate,
    response_model=ApplyResponse,
)
async def update_api_tools(
    include_add: list[str] | None = None,
    include_remove: list[str] | None = None,
    exclude_add: list[str] | None = None,
    exclude_remove: list[str] | None = None,
) -> dict:
    """Add/remove names on the ``api_tools`` include/exclude lists and hot-reload the manifest.

    Returns the apply response. An empty change, a duplicate add, or a missing remove is a
    loud 400/404.
    """
    include_add = include_add or []
    include_remove = include_remove or []
    exclude_add = exclude_add or []
    exclude_remove = exclude_remove or []
    with translate_orphan_env_write():
        try:
            if not (include_add or include_remove or exclude_add or exclude_remove):
                raise ValueError("nothing to change")  # noqa: TRY301 translated to BadRequestError below

            def mutator(document: dict[str, Any]) -> None:
                api_tools = document.get("api_tools")
                if not isinstance(api_tools, dict):
                    api_tools = {}
                    document["api_tools"] = api_tools
                included = _edit_name_list(api_tools.get("include") or [], include_add, include_remove, "include")
                excluded = _edit_name_list(api_tools.get("exclude") or [], exclude_add, exclude_remove, "exclude")
                api_tools["include"] = included
                api_tools["exclude"] = excluded

            result = await ConfigService.from_app().apply_change(mutator)
        except BackendNeedsBusError as exc:
            raise BadRequestError(str(exc)) from exc
        except LookupError as exc:
            raise NotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise BadRequestError(f"invalid api_tools config: {exc}") from exc
        return apply_response(result)
