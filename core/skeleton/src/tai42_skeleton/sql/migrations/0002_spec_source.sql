-- Widen the marketplace attribution `source` CHECK to admit a descriptor-only
-- plugin's `spec` source alongside `pypi`/`github`.
--
-- A descriptor-only plugin (a `PluginSpec` whose every item is a data item and
-- which names no `package`) is resolved from a `source == 'spec'` pointer
-- (repository_url, tag, artifact_ref, sha256 of the parsed `tai-plugin.yml`) and
-- installs nothing but its manifest entry. The 0001 baseline pinned
-- `source IN ('pypi', 'github')`, so recording such an install would violate the
-- constraint; drop and re-add it with the `spec` value admitted. The existing
-- `repository_url`/`tag`/`artifact_ref`/`sha256` columns already carry a `spec`
-- pin's provenance exactly as they carry a github pin's — no other column change.
ALTER TABLE marketplace_installs
    DROP CONSTRAINT IF EXISTS marketplace_installs_source_check;

ALTER TABLE marketplace_installs
    ADD CONSTRAINT marketplace_installs_source_check
    CHECK (source IN ('pypi', 'github', 'spec'));
