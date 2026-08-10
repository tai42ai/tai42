def build_url(base: str, route: str) -> str:
    parts = "/".join(part for part in route.split("/") if part)
    base = base.rstrip("/")
    if not parts:
        # An empty or slash-only route ("" / "/") yields the base with a single
        # trailing slash, never a doubled "//".
        return f"{base}/"
    suffix = "/" if route.endswith("/") else ""
    return f"{base}/{parts}{suffix}"
