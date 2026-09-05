"""Capture the maintained docs screenshot of the channel-web media card.

Path A (maintained): the channel-web doc page pulls its images from the PLUGIN's own
``docs/images/`` tree, so this capture writes a committed plugin asset —
``src/tai42_channel_web/docs/images/media-card.png`` — rather than a one-off. The
recapture workflow (``.github/workflows/channel-web-media-card-screenshot.yml``) runs
this script and opens a PR with the refreshed PNG whenever the media-card UI source
changes.

What it does, all through the plugin's own public doors and the proven e2e harness:

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
import shutil
import ssl
import tempfile
import threading
from pathlib import Path

import httpx
import trustme
from playwright.sync_api import sync_playwright
from tai42_e2e import diagnostics, manifests
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.harness import connect_infra
from tai42_e2e.manifests import build_channel_stack
from tai42_e2e.netfixtures import FakeSlack, FakeTelegram, FakeTwilio
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import Infra, TaiStack
from tai42_e2e.webchat import SESSION_COOKIE, registered_visitor_id

# The monorepo root, four levels up from this file (plugins/channel-web/scripts/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLACEHOLDER = Path(__file__).resolve().parent / "assets" / "placeholder.png"
_DEFAULT_OUT = (
    _REPO_ROOT / "plugins" / "channel-web" / "src" / "tai42_channel_web" / "docs" / "images" / "media-card.png"
)

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

    def log_message(self, format: str, *args: object) -> None:
        # Quiet: the capture's own prints are the only output that matters.
        return


@contextlib.contextmanager
def _https_image_server(image_bytes: bytes):
    """Start a local https server issuing a self-signed cert for 127.0.0.1 that serves
    ``image_bytes`` as a PNG, yielding its ``https://127.0.0.1:<port>/placeholder.png``
    URL. The browser fetches it with ``ignore_https_errors`` set; the server under test
    never dials it."""
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
    """Boot the channel-web-carrying stack (``build_channel_stack``) and yield it, torn
    down leak-free at block exit. The web channel has no vendor, but the profile also loads
    the telegram/slack/twilio plugins, so their in-process recording stubs stand in for the
    outbound API base URLs the profile points at."""
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


def main(argv: list[str] | None = None) -> int:
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
    try:
        with _https_image_server(image_bytes) as image_url, _channel_stack(infra, tmp_root) as stack:
            _capture(stack, image_url, args.out, headed=args.headed)
    finally:
        infra.redis.close()
        if infra.checkpoint_redis is not None:
            infra.checkpoint_redis.close()
        shutil.rmtree(tmp_root, ignore_errors=True)

    size = args.out.stat().st_size
    print(f"captured media card -> {args.out} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
