# tai42-toolbox

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

The reference contrib package for the TAI ecosystem — an opt-in, manifest-loaded
collection of generic **tools** and **tool extensions**.

> A plugin extends the platform; a *tool extension* extends a single tool.

Everything here registers through the `tai42_app` handle from `tai42_contract.app`
and is loaded by the host from the manifest (`tools[].module` /
`extensions_modules`). Its only tai-* dependencies are `tai42-contract` (the
interfaces it registers through) and `tai42-kit` (the curl client, the jq
compiler, and the llm/embedding factories the heavier modules wire to). It
**never** imports the skeleton — the toolbox is contract-facing.

The current release line tracks the **7.x contract** (`tai42-contract>=7,<8`).

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server (or let it ride in through `tai42-skeleton`'s `[toolbox]` extra):

```bash
uv add tai42-toolbox
uv add "tai42-toolbox[http]"    # add only the extras you use
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/toolbox
uv add --editable "../tai-toolbox[http]"
```

The base install stays light. Each heavier module is gated behind its own extra;
a module whose extra is missing fails loudly at import with an
`install tai42-toolbox[extra]` hint — never a silent skip.

| Extra | Pulls | Backs |
|---|---|---|
| `http` | `tai42-kit[curl]` | the `request` tool |
| `prometheus` | `prometheus-client` | the `prometheus_metrics` tool extension |
| `chain` | `tai42-kit[jq]` | the `chain` tool extension |
| `embeddings` | `tai42-kit[llm]` | the `generate_embeddings` and `pad_embeddings` tools |
| `proxy` | `PySocks` | SOCKS routing in the `proxy` tool extension (its HTTP/HTTPS path is stdlib-only) |

## Catalog

One row per registered artifact. **Kind** is the `ExtensionKind` for a tool
extension (Wrapper / Transformer) or `Tool` for a tool. **Extra** is the gating
optional dependency, or `—` when the module needs none. A gated module whose
extra is not installed fails loudly at import with an `install tai42-toolbox[extra]`
hint — never a silent skip.

### Tool extensions

| Name | Kind | Extra | Description |
|---|---|---|---|
| `cache` | Wrapper | `—` | Memoizes the tool's results by call arguments and single-flights concurrent identical calls; adds an `exp` (seconds-to-live) control kwarg. |
| `proxy` | Wrapper | `proxy` | Routes the tool's connections through a SOCKS or HTTP/HTTPS proxy; adds a `proxies` pool control kwarg. The `proxy` extra (PySocks) is needed only for SOCKS; the HTTP/HTTPS path is stdlib-only. |
| `prometheus_metrics` | Wrapper | `prometheus` | Records a call count, an error count, and a run-time histogram per invocation; presents the tool's input schema unchanged. |
| `batch` | Transformer | `—` | Runs many instances of the tool from a list of parameter sets, sequentially or in parallel, returning results in input order. |
| `chain` | Transformer | `chain` | Calls the tool, transforms its output with a jq expression, then calls a second named tool with the result. |
| `output_schema` | Transformer | `—` | Forces the tool's advertised output schema to a JSON Schema supplied in the combo's `config` (under a `schema` key) and validates each result against it, raising loudly on a mismatch. |

### Tools

| Name | Kind | Extra | Description |
|---|---|---|---|
| `generate_embeddings` | Tool | `embeddings` | Generates embedding vectors for a string or list of strings via the configured provider. |
| `pad_embeddings` | Tool | `embeddings` | Pads embedding vectors with zeros up to a fixed width; raises rather than truncating a vector already wider than the target. |
| `request` | Tool | `http` | Executes an HTTP request through tai42-kit's pooled curl client, with optional keyed session reuse (shared cookies and connections). |
| `generate_uuid` | Tool | `—` | Generates a random version-4 UUID. |
| `current_time_info` | Tool | `—` | Returns the current time as a structured object (UTC, local, and high-precision system timestamps). |

## Security

**SSRF guard (`request` tool).** The `request` tool fetches a caller-supplied URL
server-side, so an agent steered by a poisoned page could aim it at internal-only
services or the cloud metadata endpoint. A guard is **on by default**: it resolves
each target host and refuses any address that is private, loopback, link-local,
reserved, multicast, or unspecified, and refuses a response larger than a cap
(loudly, never truncated). The guard itself lives in tai42-kit
(`tai42_kit.net.url_guard`) and is shared with kit's `fetch_url` download; configure
it with `TAI_URL_GUARD_`-prefixed settings:

| Setting | Default | Meaning |
|---|---|---|
| `TAI_URL_GUARD_ENABLED` | `true` | Turn the guard off entirely. |
| `TAI_URL_GUARD_ALLOW_CIDRS` | `[]` | CIDR ranges to opt back in (e.g. `["10.0.0.0/8"]`) for a deployment that deliberately reaches internal hosts. |
| `TAI_URL_GUARD_MAX_RESPONSE_BYTES` | `104857600` | Refuse a response body larger than this. |
| `TAI_URL_GUARD_MAX_REDIRECTS` | `20` | Redirect-follow limit for a guarded download (kit's `fetch_url`). |

**DNS-rebinding.** Resolving a host and then letting the HTTP client re-resolve
it at connect time leaves a rebinding window (an attacker answers a public
address during the check and an internal one at connect). The `request` tool
closes this by resolving each host once, validating every returned address, and
pinning curl to that validated address per request via `CURLOPT_RESOLVE`
(resolve-once, connect-to-validated-IP). TLS is unaffected — SNI and certificate
validation still use the original hostname. As defense in depth, network egress
controls remain worthwhile but are not required to close rebinding here.

**Response size cap.** The `request` tool streams the response body and enforces
the size cap chunk by chunk, refusing an over-cap body the moment it crosses the
limit — never after buffering the whole body.

While the guard is on, the `request` tool does not auto-follow redirects (its curl
transport has no per-hop hook to re-check each redirect target), so a 3xx is
returned as-is.

**`proxy` routing (task-scoped).** The `proxy` tool extension routes a wrapped
tool's connections through a chosen proxy. Routing is dispatched on a contextvar,
so it is **task-scoped**: only the routed call's own connections go through the
proxy, and concurrent routed and unrouted calls run in parallel without affecting
each other. The HTTP/HTTPS proxy verifies the proxy's own TLS certificate and
sends the destination host to the proxy to resolve (no local DNS leak). A proxy
that accepts TCP but never answers the handshake cannot wedge the call: the
connect/negotiation runs under `PROXY_CONNECT_TIMEOUT`.

Configure the proxy pool and policy with `PROXY_`-prefixed settings:

| Setting | Default | Meaning |
|---|---|---|
| `PROXY_POOL` | `[]` | The operator proxy-URL pool routed over when a call passes no `proxies` (or a call selects from). |
| `PROXY_ALLOW_CALLER_URLS` | `false` | When false, a call's `proxies` kwarg may only select URLs already in `PROXY_POOL`; an out-of-pool URL is refused loudly. Set true to let a call supply its own proxy URL. |
| `PROXY_CONNECT_TIMEOUT` | `30` | Connect/negotiation timeout (seconds) for the proxy handshake. |

With `PROXY_ALLOW_CALLER_URLS=true`, the routed call's own traffic — its SNI and
any non-TLS content — is visible to the caller-chosen proxy; enable it only when a
caller supplying its own proxy is intended.

**Proxy host SSRF guard (caller-supplied only).** A caller-supplied proxy host (a
selected URL not in `PROXY_POOL`, and each proxy in the `request` tool's own
`session_params.proxies`/`proxy`) is run through the SSRF guard when the guard is
on: its host is resolved and validated, and the connection targets the validated
address rather than re-resolving the hostname, closing DNS-rebinding on the proxy
hop. For an HTTPS proxy the TLS SNI and certificate verification still use the
original hostname. A rejected proxy host raises loudly; nothing is routed. The
`PROXY_POOL` entries are **trusted-by-configuration** (an operator-vetted pool,
often a private-IP corporate egress proxy) and are not guarded — guarding them
would break a standard deploy and force an over-broad `TAI_URL_GUARD_ALLOW_CIDRS`.

**Routing propagation boundary.** The route follows the asyncio task tree: child
tasks and `asyncio.to_thread` inherit it, but `loop.run_in_executor` and a raw
`threading.Thread` do not — a tool offloading network work that way silently
escapes routing. The dispatcher is Python-level and cannot see sockets opened by a
C-level network stack (a libcurl-class client); for the `request` tool, use its
native `proxies` session param rather than this extension. The route is captured
when a socket is created, so a tool that reuses a keep-alive connection from a pool
built outside the routed window is not re-routed. A tool that needs routing to bind
reliably must open its connections inside the routed window (e.g. a per-call
session), not reuse a shared pool built outside it.

The route is also **process-local**: the extension registers with
`requires_body_locality=True` because its wrapper routes only when it executes in
the process running the tool body. In a stacked combo the `proxy` extension must
bind inside any execution-relocating extension (a BACKEND-kind extension, which
ships the tool body to a worker process), so the routing wrapper travels with the
body; the platform reads the marker and rejects the wrong order at bind time.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --editable ".[dev,http,prometheus,chain,embeddings,proxy]"
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
uv run --no-sync pytest --cov --cov-report=term-missing
```

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
