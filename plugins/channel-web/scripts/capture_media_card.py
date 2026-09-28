"""Capture the maintained docs screenshots of the channel-web media UI.

Path A (maintained): the channel-web doc page pulls its images from the PLUGIN's own
``docs/images/`` tree, so these captures write committed plugin assets —
``src/tai42_channel_web/docs/images/media-card.png`` (the agent-sent media card) plus the
visitor's inbound-attachment shots ``inbound-attachment-tray-{light,dark}.png`` (the
composer with a ready attachment in the tray) and ``inbound-attachment-row-{light,dark}.png``
(a transcript row carrying an inbound image and a document) — rather than one-offs. The
recapture workflow (``.github/workflows/channel-web-media-card-screenshot.yml``) runs this
script and opens a PR with the refreshed PNGs whenever the media UI source changes.

The media card is captured over ``build_channel_stack`` through the ``/api/notifications``
door; the inbound-attachment shots drive the composer's real upload + send over the
``build_web_media_stack`` profile (the upload door + the redis conversations backend + the
storage-local blob provider), so a visitor attaches, uploads, and sends exactly as in a
browser, in both the light and dark themes.

What the media-card capture does, all through the plugin's own public doors and the proven e2e harness:

* Boots a channel stack carrying ``tai42_channel_web`` (the same ``build_channel_stack``
  profile the channel e2e suite runs on), so the web chat page, the SSE stream, and the
  ``/api/notifications`` send door are live.
* Opens the visitor chat page in a real Chromium via Playwright — the page bundle mints
  and registers its own ``tai_web_session`` cookie on load, exactly as a first-time
  visitor's browser does.
* Reads the session cookie back out of the browser, resolves the server-side visitor id
  it is registered against (the conversation address the doors never disclose), and
  drives a media-card notification addressed at that visitor pair over
  ``POST /api/notifications`` — an https image with a caption plus two tappable option
  chips.
* Waits for the card to render with its image loaded and both chips visible, then
  screenshots the ``.tcw-media`` element (a tight crop) to the docs image path.

The media card renders an image ONLY from an absolute ``https`` URL (an ``http:``/``data:``
image is refused), so the placeholder image beside this script is served over a local
https server whose self-signed certificate the browser is told to accept
(``ignore_https_errors``). The server under test never fetches the image — only the
browser does — so nothing but the visitor's own browser reaches the local origin.

Run it (from the monorepo root, in the e2e venv, with the shared Redis/Postgres up):

    uv run --no-sync python plugins/channel-web/scripts/capture_media_card.py
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import secrets
import shutil
import ssl
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import httpx
import trustme
from playwright.sync_api import Page, sync_playwright
from tai42_e2e import Infra, diagnostics, manifests
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.channel_stubs import TINY_PDF, TINY_PNG, FakeSlack, FakeTelegram, FakeTwilio, FakeWhatsApp
from tai42_e2e.harness import connect_infra
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_channel_stack, build_web_media_stack
from tai42_e2e.seeding import seed_bridge_authz
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.webchat import SESSION_COOKIE, registered_visitor_id

# The monorepo root, four levels up from this file (plugins/channel-web/scripts/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLACEHOLDER = Path(__file__).resolve().parent / "assets" / "placeholder.png"
_DOCS_IMAGES = _REPO_ROOT / "plugins" / "channel-web" / "src" / "tai42_channel_web" / "docs" / "images"
_DEFAULT_OUT = _DOCS_IMAGES / "media-card.png"

# The visitor's inbound-attachment fixtures (platform rule: generic, domain-agnostic names).
_INBOUND_IMAGE_NAME = "photo.png"
_INBOUND_DOC_NAME = "document.pdf"

# The two themes the docs page renders under; Playwright drives each through the media query the
# stylesheet keys its dark palette off.
_THEMES: tuple[Literal["light", "dark"], ...] = ("light", "dark")

# Domain-agnostic card content (platform rule: no business-domain words, no client/product
# names anywhere). A neutral caption and two generic option chips.
_CAPTION = "Status update"
_OPTIONS = [
    {"kind": "reply", "text": "View details"},
    {"kind": "reply", "text": "Dismiss"},
]


class _ImageHandler(http.server.BaseHTTPRequestHandler):
    """Serve the placeholder PNG for every GET — the one image the card fetches."""

    image_bytes: bytes = b""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.image_bytes)))
        self.end_headers()
        self.wfile.write(self.image_bytes)

    # ``format`` mirrors the http.server.BaseHTTPRequestHandler.log_message(self, format, *args) override.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet: the capture's own prints are the only output that matters.
        return


@contextlib.contextmanager
def _https_image_server(image_bytes: bytes):
    """Start a local https server (self-signed for 127.0.0.1) serving ``image_bytes`` as a PNG, yielding its URL.

    The URL is ``https://127.0.0.1:<port>/placeholder.png``. The browser fetches it with
    ``ignore_https_errors`` set; the server under test never dials it.
    """
    handler = type("_BoundImageHandler", (_ImageHandler,), {"image_bytes": image_bytes})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(ctx)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, name="media-card-image", daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"https://127.0.0.1:{port}/placeholder.png"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


@contextlib.contextmanager
def _channel_stack(infra: Infra, root: Path):
    """Boot the channel-web-carrying stack (``build_channel_stack``) and yield it, torn down leak-free at exit.

    The web channel has no vendor, but the profile also loads the telegram/slack/twilio plugins, so their
    in-process recording stubs stand in for the outbound API base URLs the profile points at.
    """
    fake_telegram = FakeTelegram()
    fake_slack = FakeSlack()
    fake_twilio = FakeTwilio()
    fake_telegram.start()
    fake_slack.start()
    fake_twilio.start()
    resource_kwargs = {
        "telegram_api_base_url": fake_telegram.api_base_url,
        "slack_api_base_url": fake_slack.api_base_url,
        "twilio_api_base_url": fake_twilio.api_base_url,
    }
    try:
        resources, config = allocate_and_build(infra, root, build_channel_stack, resource_kwargs, False)
        stack = TaiStack(config, infra, resources, root)
        with stack, diagnostics.track(stack):
            yield stack
    finally:
        fake_twilio.stop()
        fake_slack.stop()
        fake_telegram.stop()


def _capture(stack: TaiStack, image_url: str, out_path: Path, *, headed: bool) -> None:
    """Open the visitor page, drive the media-card notification, and screenshot the card."""
    identity = manifests.WEB_IDENTITY
    page_url = f"http://{stack.host}:{stack.port_b}/api/channels/web/chat/{identity}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        # ignore_https_errors accepts the placeholder server's self-signed cert; a 2x scale
        # gives a crisp shot on a HiDPI docs page.
        context = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 900, "height": 720},
            device_scale_factor=2,
        )
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            # The composer renders only once the bundle has a live registered session, so
            # its presence is the "page is ready, cookie is set" barrier.
            page.wait_for_selector(".tcw-composer", timeout=20000)

            token = next((c.get("value") for c in context.cookies() if c.get("name") == SESSION_COOKIE), None)
            if token is None:
                raise RuntimeError(f"the chat page minted no {SESSION_COOKIE} cookie")
            visitor_id = registered_visitor_id(stack.resources.redis_url, token)
            recipient = f"{identity}:{visitor_id}"

            notify_url = f"http://{stack.host}:{stack.port_a}/api/notifications"
            body = {
                "message": "Your request is being processed.",
                "channel": "web",
                "recipient": recipient,
                "media": [{"kind": "image", "url": image_url, "caption": _CAPTION}],
                "options": _OPTIONS,
            }
            response = httpx.post(notify_url, json=body, timeout=15.0)
            if response.status_code != 200:
                raise RuntimeError(f"notify door refused (HTTP {response.status_code}): {response.text[:500]}")

            # The card, its image actually loaded, and both option chips present, before the shot.
            page.wait_for_selector(".tcw-media", timeout=20000)
            page.wait_for_function(
                "() => { const i = document.querySelector('.tcw-media-image');"
                " return !!i && i.complete && i.naturalWidth > 0; }",
                timeout=20000,
            )
            page.wait_for_function(
                f"() => document.querySelectorAll('.tcw-media-options button').length >= {len(_OPTIONS)}",
                timeout=20000,
            )

            card = page.query_selector(".tcw-media")
            if card is None:
                raise RuntimeError("the media card element (.tcw-media) never appeared")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            card.screenshot(path=str(out_path))
        finally:
            context.close()
            browser.close()


@contextlib.contextmanager
def _web_media_stack(infra: Infra, root: Path) -> Iterator[tuple[TaiStack, str]]:
    """Boot the web-media stack and yield ``(stack, root_token)``, torn down leak-free at exit.

    The web channel's upload + message doors, the redis conversations backend a bridged turn accepts
    into, and storage-local as the blob provider.
    Unlike the channel stack the media-card capture uses, this one can ingest an upload and bridge a
    message that references it, so the composer's real upload + send drive the inbound-attachment
    captures end to end. twilio/whatsapp ride their in-process stubs to satisfy the profile.
    """
    fake_twilio = FakeTwilio()
    fake_whatsapp = FakeWhatsApp()
    llm = LlmStub()
    fake_twilio.start()
    fake_whatsapp.start()
    llm.start()
    resource_kwargs = {
        "llm_base_url": llm.base_url,
        "twilio_api_base_url": fake_twilio.api_base_url,
        "whatsapp_api_base_url": fake_whatsapp.api_base_url,
    }
    try:
        resources, config = allocate_and_build(infra, root, build_web_media_stack, resource_kwargs, False)
        stack = TaiStack(config, infra, resources, root)
        try:
            root_token = seed_bridge_authz(infra, resources)
        except BaseException:
            stack.teardown()
            raise
        # The access-controlled stack drains its boot reload gate through the MCP probe, which the
        # token must carry.
        stack.auth_token = root_token
        with stack, diagnostics.track(stack):
            yield stack, root_token
    finally:
        llm.stop()
        fake_whatsapp.stop()
        fake_twilio.stop()


def _create_capture_web_route(stack: TaiStack, root_token: str) -> str:
    """Mint an execution key and a tool-target web route on a fresh identity, returning that identity.

    The visitor's send bridges to this route; the tool target dispatches deterministically (no LLM),
    replying ``ok`` — the capture reads only the visitor's own inbound frame, not the reply.
    """
    suffix = secrets.token_hex(4)
    identity = f"capture-web-{suffix}"
    route_name = f"capture-route-{suffix}"
    exec_key = f"capture-exec-{suffix}"
    admin = f"http://{stack.host}:{stack.port_b}"
    headers = {"Authorization": f"Bearer {root_token}"}
    minted = httpx.post(
        f"{admin}/api/auth/api-keys",
        headers=headers,
        json={"user_id": exec_key, "description": "capture web route", "scopes": ["e2e-all"]},
        timeout=15.0,
    )
    if minted.status_code != 200:
        raise RuntimeError(f"minting the capture execution key failed (HTTP {minted.status_code}): {minted.text[:500]}")
    route = httpx.post(
        f"{admin}/api/conversations/{route_name}",
        headers=headers,
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": "e2e_record",
            "execution_key": exec_key,
            "channel": "web",
            "our_identity": identity,
            "start_expr": {"content": '{key: "capture", value: "noop"}'},
            "reply_expr": {"content": '"ok"'},
        },
        timeout=15.0,
    )
    if route.status_code != 200:
        raise RuntimeError(f"creating the capture web route failed (HTTP {route.status_code}): {route.text[:500]}")
    return identity


def _settle(page: Page) -> None:
    """Wait for every running CSS transition and animation on the page to finish.

    A shot taken mid-transition (a control changing colour as it enables, a row fading
    in) captures an intermediate frame and makes the image nondeterministic.
    """
    # ``allSettled``: an animation on an element that left the DOM meanwhile (the optimistic
    # row the durable frame replaced) is cancelled and its promise rejects; a looping
    # animation never finishes and is skipped.
    page.evaluate(
        "() => Promise.allSettled(document.getAnimations()"
        ".filter((a) => a.effect === null || a.effect.getTiming().iterations !== Infinity)"
        ".map((a) => a.finished))"
    )


def _capture_composer_tray(
    stack: TaiStack, identity: str, out_path: Path, *, theme: Literal["light", "dark"], headed: bool
) -> None:
    """Screenshot the composer with one ready attachment in its tray — the pending-attachment state.

    Opens the visitor page, attaches one image through the composer's file input, and shoots the whole
    composer (the tray above the attach button, the input and the send control) once the item uploads
    to ``ready``.
    """
    page_url = f"http://{stack.host}:{stack.port_b}/api/channels/web/chat/{identity}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(color_scheme=theme, viewport={"width": 900, "height": 720}, device_scale_factor=2)
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            page.wait_for_selector(".tcw-composer", timeout=20000)
            page.set_input_files(
                'input[type="file"]',
                files=[{"name": _INBOUND_IMAGE_NAME, "mimeType": "image/png", "buffer": TINY_PNG}],
            )
            # The item uploads through the real door; its tray card flips to ready when the upload returns.
            page.wait_for_selector('[data-testid="attach-item"][data-status="ready"]', timeout=20000)
            # A ready attachment enables the send control; wait for that state and for its
            # colour transition to end before shooting.
            page.wait_for_selector("button.tcw-send:not([disabled])", timeout=20000)
            _settle(page)
            composer = page.query_selector(".tcw-composer")
            if composer is None:
                raise RuntimeError("the composer element (.tcw-composer) never appeared")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            composer.screenshot(path=str(out_path))
        finally:
            context.close()
            browser.close()


def _capture_inbound_row(
    stack: TaiStack, identity: str, out_path: Path, *, theme: Literal["light", "dark"], headed: bool
) -> None:
    """Screenshot the inbound transcript row carrying an image and a document.

    Opens the visitor page, attaches an image AND a document through the composer, sends, and shoots
    the inbound row once it renders with the image loaded and the doc card present.
    """
    page_url = f"http://{stack.host}:{stack.port_b}/api/channels/web/chat/{identity}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(color_scheme=theme, viewport={"width": 900, "height": 720}, device_scale_factor=2)
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            page.wait_for_selector(".tcw-composer", timeout=20000)
            page.set_input_files(
                'input[type="file"]',
                files=[
                    {"name": _INBOUND_IMAGE_NAME, "mimeType": "image/png", "buffer": TINY_PNG},
                    {"name": _INBOUND_DOC_NAME, "mimeType": "application/pdf", "buffer": TINY_PDF},
                ],
            )
            page.wait_for_function(
                '() => document.querySelectorAll(\'[data-testid="attach-item"][data-status="ready"]\').length >= 2',
                timeout=20000,
            )
            # Send with no caption: the message rides only its attachments and renders as a media-only
            # inbound row.
            page.click("button.tcw-send")
            page.wait_for_selector(".tcw-media-in", timeout=20000)
            # The served image has actually loaded and the document card is present before the shot.
            # The DURABLE row, not the optimistic one: the visitor's own frame replaces the
            # optimistic bubble (whose image is the client data: preview) with the served
            # reference, so the shot waits for an image sourced from the served route.
            page.wait_for_function(
                "() => { const i = document.querySelector('.tcw-media-in .tcw-media-image');"
                " return !!i && i.src.includes('/api/interactions/media/') && i.complete && i.naturalWidth > 0; }",
                timeout=20000,
            )
            page.wait_for_selector(".tcw-media-in .tcw-doc-card", timeout=20000)
            _settle(page)
            row = page.query_selector(".tcw-media-in")
            if row is None:
                raise RuntimeError("the inbound media row (.tcw-media-in) never appeared")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            row.screenshot(path=str(out_path))
        finally:
            context.close()
            browser.close()


def _capture_inbound_attachments(infra: Infra, root: Path, *, headed: bool) -> list[Path]:
    """Capture the composer tray + the inbound transcript row in both themes; return the PNG paths.

    Boots the web-media stack once and drives the composer for each theme.
    """
    written: list[Path] = []
    with _web_media_stack(infra, root) as (stack, root_token):
        identity = _create_capture_web_route(stack, root_token)
        for theme in _THEMES:
            tray_out = _DOCS_IMAGES / f"inbound-attachment-tray-{theme}.png"
            _capture_composer_tray(stack, identity, tray_out, theme=theme, headed=headed)
            written.append(tray_out)
            row_out = _DOCS_IMAGES / f"inbound-attachment-row-{theme}.png"
            _capture_inbound_row(stack, identity, row_out, theme=theme, headed=headed)
            written.append(row_out)
    return written


def main(argv: list[str] | None = None) -> int:
    """Capture the media-card screenshot to a PNG, returning the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help="Where to write the captured PNG (default: the plugin's docs/images/media-card.png).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the browser headed (for local debugging); headless by default.",
    )
    args = parser.parse_args(argv)

    if not _PLACEHOLDER.is_file():
        raise RuntimeError(f"placeholder image missing at {_PLACEHOLDER}")
    image_bytes = _PLACEHOLDER.read_bytes()

    settings = HarnessSettings()
    infra = connect_infra(settings)
    tmp_root = Path(tempfile.mkdtemp(prefix="channel-web-media-card-"))
    tmp_root_inbound = Path(tempfile.mkdtemp(prefix="channel-web-inbound-media-"))
    inbound_paths: list[Path] = []
    try:
        with _https_image_server(image_bytes) as image_url, _channel_stack(infra, tmp_root) as stack:
            _capture(stack, image_url, args.out, headed=args.headed)
        inbound_paths = _capture_inbound_attachments(infra, tmp_root_inbound, headed=args.headed)
    finally:
        infra.redis.close()
        if infra.checkpoint_redis is not None:
            infra.checkpoint_redis.close()
        shutil.rmtree(tmp_root, ignore_errors=True)
        shutil.rmtree(tmp_root_inbound, ignore_errors=True)

    size = args.out.stat().st_size
    print(f"captured media card -> {args.out} ({size} bytes)")
    for path in inbound_paths:
        print(f"captured inbound attachment -> {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
