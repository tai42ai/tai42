"""The ``proxy`` tool extension (WRAPPER kind).

Branches a tool into a ``<tool>_proxy`` variant that runs the wrapped tool with a task-scoped route
active, so connections it opens are proxied while concurrent tasks are untouched. Adds one reserved
control kwarg ``proxies`` (a proxy-URL pool). Routing is dispatched on a contextvar (see
:mod:`tai42_toolbox._internal.extensions.socket_routing`), so it follows the asyncio task tree
(``to_thread`` inherits; ``run_in_executor`` and raw threads do not) and a C-level client (curl's
own transport — use its native ``proxies`` param) is not covered.

Registers ``requires_body_locality=True``: the route and socket dispatcher are process-local, so in
a stacked combo the wrapper must bind INSIDE any execution-relocating extension; the bind engine enforces this.
"""

import inspect

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind

from tai42_toolbox._internal.extensions.proxy_context import build_route
from tai42_toolbox._internal.extensions.signature import with_added_params
from tai42_toolbox._internal.extensions.socket_routing import install_dispatcher, route


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="proxy", requires_body_locality=True)
def proxy(func, name, description):
    """Branch ``func`` into a proxy-routed ``<name>_proxy`` variant.

    Each call routes independently on a contextvar, so concurrent proxy-routed calls
    run in parallel without colliding. A tool that needs its connections routed must
    open them inside the call (the route follows the task tree, not a shared
    connection pool built outside it).
    """
    install_dispatcher()

    async def wrapper(*args, **kwargs):
        proxies = kwargs.pop("proxies", None)
        cfg = await build_route(proxies)
        with route(cfg):
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)

    sig = inspect.signature(func)
    proxies_param = inspect.Parameter(
        "proxies", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=list[str] | None
    )
    new_sig = with_added_params(sig, proxies_param)

    new_name = f"{name}_{proxy.__name__}"

    routed_doc = (
        f"{description}\n\n"
        "Note: this proxy-routed tool routes the connections it opens through a "
        "proxy; each call routes independently and can run in parallel with others."
    )

    return create_function(
        func_signature=new_sig,
        func_impl=wrapper,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=routed_doc,
    )


# Subtracted from the branch's input schema by the apply site so the WRAPPER schema check ignores it.
proxy.reserved_params = frozenset({"proxies"})
