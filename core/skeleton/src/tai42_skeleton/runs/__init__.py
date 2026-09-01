"""The platform-side runs index — one enumerable row per run, persisted in the
skeleton's own Postgres so a deployment can list its runs WITHOUT the observability
vendor.

A "run" is one OUTERMOST registered-preset dispatch: the ``run_tool`` chokepoint
(:mod:`tai42_skeleton.runs.chokepoint`) writes exactly one row per such dispatch,
aligning a row with a monitoring trace. Nested sub-preset dispatches and raw
(non-preset) tool calls are deliberately NOT rows. Node-level detail stays in the
conversation checkpoints and the observability vendor — this package is enumeration
only: the store (:mod:`tai42_skeleton.runs.store`), the read/prune operations
(:mod:`tai42_skeleton.operations.runs`), and the HTTP surface
(:mod:`tai42_skeleton.routers.runs`).
"""
