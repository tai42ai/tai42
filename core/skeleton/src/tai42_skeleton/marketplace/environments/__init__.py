"""The isolated plugin environments under ``TAI_PLUGINS_DIR``.

Every packaged plugin runs from an environment built from the install store and never
patched in place. The in-process plugins share ONE environment, the plugin layer, resolved
in one pip call against the image's pinned distributions and put at the end of the serving
interpreter's ``sys.path``; every ``mcp-server`` package gets its own virtual environment,
launched as a separate process. Each environment is built into a fresh directory, made
current by an atomic symlink swap, and its older builds are deleted after the change commits.

Its pieces, one concern each:

- :mod:`.settings` — the ``TAI_PLUGINS_DIR`` setting and its refusals.
- :mod:`.image` — the image environment's distributions and the constraints that pin them.
- :mod:`.layout` — the directory layout, build ids and the ``tai42-build.json`` record.
- :mod:`.targets` — which environment each install-store row belongs to.
- :mod:`.consistency` — whether a layer can sit beside the image.
- :mod:`.build` — the layer and server builders and their post-build checks.
- :mod:`.swap` — committing a build, the ``current`` swap, swap-back and pruning.
- :mod:`.findings` — how an inconsistency of the plugins directory is worded.
- :mod:`.current_record` — reading a current build's record, or the finding when it does not read.
- :mod:`.verify` — the consistency checks of the current environments against the store.
- :mod:`.console_scripts` — the console scripts a server build declares for its own package.
- :mod:`.activate` — making the current layer and server commands visible to this process.
- :mod:`.current_builds` — the distributions the current environments hold, read import-free.
- :mod:`.boot_verification` — verifying the environments against the store once per process.
- :mod:`.change` — one marketplace operation's builds, swaps, swap-backs and prunes.
- :mod:`.rebuild` — rebuilding the environments that fail verification.
"""

from __future__ import annotations
