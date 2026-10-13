:robot: I have created a release *beep* *boop*
---


<details><summary>tai42-contract: 17.0.0</summary>

## [17.0.0](https://github.com/tai42ai/tai42/compare/tai42-contract-v16.2.0...tai42-contract-v17.0.0) (2026-10-13)


###   BREAKING CHANGES

* the inbound MCP door: protected-resource metadata, the challenge and the request resource
* the published response schemas declare enums for the closed fields; the fold operation's mode parameter is typed FoldMode; TranscriptOrder moves to tai42_skeleton.conversations.record_pages, and tai42_skeleton.operations.conversations.models no longer exports TRANSCRIPT_ORDERS.
* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.
* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* OperationError and UnavailableError move from tai42_skeleton.operations to tai42_contract.errors; tai42_skeleton.template ContentPart and ResourceManager.normalize_media are replaced by tai42_contract.template.ImageReference and resolve_image; StructuredOutputPlan.bound_typed_dict is removed; LeadingUserMiddleware no longer has before_model/abefore_model; roll_cache_marks requires mark_key; RetrievalToolsGraph requires a provider; default_ttl_seconds is removed from SandboxDispatchSettings, DockerSandboxSettings and SandboxLocalSettings, and a stale SANDBOX_<PROVIDER>_DEFAULT_TTL_SECONDS refuses boot; tai42_e2e.settings.LLM_PROVIDERS changes.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* tai42_contract.agent.SubAgentSpec and tai42_skeleton.agent.SubAgentSpec are removed (the agents plugin keeps its own); PresetSpec.name must match PRESET_NAME_PATTERN; the suspended-interaction marker moves from tai42_contract.interactions.continuation to park_markers, and suspended_interaction_marker requires resume_owner and the keyword-only interaction_ids and caller_interaction_ids; reserved_thread_namespace_error takes the agent class first; CLIENT_TOOL_NAME_MAX_LEN is removed (use TOOL_NAME_MAX_LEN); the agents' run and astream no longer accept free keyword arguments, and claude_code's build_options_payload and build_payload take proxy_tools in place of tool_names; the contract's served documents change.
* **skeleton:** a configuration whose fleet apply window is below three times the tool-runs drain budget refuses to boot, reload or apply (ApplyWindowBelowDrainError); the default apply window is 90 seconds; translate_backend_needs_bus is renamed translate_config_invariants.
* the AppAdmin protocol gains reprobe_failed_mcps(titles); tai42_e2e.topology.StackResources takes langfuse_host, langfuse_public_key and langfuse_secret_key at new positions; pass them by keyword.
* ProviderDescriptor.icon_url and ProviderCatalogEntry.icon_url widen from str to str | None; the ConnectionService.complete_connect Protocol takes code: str | None and the new iss and error keywords, so an external implementer of the Protocol must add them; tai42_skeleton.connectors.oauth.client build_authorize_url, exchange_code, refresh and revoke take the required resolved and client arguments.
* resolve_trace_context and bind_run_trace raise InvalidRunTraceError for an explicit monitoring_trace_id that is not 32 lowercase hex (not all zeros) or a monitoring_parent_span_id that is not 16 lowercase hex (not all zeros).
* **skeleton:** DELETE /api/auth/principals/{id} of a principal whose login record a provider keeps answers 409 naming that provider's member actions; GET /api/auth/members joins each principal's role (principals[].role) and derives the row role from them. In the contract, MemberEntry.role is removed and MemberPrincipalState.role is required.
* an env write of a recycle-class key is refused on a bare deployment and rolls a fleet recycle on a supervised one, on every env write door; after a converged roll the serving worker exits (drains and is respawned) after a projected tool call too (MCP tools/call, tai tools run, an agent, a preset, a schedule, a hook), and a backend worker exits and is respawned after a job that wrote a recycle-class env key; write_env, set_mcp_secret_env, import_backup, marketplace_install, marketplace_update and marketplace_uninstall return an OperationResponse instead of a plain payload; tai42_skeleton.app.graceful_exit replaces request_serve_graceful_exit, request_backend_graceful_exit and graceful_exit_for with request_graceful_exit(pid); ConfigService.apply_replace_env no longer takes orchestrate; and tai42_skeleton.marketplace.manifest_apply.reload_report is removed (the installer hands its ApplyResult to the operation, which renders it).
* a profile apply reports a stopped recycle roll; the working signal keeps its cadence ([#549](https://github.com/tai42ai/tai42/issues/549))
* an e2e leg proves a door binding save resolves its template references in one batch ([#548](https://github.com/tai42ai/tai42/issues/548))
* one batch template-reference resolver on the states facet; the binding saves use it ([#541](https://github.com/tai42ai/tai42/issues/541))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* the accounts archive is version 2 and refuses version 1; the accounts plugin drops its role column, guard and lock; the contract gains the admin services.
* the readiness and drain registries live on the app instance; the connector token store's listing and restore seams replace the SQL that backup and re-encrypt carried; the kit's pinning transport constructor changes.
* the states facet and the pending-saves routes change shape; the PUT state answer carries held.
* the trace-detail wire loses type and usage and gains kind and the token fields; an unknown LANGFUSE_ variable refuses boot.
* the two access-control path-list settings default to empty and authenticated_always_allowed_paths is removed.
* the states facet gains the two resolver methods and the engine no longer resolves names itself.
* the contract's access-control surface drops the unused PolicyEnforcer protocol; route rows change shape.
* the policy enforcer is served by the accessor, not constructed per call; the tool-surface generation is part of the tools facet.
* the park records and the resolution record change shape; the LLM_PROVIDER_CHECKPOINT_TTL_MINUTES and FLOW_CHECKPOINT_RETENTION_DAYS variables are refused.
* the contract's template models are typed and the states facet's render methods change shape; the declarations and templates tables take their version from a sequence; the closed-unit error is typed.
* INTERACTIONS_NOTIFICATIONS_* and INTERACTIONS_SEND_RECEIPT_* move to CHANNELS_*; the config env door body is {env, secret_keys}; the recycle constants are removed in favour of TAI_SUPERVISED_PINNED_KEYS; an unknown name under an owned prefix refuses boot.
* the monitoring writer protocol and the recorded shapes change; the plugin's SDK-provided settings names are refused; the collector credential is OTEL_COLLECTOR_LANGFUSE_AUTH.
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* a transactional state outbox, the after-reply call, pending-save doors and card ([#531](https://github.com/tai42ai/tai42/issues/531)) ([e9af951](https://github.com/tai42ai/tai42/commit/e9af951ea7dd70b8ce9f031aac088a50389ad4f1))
* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* connectors own their token rows; readiness and drain registries per app; public seams ([#533](https://github.com/tai42ai/tai42/issues/533)) ([1bd4cd1](https://github.com/tai42ai/tai42/commit/1bd4cd1ada2b50977850483eac1908b1c0b5c7e1))
* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
* monitoring over OpenTelemetry with records stored once and exported through a collector ([#520](https://github.com/tai42ai/tai42/issues/520)) ([ab2000e](https://github.com/tai42ai/tai42/commit/ab2000e7f3f8f30046164e8460546a24b082191f))
* monitoring read side: span kinds and tokens, GenAI messages, references resolved on request ([#529](https://github.com/tai42ai/tai42/issues/529)) ([8ea9e65](https://github.com/tai42ai/tai42/commit/8ea9e650dbfc2114349e64c4c5043906d602aeeb))
* one batch template-reference resolver on the states facet; the binding saves use it ([#541](https://github.com/tai42ai/tai42/issues/541)) ([a5663b3](https://github.com/tai42ai/tai42/commit/a5663b36a63224cf5259ec969b61ebb7df97ae27))
* one canonical route path on every door, per-writer policy versions, SSE bare-prefix record ([#525](https://github.com/tai42ai/tai42/issues/525)) ([25f713b](https://github.com/tai42ai/tai42/commit/25f713b9d63b2346ffe33408105b09efcfed64f5))
* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* one guarded admin service set, the access-control backup section, throttles, providers ([#535](https://github.com/tai42ai/tai42/issues/535)) ([517048b](https://github.com/tai42ai/tai42/commit/517048b5785671777c3e68c969deeb6ea50ac183))
* one policy enforcer per loop, route/config snapshots, tool-surface generation, graph cache ([#526](https://github.com/tai42ai/tai42/issues/526)) ([58934b2](https://github.com/tai42ai/tai42/commit/58934b2010fc047b60012bec310b9dd2e06e5d4f))
* one subject resolver and one template-reference resolver on the states facet, on every door ([#527](https://github.com/tai42ai/tai42/issues/527)) ([d5939b1](https://github.com/tai42ai/tai42/commit/d5939b15c0c3585ae1318e68676789b3de730c7d))
* owned env prefixes, channel settings, one owner for secret marks and env markers, pinned keys ([#521](https://github.com/tai42ai/tai42/issues/521)) ([5c5c046](https://github.com/tai42ai/tai42/commit/5c5c04649afdb0866981127ffe0c0424b04af463))
* park index with token-guarded finalize, chain lineage on every resume door, failed terminal ([#524](https://github.com/tai42ai/tai42/issues/524)) ([e59ec7b](https://github.com/tai42ai/tai42/commit/e59ec7b15d868079e6e04b15c9550cd6665262c0))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* routes declare their reach; the server serves the sub-MCP mount, pattern and public id ([#528](https://github.com/tai42ai/tai42/issues/528)) ([1accaf5](https://github.com/tai42ai/tai42/commit/1accaf55e1c312216e13dd275c0661d46bb7ae86))
* **skeleton:** a member's role is joined per principal; a login-owning provider owns the delete ([26a14e9](https://github.com/tai42ai/tai42/commit/26a14e9008dde22a883fefed9017362d3a88d11f))
* **skeleton:** the fleet apply window honours the retire's drain budget ([35a3fd4](https://github.com/tai42ai/tai42/commit/35a3fd476bde0a731892a413c50089893517feed))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* structured output binds the authored schema; provider-keyed history; the sandbox seam ([f2675f8](https://github.com/tai42ai/tai42/commit/f2675f8511fc14a5dcda4b9a63eaa6a85a1949c0))
* the add-server door: discover, add and remove a discovered MCP server and set its client ([d308b40](https://github.com/tai42ai/tai42/commit/d308b40bcf6fe1106fac9f1dbf8ffcabb51758d1))
* the agent surface is framework-neutral: client tools, one run signature, declared parks ([37578fc](https://github.com/tai42ai/tai42/commit/37578fc51dea9232ae4b2bcc095dce3103ba1b72))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))
* the connector discovery seam: discovered providers, one resolver on every OAuth door ([34578ef](https://github.com/tai42ai/tai42/commit/34578ef0abc208624d0ac7cc5572225681c7384c))
* the inbound MCP door: protected-resource metadata, the challenge and the request resource ([5aeb9bc](https://github.com/tai42ai/tai42/commit/5aeb9bcd24a454327e88d0464d2a2a26c5e1fad4))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))
* the skeleton publishes its OpenAPI artifact; the contract serves its constants ([6344834](https://github.com/tai42ai/tai42/commit/6344834a7a2a03d9e8773c50cb2b62853be908a9))
* typed state templates, version-keyed catalogs, a compare-and-set unit commit ([#523](https://github.com/tai42ai/tai42/issues/523)) ([51f7aa6](https://github.com/tai42ai/tai42/commit/51f7aa6656b23735d8c364b7e3fef2265470ae2e))


### Bug Fixes

* a profile apply reports a stopped recycle roll; the working signal keeps its cadence ([#549](https://github.com/tai42ai/tai42/issues/549)) ([ac2e8c2](https://github.com/tai42ai/tai42/commit/ac2e8c29289607141dd06702c18f4f46597b0f36))
* an e2e leg proves a door binding save resolves its template references in one batch ([#548](https://github.com/tai42ai/tai42/issues/548)) ([6ada66c](https://github.com/tai42ai/tai42/commit/6ada66c563318655110ce34b945c6e170049662a))
* recycle-class env changes respawn through every door and judge the value a worker bound ([efc13d0](https://github.com/tai42ai/tai42/commit/efc13d0f3e99d95db8bba7500ee3f10c517e97b8))
* refuse a run whose explicit trace id or parent anchor no writer can record under ([62c9265](https://github.com/tai42ai/tai42/commit/62c92653674f92c626c2180630712a5be5a50bd1))
</details>

<details><summary>tai42-kit: 8.0.0</summary>

## [8.0.0](https://github.com/tai42ai/tai42/compare/tai42-kit-v7.7.3...tai42-kit-v8.0.0) (2026-10-13)


###   BREAKING CHANGES

* the inbound MCP door: protected-resource metadata, the challenge and the request resource
* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* OperationError and UnavailableError move from tai42_skeleton.operations to tai42_contract.errors; tai42_skeleton.template ContentPart and ResourceManager.normalize_media are replaced by tai42_contract.template.ImageReference and resolve_image; StructuredOutputPlan.bound_typed_dict is removed; LeadingUserMiddleware no longer has before_model/abefore_model; roll_cache_marks requires mark_key; RetrievalToolsGraph requires a provider; default_ttl_seconds is removed from SandboxDispatchSettings, DockerSandboxSettings and SandboxLocalSettings, and a stale SANDBOX_<PROVIDER>_DEFAULT_TTL_SECONDS refuses boot; tai42_e2e.settings.LLM_PROVIDERS changes.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* tai42_contract.agent.SubAgentSpec and tai42_skeleton.agent.SubAgentSpec are removed (the agents plugin keeps its own); PresetSpec.name must match PRESET_NAME_PATTERN; the suspended-interaction marker moves from tai42_contract.interactions.continuation to park_markers, and suspended_interaction_marker requires resume_owner and the keyword-only interaction_ids and caller_interaction_ids; reserved_thread_namespace_error takes the agent class first; CLIENT_TOOL_NAME_MAX_LEN is removed (use TOOL_NAME_MAX_LEN); the agents' run and astream no longer accept free keyword arguments, and claude_code's build_options_payload and build_payload take proxy_tools in place of tool_names; the contract's served documents change.
* **kit:** a cancellation requested during a pooled Postgres checkout reaches its task
* ProviderDescriptor.icon_url and ProviderCatalogEntry.icon_url widen from str to str | None; the ConnectionService.complete_connect Protocol takes code: str | None and the new iss and error keywords, so an external implementer of the Protocol must add them; tai42_skeleton.connectors.oauth.client build_authorize_url, exchange_code, refresh and revoke take the required resolved and client arguments.
* resolve_trace_context and bind_run_trace raise InvalidRunTraceError for an explicit monitoring_trace_id that is not 32 lowercase hex (not all zeros) or a monitoring_parent_span_id that is not 16 lowercase hex (not all zeros).
* an env write of a recycle-class key is refused on a bare deployment and rolls a fleet recycle on a supervised one, on every env write door; after a converged roll the serving worker exits (drains and is respawned) after a projected tool call too (MCP tools/call, tai tools run, an agent, a preset, a schedule, a hook), and a backend worker exits and is respawned after a job that wrote a recycle-class env key; write_env, set_mcp_secret_env, import_backup, marketplace_install, marketplace_update and marketplace_uninstall return an OperationResponse instead of a plain payload; tai42_skeleton.app.graceful_exit replaces request_serve_graceful_exit, request_backend_graceful_exit and graceful_exit_for with request_graceful_exit(pid); ConfigService.apply_replace_env no longer takes orchestrate; and tai42_skeleton.marketplace.manifest_apply.reload_report is removed (the installer hands its ApplyResult to the operation, which renders it).
* a reload answers its caller on every transport; the bus heartbeat survives the rebuild freeze ([#551](https://github.com/tai42ai/tai42/issues/551))
* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545))
* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544))
* the Redis checkpoint codec keeps a datetime's zone and fold; store format generation 3 ([#543](https://github.com/tai42ai/tai42/issues/543))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* storage-local has no default root; the kit logging registries replace the per-feature hooks; the curl client's pool-key parameter is renamed.
* the readiness and drain registries live on the app instance; the connector token store's listing and restore seams replace the SQL that backup and re-encrypt carried; the kit's pinning transport constructor changes.
* the trace-detail wire loses type and usage and gains kind and the token fields; an unknown LANGFUSE_ variable refuses boot.
* the park records and the resolution record change shape; the LLM_PROVIDER_CHECKPOINT_TTL_MINUTES and FLOW_CHECKPOINT_RETENTION_DAYS variables are refused.
* the engine's checkpoint TTL setting is removed in favour of the platform retention setting; stored checkpoints in the previous format are refused at boot and must be reset; the kit declares redisvl in its llm extra.
* INTERACTIONS_NOTIFICATIONS_* and INTERACTIONS_SEND_RECEIPT_* move to CHANNELS_*; the config env door body is {env, secret_keys}; the recycle constants are removed in favour of TAI_SUPERVISED_PINNED_KEYS; an unknown name under an owned prefix refuses boot.
* the monitoring writer protocol and the recorded shapes change; the plugin's SDK-provided settings names are refused; the collector credential is OTEL_COLLECTOR_LANGFUSE_AUTH.
* **kit:** a settings class that sets model_config["env_file"] is refused; get_mcp_transport returns the keep-alive transport type.
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545)) ([77ea6e2](https://github.com/tai42ai/tai42/commit/77ea6e2898d2c8d743920cb4567a61e4748e16fc))
* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* connectors own their token rows; readiness and drain registries per app; public seams ([#533](https://github.com/tai42ai/tai42/issues/533)) ([1bd4cd1](https://github.com/tai42ai/tai42/commit/1bd4cd1ada2b50977850483eac1908b1c0b5c7e1))
* content-agnostic storage, kit-owned log redaction, one error-metadata span builder ([#534](https://github.com/tai42ai/tai42/issues/534)) ([71d93e8](https://github.com/tai42ai/tai42/commit/71d93e833e95df0a1d034681c8550acfcbd11374))
* **kit:** env-file cache, keyed settings, orjson jsonb reads, MCP keep-alive, read_connection ([#516](https://github.com/tai42ai/tai42/issues/516)) ([886da9e](https://github.com/tai42ai/tai42/commit/886da9edd901fd8456212abb320ccea8da0fa2a3))
* monitoring over OpenTelemetry with records stored once and exported through a collector ([#520](https://github.com/tai42ai/tai42/issues/520)) ([ab2000e](https://github.com/tai42ai/tai42/commit/ab2000e7f3f8f30046164e8460546a24b082191f))
* monitoring read side: span kinds and tokens, GenAI messages, references resolved on request ([#529](https://github.com/tai42ai/tai42/issues/529)) ([8ea9e65](https://github.com/tai42ai/tai42/commit/8ea9e650dbfc2114349e64c4c5043906d602aeeb))
* one checkpoint retention setting, faithful Redis codec, durability mode, stale-thread sweep ([#519](https://github.com/tai42ai/tai42/issues/519)) ([b261cea](https://github.com/tai42ai/tai42/commit/b261cea384f09450c548c96b0af3efa025d65fe1))
* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* owned env prefixes, channel settings, one owner for secret marks and env markers, pinned keys ([#521](https://github.com/tai42ai/tai42/issues/521)) ([5c5c046](https://github.com/tai42ai/tai42/commit/5c5c04649afdb0866981127ffe0c0424b04af463))
* park index with token-guarded finalize, chain lineage on every resume door, failed terminal ([#524](https://github.com/tai42ai/tai42/issues/524)) ([e59ec7b](https://github.com/tai42ai/tai42/commit/e59ec7b15d868079e6e04b15c9550cd6665262c0))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* structured output binds the authored schema; provider-keyed history; the sandbox seam ([f2675f8](https://github.com/tai42ai/tai42/commit/f2675f8511fc14a5dcda4b9a63eaa6a85a1949c0))
* the agent surface is framework-neutral: client tools, one run signature, declared parks ([37578fc](https://github.com/tai42ai/tai42/commit/37578fc51dea9232ae4b2bcc095dce3103ba1b72))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))
* the connector discovery seam: discovered providers, one resolver on every OAuth door ([34578ef](https://github.com/tai42ai/tai42/commit/34578ef0abc208624d0ac7cc5572225681c7384c))
* the inbound MCP door: protected-resource metadata, the challenge and the request resource ([5aeb9bc](https://github.com/tai42ai/tai42/commit/5aeb9bcd24a454327e88d0464d2a2a26c5e1fad4))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* a reload answers its caller on every transport; the bus heartbeat survives the rebuild freeze ([#551](https://github.com/tai42ai/tai42/issues/551)) ([d2e24ac](https://github.com/tai42ai/tai42/commit/d2e24acefee6444ac87caa1b318f5f6ef43c45bc))
* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544)) ([18b495b](https://github.com/tai42ai/tai42/commit/18b495b0f3992e8b13aa7c2a1e98939caad014d0))
* **kit:** a cancellation requested during a pooled Postgres checkout reaches its task ([25cb046](https://github.com/tai42ai/tai42/commit/25cb04686cd290c451c0f07118ece8564fb8fc79))
* recycle-class env changes respawn through every door and judge the value a worker bound ([efc13d0](https://github.com/tai42ai/tai42/commit/efc13d0f3e99d95db8bba7500ee3f10c517e97b8))
* refuse a run whose explicit trace id or parent anchor no writer can record under ([62c9265](https://github.com/tai42ai/tai42/commit/62c92653674f92c626c2180630712a5be5a50bd1))
* the Redis checkpoint codec keeps a datetime's zone and fold; store format generation 3 ([#543](https://github.com/tai42ai/tai42/issues/543)) ([3fc9f7d](https://github.com/tai42ai/tai42/commit/3fc9f7d35d11cd166b92fbec9291c5fbccf8c5c5))
</details>

<details><summary>tai42-cli: 14.0.0</summary>

## [14.0.0](https://github.com/tai42ai/tai42/compare/tai42-cli-v13.1.1...tai42-cli-v14.0.0) (2026-10-13)


###   BREAKING CHANGES

* the studio plugin listing serves each plugin's route mounts and route panels
* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.
* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* tai42_e2e.settings.REAL_SERVICES gains the connector-slack and connector-github real-service seams.
* the AppAdmin protocol gains reprobe_failed_mcps(titles); tai42_e2e.topology.StackResources takes langfuse_host, langfuse_public_key and langfuse_secret_key at new positions; pass them by keyword.
* tai42_skeleton.connectors.oauth.crypto encrypt, decrypt and decrypt_reporting_key take a keyword aad in place of connection_id (the connection path passes aad=connection_id, so stored bytes are unchanged).
* ProviderDescriptor.icon_url and ProviderCatalogEntry.icon_url widen from str to str | None; the ConnectionService.complete_connect Protocol takes code: str | None and the new iss and error keywords, so an external implementer of the Protocol must add them; tai42_skeleton.connectors.oauth.client build_authorize_url, exchange_code, refresh and revoke take the required resolved and client arguments.
* resolve_trace_context and bind_run_trace raise InvalidRunTraceError for an explicit monitoring_trace_id that is not 32 lowercase hex (not all zeros) or a monitoring_parent_span_id that is not 16 lowercase hex (not all zeros).
* an env write of a recycle-class key is refused on a bare deployment and rolls a fleet recycle on a supervised one, on every env write door; after a converged roll the serving worker exits (drains and is respawned) after a projected tool call too (MCP tools/call, tai tools run, an agent, a preset, a schedule, a hook), and a backend worker exits and is respawned after a job that wrote a recycle-class env key; write_env, set_mcp_secret_env, import_backup, marketplace_install, marketplace_update and marketplace_uninstall return an OperationResponse instead of a plain payload; tai42_skeleton.app.graceful_exit replaces request_serve_graceful_exit, request_backend_graceful_exit and graceful_exit_for with request_graceful_exit(pid); ConfigService.apply_replace_env no longer takes orchestrate; and tai42_skeleton.marketplace.manifest_apply.reload_report is removed (the installer hands its ApplyResult to the operation, which renders it).
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542))
* the readiness and drain registries live on the app instance; the connector token store's listing and restore seams replace the SQL that backup and re-encrypt carried; the kit's pinning transport constructor changes.
* the states facet and the pending-saves routes change shape; the PUT state answer carries held.
* the trace-detail wire loses type and usage and gains kind and the token fields; an unknown LANGFUSE_ variable refuses boot.
* the contract's template models are typed and the states facet's render methods change shape; the declarations and templates tables take their version from a sequence; the closed-unit error is typed.
* the engine's checkpoint TTL setting is removed in favour of the platform retention setting; stored checkpoints in the previous format are refused at boot and must be reset; the kit declares redisvl in its llm extra.
* INTERACTIONS_NOTIFICATIONS_* and INTERACTIONS_SEND_RECEIPT_* move to CHANNELS_*; the config env door body is {env, secret_keys}; the recycle constants are removed in favour of TAI_SUPERVISED_PINNED_KEYS; an unknown name under an owned prefix refuses boot.
* the monitoring writer protocol and the recorded shapes change; the plugin's SDK-provided settings names are refused; the collector credential is OTEL_COLLECTOR_LANGFUSE_AUTH.
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* a transactional state outbox, the after-reply call, pending-save doors and card ([#531](https://github.com/tai42ai/tai42/issues/531)) ([e9af951](https://github.com/tai42ai/tai42/commit/e9af951ea7dd70b8ce9f031aac088a50389ad4f1))
* connectors own their token rows; readiness and drain registries per app; public seams ([#533](https://github.com/tai42ai/tai42/issues/533)) ([1bd4cd1](https://github.com/tai42ai/tai42/commit/1bd4cd1ada2b50977850483eac1908b1c0b5c7e1))
* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
* monitoring over OpenTelemetry with records stored once and exported through a collector ([#520](https://github.com/tai42ai/tai42/issues/520)) ([ab2000e](https://github.com/tai42ai/tai42/commit/ab2000e7f3f8f30046164e8460546a24b082191f))
* monitoring read side: span kinds and tokens, GenAI messages, references resolved on request ([#529](https://github.com/tai42ai/tai42/issues/529)) ([8ea9e65](https://github.com/tai42ai/tai42/commit/8ea9e650dbfc2114349e64c4c5043906d602aeeb))
* mounted MCP tools keep their server's display fields; real legs for the vendor connectors ([ac3105a](https://github.com/tai42ai/tai42/commit/ac3105a2c4771ac42f340b8f1d0dd54cb4c0ae0b))
* one checkpoint retention setting, faithful Redis codec, durability mode, stale-thread sweep ([#519](https://github.com/tai42ai/tai42/issues/519)) ([b261cea](https://github.com/tai42ai/tai42/commit/b261cea384f09450c548c96b0af3efa025d65fe1))
* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* owned env prefixes, channel settings, one owner for secret marks and env markers, pinned keys ([#521](https://github.com/tai42ai/tai42/issues/521)) ([5c5c046](https://github.com/tai42ai/tai42/commit/5c5c04649afdb0866981127ffe0c0424b04af463))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* the add-server door: discover, add and remove a discovered MCP server and set its client ([d308b40](https://github.com/tai42ai/tai42/commit/d308b40bcf6fe1106fac9f1dbf8ffcabb51758d1))
* the connector client store, the client-identity document and dynamic client registration ([74aa542](https://github.com/tai42ai/tai42/commit/74aa542323e5992ef275c78203d777c7fdfc5c06))
* the connector discovery seam: discovered providers, one resolver on every OAuth door ([34578ef](https://github.com/tai42ai/tai42/commit/34578ef0abc208624d0ac7cc5572225681c7384c))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))
* the studio plugin listing serves each plugin's route mounts and route panels ([45392e4](https://github.com/tai42ai/tai42/commit/45392e43b21e2e4d4229b1f823a56f2d2c126e1a))
* typed state templates, version-keyed catalogs, a compare-and-set unit commit ([#523](https://github.com/tai42ai/tai42/issues/523)) ([51f7aa6](https://github.com/tai42ai/tai42/commit/51f7aa6656b23735d8c364b7e3fef2265470ae2e))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542)) ([f00f65c](https://github.com/tai42ai/tai42/commit/f00f65c565736a4b80caa96624b536a0c66b0319))
* recycle-class env changes respawn through every door and judge the value a worker bound ([efc13d0](https://github.com/tai42ai/tai42/commit/efc13d0f3e99d95db8bba7500ee3f10c517e97b8))
* refuse a run whose explicit trace id or parent anchor no writer can record under ([62c9265](https://github.com/tai42ai/tai42/commit/62c92653674f92c626c2180630712a5be5a50bd1))
</details>

<details><summary>tai42-skeleton: 23.0.0</summary>

## [23.0.0](https://github.com/tai42ai/tai42/compare/tai42-skeleton-v22.1.3...tai42-skeleton-v23.0.0) (2026-10-13)


###   BREAKING CHANGES

* the inbound MCP door: protected-resource metadata, the challenge and the request resource
* the studio plugin listing serves each plugin's route mounts and route panels
* the published response schemas declare enums for the closed fields; the fold operation's mode parameter is typed FoldMode; TranscriptOrder moves to tai42_skeleton.conversations.record_pages, and tai42_skeleton.operations.conversations.models no longer exports TRANSCRIPT_ORDERS.
* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.
* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* OperationError and UnavailableError move from tai42_skeleton.operations to tai42_contract.errors; tai42_skeleton.template ContentPart and ResourceManager.normalize_media are replaced by tai42_contract.template.ImageReference and resolve_image; StructuredOutputPlan.bound_typed_dict is removed; LeadingUserMiddleware no longer has before_model/abefore_model; roll_cache_marks requires mark_key; RetrievalToolsGraph requires a provider; default_ttl_seconds is removed from SandboxDispatchSettings, DockerSandboxSettings and SandboxLocalSettings, and a stale SANDBOX_<PROVIDER>_DEFAULT_TTL_SECONDS refuses boot; tai42_e2e.settings.LLM_PROVIDERS changes.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* tai42_contract.agent.SubAgentSpec and tai42_skeleton.agent.SubAgentSpec are removed (the agents plugin keeps its own); PresetSpec.name must match PRESET_NAME_PATTERN; the suspended-interaction marker moves from tai42_contract.interactions.continuation to park_markers, and suspended_interaction_marker requires resume_owner and the keyword-only interaction_ids and caller_interaction_ids; reserved_thread_namespace_error takes the agent class first; CLIENT_TOOL_NAME_MAX_LEN is removed (use TOOL_NAME_MAX_LEN); the agents' run and astream no longer accept free keyword arguments, and claude_code's build_options_payload and build_payload take proxy_tools in place of tool_names; the contract's served documents change.
* **skeleton:** a configuration whose fleet apply window is below three times the tool-runs drain budget refuses to boot, reload or apply (ApplyWindowBelowDrainError); the default apply window is 90 seconds; translate_backend_needs_bus is renamed translate_config_invariants.
* **kit:** a cancellation requested during a pooled Postgres checkout reaches its task
* tai42_e2e.settings.REAL_SERVICES gains the connector-slack and connector-github real-service seams.
* the AppAdmin protocol gains reprobe_failed_mcps(titles); tai42_e2e.topology.StackResources takes langfuse_host, langfuse_public_key and langfuse_secret_key at new positions; pass them by keyword.
* tai42_skeleton.connectors.token_injection.call_with_auth no longer takes auth (its parameters are config, transport, tool_name, arguments, mcp_client) and handle_token_expired is removed (folded into with_managed_auth); tai42_skeleton.app.epoch.EpochAdmissionApp is no longer reachable from the epoch module; import it from tai42_skeleton.app.admission, where it is defined.
* tai42_skeleton.connectors.oauth.crypto encrypt, decrypt and decrypt_reporting_key take a keyword aad in place of connection_id (the connection path passes aad=connection_id, so stored bytes are unchanged).
* ProviderDescriptor.icon_url and ProviderCatalogEntry.icon_url widen from str to str | None; the ConnectionService.complete_connect Protocol takes code: str | None and the new iss and error keywords, so an external implementer of the Protocol must add them; tai42_skeleton.connectors.oauth.client build_authorize_url, exchange_code, refresh and revoke take the required resolved and client arguments.
* **skeleton:** wall-clock-free skeleton tests; a profile apply computes the X band once
* tai42_skeleton.app.epoch.attach_boot_serving_app takes a required keyword-only stateless_http argument.
* **skeleton:** tai42_skeleton.app no longer exports TaiMCP and tai42_skeleton.access_control no longer exports AuthAdapter, AccessControlSettings or access_control_settings; import them from their defining submodules.
* resolve_trace_context and bind_run_trace raise InvalidRunTraceError for an explicit monitoring_trace_id that is not 32 lowercase hex (not all zeros) or a monitoring_parent_span_id that is not 16 lowercase hex (not all zeros).
* **skeleton:** DELETE /api/auth/principals/{id} of a principal whose login record a provider keeps answers 409 naming that provider's member actions; GET /api/auth/members joins each principal's role (principals[].role) and derives the row role from them. In the contract, MemberEntry.role is removed and MemberPrincipalState.role is required.
* an env write of a recycle-class key is refused on a bare deployment and rolls a fleet recycle on a supervised one, on every env write door; after a converged roll the serving worker exits (drains and is respawned) after a projected tool call too (MCP tools/call, tai tools run, an agent, a preset, a schedule, a hook), and a backend worker exits and is respawned after a job that wrote a recycle-class env key; write_env, set_mcp_secret_env, import_backup, marketplace_install, marketplace_update and marketplace_uninstall return an OperationResponse instead of a plain payload; tai42_skeleton.app.graceful_exit replaces request_serve_graceful_exit, request_backend_graceful_exit and graceful_exit_for with request_graceful_exit(pid); ConfigService.apply_replace_env no longer takes orchestrate; and tai42_skeleton.marketplace.manifest_apply.reload_report is removed (the installer hands its ApplyResult to the operation, which renders it).
* a reload answers its caller on every transport; the bus heartbeat survives the rebuild freeze ([#551](https://github.com/tai42ai/tai42/issues/551))
* a profile apply reports a stopped recycle roll; the working signal keeps its cadence ([#549](https://github.com/tai42ai/tai42/issues/549))
* the states settings are read once per settings generation ([#547](https://github.com/tai42ai/tai42/issues/547))
* an explicit null template default survives the write; a null tool argument reaches its server ([#546](https://github.com/tai42ai/tai42/issues/546))
* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545))
* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542))
* one batch template-reference resolver on the states facet; the binding saves use it ([#541](https://github.com/tai42ai/tai42/issues/541))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* the access-control route index follows the route registry version ([#539](https://github.com/tai42ai/tai42/issues/539))
* storage-local has no default root; the kit logging registries replace the per-feature hooks; the curl client's pool-key parameter is renamed.
* the accounts archive is version 2 and refuses version 1; the accounts plugin drops its role column, guard and lock; the contract gains the admin services.
* the readiness and drain registries live on the app instance; the connector token store's listing and restore seams replace the SQL that backup and re-encrypt carried; the kit's pinning transport constructor changes.
* the states facet and the pending-saves routes change shape; the PUT state answer carries held.
* the access-control middleware stack gains a first entry.
* the trace-detail wire loses type and usage and gains kind and the token fields; an unknown LANGFUSE_ variable refuses boot.
* the two access-control path-list settings default to empty and authenticated_always_allowed_paths is removed.
* the states facet gains the two resolver methods and the engine no longer resolves names itself.
* the contract's access-control surface drops the unused PolicyEnforcer protocol; route rows change shape.
* the policy enforcer is served by the accessor, not constructed per call; the tool-surface generation is part of the tools facet.
* the park records and the resolution record change shape; the LLM_PROVIDER_CHECKPOINT_TTL_MINUTES and FLOW_CHECKPOINT_RETENTION_DAYS variables are refused.
* the contract's template models are typed and the states facet's render methods change shape; the declarations and templates tables take their version from a sequence; the closed-unit error is typed.
* the engine's checkpoint TTL setting is removed in favour of the platform retention setting; stored checkpoints in the previous format are refused at boot and must be reset; the kit declares redisvl in its llm extra.
* INTERACTIONS_NOTIFICATIONS_* and INTERACTIONS_SEND_RECEIPT_* move to CHANNELS_*; the config env door body is {env, secret_keys}; the recycle constants are removed in favour of TAI_SUPERVISED_PINNED_KEYS; an unknown name under an owned prefix refuses boot.
* the monitoring writer protocol and the recorded shapes change; the plugin's SDK-provided settings names are refused; the collector credential is OTEL_COLLECTOR_LANGFUSE_AUTH.
* **skeleton:** BaseHooksManager.register is concrete and backends implement _store_hook; the door statuses and the backup section order change.
* **kit:** a settings class that sets model_config["env_file"] is refused; get_mcp_transport returns the keep-alive transport type.
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.
* **skeleton:** BaseConversationsManager gains the abstract durable property; every conversations manager implementation must provide it.

### Features

* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545)) ([77ea6e2](https://github.com/tai42ai/tai42/commit/77ea6e2898d2c8d743920cb4567a61e4748e16fc))
* a transactional state outbox, the after-reply call, pending-save doors and card ([#531](https://github.com/tai42ai/tai42/issues/531)) ([e9af951](https://github.com/tai42ai/tai42/commit/e9af951ea7dd70b8ce9f031aac088a50389ad4f1))
* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* connectors own their token rows; readiness and drain registries per app; public seams ([#533](https://github.com/tai42ai/tai42/issues/533)) ([1bd4cd1](https://github.com/tai42ai/tai42/commit/1bd4cd1ada2b50977850483eac1908b1c0b5c7e1))
* content-agnostic storage, kit-owned log redaction, one error-metadata span builder ([#534](https://github.com/tai42ai/tai42/issues/534)) ([71d93e8](https://github.com/tai42ai/tai42/commit/71d93e833e95df0a1d034681c8550acfcbd11374))
* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
* **kit:** env-file cache, keyed settings, orjson jsonb reads, MCP keep-alive, read_connection ([#516](https://github.com/tai42ai/tai42/issues/516)) ([886da9e](https://github.com/tai42ai/tai42/commit/886da9edd901fd8456212abb320ccea8da0fa2a3))
* monitoring over OpenTelemetry with records stored once and exported through a collector ([#520](https://github.com/tai42ai/tai42/issues/520)) ([ab2000e](https://github.com/tai42ai/tai42/commit/ab2000e7f3f8f30046164e8460546a24b082191f))
* monitoring read side: span kinds and tokens, GenAI messages, references resolved on request ([#529](https://github.com/tai42ai/tai42/issues/529)) ([8ea9e65](https://github.com/tai42ai/tai42/commit/8ea9e650dbfc2114349e64c4c5043906d602aeeb))
* mounted MCP tools keep their server's display fields; real legs for the vendor connectors ([ac3105a](https://github.com/tai42ai/tai42/commit/ac3105a2c4771ac42f340b8f1d0dd54cb4c0ae0b))
* one batch template-reference resolver on the states facet; the binding saves use it ([#541](https://github.com/tai42ai/tai42/issues/541)) ([a5663b3](https://github.com/tai42ai/tai42/commit/a5663b36a63224cf5259ec969b61ebb7df97ae27))
* one canonical route path on every door, per-writer policy versions, SSE bare-prefix record ([#525](https://github.com/tai42ai/tai42/issues/525)) ([25f713b](https://github.com/tai42ai/tai42/commit/25f713b9d63b2346ffe33408105b09efcfed64f5))
* one checkpoint retention setting, faithful Redis codec, durability mode, stale-thread sweep ([#519](https://github.com/tai42ai/tai42/issues/519)) ([b261cea](https://github.com/tai42ai/tai42/commit/b261cea384f09450c548c96b0af3efa025d65fe1))
* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* one guarded admin service set, the access-control backup section, throttles, providers ([#535](https://github.com/tai42ai/tai42/issues/535)) ([517048b](https://github.com/tai42ai/tai42/commit/517048b5785671777c3e68c969deeb6ea50ac183))
* one policy enforcer per loop, route/config snapshots, tool-surface generation, graph cache ([#526](https://github.com/tai42ai/tai42/issues/526)) ([58934b2](https://github.com/tai42ai/tai42/commit/58934b2010fc047b60012bec310b9dd2e06e5d4f))
* one subject resolver and one template-reference resolver on the states facet, on every door ([#527](https://github.com/tai42ai/tai42/issues/527)) ([d5939b1](https://github.com/tai42ai/tai42/commit/d5939b15c0c3585ae1318e68676789b3de730c7d))
* owned env prefixes, channel settings, one owner for secret marks and env markers, pinned keys ([#521](https://github.com/tai42ai/tai42/issues/521)) ([5c5c046](https://github.com/tai42ai/tai42/commit/5c5c04649afdb0866981127ffe0c0424b04af463))
* park index with token-guarded finalize, chain lineage on every resume door, failed terminal ([#524](https://github.com/tai42ai/tai42/issues/524)) ([e59ec7b](https://github.com/tai42ai/tai42/commit/e59ec7b15d868079e6e04b15c9550cd6665262c0))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* routes declare their reach; the server serves the sub-MCP mount, pattern and public id ([#528](https://github.com/tai42ai/tai42/issues/528)) ([1accaf5](https://github.com/tai42ai/tai42/commit/1accaf55e1c312216e13dd275c0661d46bb7ae86))
* **skeleton:** a member's role is joined per principal; a login-owning provider owns the delete ([26a14e9](https://github.com/tai42ai/tai42/commit/26a14e9008dde22a883fefed9017362d3a88d11f))
* **skeleton:** one write service per definition kind for the doors and the restore ([#517](https://github.com/tai42ai/tai42/issues/517)) ([2e4faec](https://github.com/tai42ai/tai42/commit/2e4faec21969d1dcad82bc018e2eb494ff7b603a))
* **skeleton:** serve conversation stores and settings from the manager ([#513](https://github.com/tai42ai/tai42/issues/513)) ([5f396db](https://github.com/tai42ai/tai42/commit/5f396db8cc6f3379b1691f41c6b75b9a068a4f2e))
* **skeleton:** the fleet apply window honours the retire's drain budget ([35a3fd4](https://github.com/tai42ai/tai42/commit/35a3fd476bde0a731892a413c50089893517feed))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* structured output binds the authored schema; provider-keyed history; the sandbox seam ([f2675f8](https://github.com/tai42ai/tai42/commit/f2675f8511fc14a5dcda4b9a63eaa6a85a1949c0))
* the access-control check reads the policy version once per request ([#530](https://github.com/tai42ai/tai42/issues/530)) ([0fb54c2](https://github.com/tai42ai/tai42/commit/0fb54c27845781bb1c53889d74ea8dad97d27441))
* the add-server door: discover, add and remove a discovered MCP server and set its client ([d308b40](https://github.com/tai42ai/tai42/commit/d308b40bcf6fe1106fac9f1dbf8ffcabb51758d1))
* the agent surface is framework-neutral: client tools, one run signature, declared parks ([37578fc](https://github.com/tai42ai/tai42/commit/37578fc51dea9232ae4b2bcc095dce3103ba1b72))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))
* the connector client store, the client-identity document and dynamic client registration ([74aa542](https://github.com/tai42ai/tai42/commit/74aa542323e5992ef275c78203d777c7fdfc5c06))
* the connector discovery seam: discovered providers, one resolver on every OAuth door ([34578ef](https://github.com/tai42ai/tai42/commit/34578ef0abc208624d0ac7cc5572225681c7384c))
* the inbound MCP door: protected-resource metadata, the challenge and the request resource ([5aeb9bc](https://github.com/tai42ai/tai42/commit/5aeb9bcd24a454327e88d0464d2a2a26c5e1fad4))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))
* the run-time credential path: one managed-auth seam for every MCP call and listing ([f31a9a2](https://github.com/tai42ai/tai42/commit/f31a9a219f8ce174a860e15398d542edf84d4abc))
* the skeleton publishes its OpenAPI artifact; the contract serves its constants ([6344834](https://github.com/tai42ai/tai42/commit/6344834a7a2a03d9e8773c50cb2b62853be908a9))
* the studio plugin listing serves each plugin's route mounts and route panels ([45392e4](https://github.com/tai42ai/tai42/commit/45392e43b21e2e4d4229b1f823a56f2d2c126e1a))
* typed state templates, version-keyed catalogs, a compare-and-set unit commit ([#523](https://github.com/tai42ai/tai42/issues/523)) ([51f7aa6](https://github.com/tai42ai/tai42/commit/51f7aa6656b23735d8c364b7e3fef2265470ae2e))


### Bug Fixes

* a profile apply reports a stopped recycle roll; the working signal keeps its cadence ([#549](https://github.com/tai42ai/tai42/issues/549)) ([ac2e8c2](https://github.com/tai42ai/tai42/commit/ac2e8c29289607141dd06702c18f4f46597b0f36))
* a reload answers its caller on every transport; the bus heartbeat survives the rebuild freeze ([#551](https://github.com/tai42ai/tai42/issues/551)) ([d2e24ac](https://github.com/tai42ai/tai42/commit/d2e24acefee6444ac87caa1b318f5f6ef43c45bc))
* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544)) ([18b495b](https://github.com/tai42ai/tai42/commit/18b495b0f3992e8b13aa7c2a1e98939caad014d0))
* an explicit null template default survives the write; a null tool argument reaches its server ([#546](https://github.com/tai42ai/tai42/issues/546)) ([d1fa7e7](https://github.com/tai42ai/tai42/commit/d1fa7e741f6e2be28543ec3f898bf075a689ae0d))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542)) ([f00f65c](https://github.com/tai42ai/tai42/commit/f00f65c565736a4b80caa96624b536a0c66b0319))
* **kit:** a cancellation requested during a pooled Postgres checkout reaches its task ([25cb046](https://github.com/tai42ai/tai42/commit/25cb04686cd290c451c0f07118ece8564fb8fc79))
* recycle-class env changes respawn through every door and judge the value a worker bound ([efc13d0](https://github.com/tai42ai/tai42/commit/efc13d0f3e99d95db8bba7500ee3f10c517e97b8))
* refuse a run whose explicit trace id or parent anchor no writer can record under ([62c9265](https://github.com/tai42ai/tai42/commit/62c92653674f92c626c2180630712a5be5a50bd1))
* **skeleton:** skeleton modules import on their own; package inits re-export nothing ([aeea68d](https://github.com/tai42ai/tai42/commit/aeea68d44452a1121d1f8fce5490d83f62cda812))
* **skeleton:** wall-clock-free skeleton tests; a profile apply computes the X band once ([f3117ec](https://github.com/tai42ai/tai42/commit/f3117eccb860861367b1a431417badc38a925efa))
* sub-MCP apps keep the stateless boot mode; the e2e relay keeps idle connections open ([b99af59](https://github.com/tai42ai/tai42/commit/b99af59c800e1cbae98547b17aa9f1c3a695a574))
* the access-control route index follows the route registry version ([#539](https://github.com/tai42ai/tai42/issues/539)) ([4990e76](https://github.com/tai42ai/tai42/commit/4990e762d16c1dd4a9b78aef513fbec26277eba4))


### Performance Improvements

* the states settings are read once per settings generation ([#547](https://github.com/tai42ai/tai42/issues/547)) ([cf7c121](https://github.com/tai42ai/tai42/commit/cf7c121c7cee84937f535e49419d0927aa898045))
</details>

<details><summary>tai42-accounts-postgres: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-accounts-postgres-v4.0.4...tai42-accounts-postgres-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.
* **skeleton:** DELETE /api/auth/principals/{id} of a principal whose login record a provider keeps answers 409 naming that provider's member actions; GET /api/auth/members joins each principal's role (principals[].role) and derives the row role from them. In the contract, MemberEntry.role is removed and MemberPrincipalState.role is required.
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542))
* the accounts archive is version 2 and refuses version 1; the accounts plugin drops its role column, guard and lock; the contract gains the admin services.
* the two access-control path-list settings default to empty and authenticated_always_allowed_paths is removed.
* the policy enforcer is served by the accessor, not constructed per call; the tool-surface generation is part of the tools facet.
* **kit:** a settings class that sets model_config["env_file"] is refused; get_mcp_transport returns the keep-alive transport type.
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
* **kit:** env-file cache, keyed settings, orjson jsonb reads, MCP keep-alive, read_connection ([#516](https://github.com/tai42ai/tai42/issues/516)) ([886da9e](https://github.com/tai42ai/tai42/commit/886da9edd901fd8456212abb320ccea8da0fa2a3))
* one guarded admin service set, the access-control backup section, throttles, providers ([#535](https://github.com/tai42ai/tai42/issues/535)) ([517048b](https://github.com/tai42ai/tai42/commit/517048b5785671777c3e68c969deeb6ea50ac183))
* one policy enforcer per loop, route/config snapshots, tool-surface generation, graph cache ([#526](https://github.com/tai42ai/tai42/issues/526)) ([58934b2](https://github.com/tai42ai/tai42/commit/58934b2010fc047b60012bec310b9dd2e06e5d4f))
* routes declare their reach; the server serves the sub-MCP mount, pattern and public id ([#528](https://github.com/tai42ai/tai42/issues/528)) ([1accaf5](https://github.com/tai42ai/tai42/commit/1accaf55e1c312216e13dd275c0661d46bb7ae86))
* **skeleton:** a member's role is joined per principal; a login-owning provider owns the delete ([26a14e9](https://github.com/tai42ai/tai42/commit/26a14e9008dde22a883fefed9017362d3a88d11f))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542)) ([f00f65c](https://github.com/tai42ai/tai42/commit/f00f65c565736a4b80caa96624b536a0c66b0319))
</details>

<details><summary>tai42-agents: 9.0.0</summary>

## [9.0.0](https://github.com/tai42ai/tai42/compare/tai42-agents-v8.5.4...tai42-agents-v9.0.0) (2026-10-13)


###   BREAKING CHANGES

* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* **agents:** every parking agent kind parks under a holder and sends its caller ask up
* OperationError and UnavailableError move from tai42_skeleton.operations to tai42_contract.errors; tai42_skeleton.template ContentPart and ResourceManager.normalize_media are replaced by tai42_contract.template.ImageReference and resolve_image; StructuredOutputPlan.bound_typed_dict is removed; LeadingUserMiddleware no longer has before_model/abefore_model; roll_cache_marks requires mark_key; RetrievalToolsGraph requires a provider; default_ttl_seconds is removed from SandboxDispatchSettings, DockerSandboxSettings and SandboxLocalSettings, and a stale SANDBOX_<PROVIDER>_DEFAULT_TTL_SECONDS refuses boot; tai42_e2e.settings.LLM_PROVIDERS changes.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* tai42_contract.agent.SubAgentSpec and tai42_skeleton.agent.SubAgentSpec are removed (the agents plugin keeps its own); PresetSpec.name must match PRESET_NAME_PATTERN; the suspended-interaction marker moves from tai42_contract.interactions.continuation to park_markers, and suspended_interaction_marker requires resume_owner and the keyword-only interaction_ids and caller_interaction_ids; reserved_thread_namespace_error takes the agent class first; CLIENT_TOOL_NAME_MAX_LEN is removed (use TOOL_NAME_MAX_LEN); the agents' run and astream no longer accept free keyword arguments, and claude_code's build_options_payload and build_payload take proxy_tools in place of tool_names; the contract's served documents change.
* resolve_trace_context and bind_run_trace raise InvalidRunTraceError for an explicit monitoring_trace_id that is not 32 lowercase hex (not all zeros) or a monitoring_parent_span_id that is not 16 lowercase hex (not all zeros).
* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* the policy enforcer is served by the accessor, not constructed per call; the tool-surface generation is part of the tools facet.
* the park records and the resolution record change shape; the LLM_PROVIDER_CHECKPOINT_TTL_MINUTES and FLOW_CHECKPOINT_RETENTION_DAYS variables are refused.
* the engine's checkpoint TTL setting is removed in favour of the platform retention setting; stored checkpoints in the previous format are refused at boot and must be reset; the kit declares redisvl in its llm extra.
* the monitoring writer protocol and the recorded shapes change; the plugin's SDK-provided settings names are refused; the collector credential is OTEL_COLLECTOR_LANGFUSE_AUTH.
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545)) ([77ea6e2](https://github.com/tai42ai/tai42/commit/77ea6e2898d2c8d743920cb4567a61e4748e16fc))
* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* monitoring over OpenTelemetry with records stored once and exported through a collector ([#520](https://github.com/tai42ai/tai42/issues/520)) ([ab2000e](https://github.com/tai42ai/tai42/commit/ab2000e7f3f8f30046164e8460546a24b082191f))
* one checkpoint retention setting, faithful Redis codec, durability mode, stale-thread sweep ([#519](https://github.com/tai42ai/tai42/issues/519)) ([b261cea](https://github.com/tai42ai/tai42/commit/b261cea384f09450c548c96b0af3efa025d65fe1))
* one policy enforcer per loop, route/config snapshots, tool-surface generation, graph cache ([#526](https://github.com/tai42ai/tai42/issues/526)) ([58934b2](https://github.com/tai42ai/tai42/commit/58934b2010fc047b60012bec310b9dd2e06e5d4f))
* park index with token-guarded finalize, chain lineage on every resume door, failed terminal ([#524](https://github.com/tai42ai/tai42/issues/524)) ([e59ec7b](https://github.com/tai42ai/tai42/commit/e59ec7b15d868079e6e04b15c9550cd6665262c0))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* structured output binds the authored schema; provider-keyed history; the sandbox seam ([f2675f8](https://github.com/tai42ai/tai42/commit/f2675f8511fc14a5dcda4b9a63eaa6a85a1949c0))
* the agent surface is framework-neutral: client tools, one run signature, declared parks ([37578fc](https://github.com/tai42ai/tai42/commit/37578fc51dea9232ae4b2bcc095dce3103ba1b72))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* **agents:** every parking agent kind parks under a holder and sends its caller ask up ([244c9e6](https://github.com/tai42ai/tai42/commit/244c9e62ca560837d2a6252cae189a16434c3f2c))
* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542)) ([f00f65c](https://github.com/tai42ai/tai42/commit/f00f65c565736a4b80caa96624b536a0c66b0319))
* refuse a run whose explicit trace id or parent anchor no writer can record under ([62c9265](https://github.com/tai42ai/tai42/commit/62c92653674f92c626c2180630712a5be5a50bd1))
</details>

<details><summary>tai42-backend-arq: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-backend-arq-v4.3.4...tai42-backend-arq-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* an env write of a recycle-class key is refused on a bare deployment and rolls a fleet recycle on a supervised one, on every env write door; after a converged roll the serving worker exits (drains and is respawned) after a projected tool call too (MCP tools/call, tai tools run, an agent, a preset, a schedule, a hook), and a backend worker exits and is respawned after a job that wrote a recycle-class env key; write_env, set_mcp_secret_env, import_backup, marketplace_install, marketplace_update and marketplace_uninstall return an OperationResponse instead of a plain payload; tai42_skeleton.app.graceful_exit replaces request_serve_graceful_exit, request_backend_graceful_exit and graceful_exit_for with request_graceful_exit(pid); ConfigService.apply_replace_env no longer takes orchestrate; and tai42_skeleton.marketplace.manifest_apply.reload_report is removed (the installer hands its ApplyResult to the operation, which renders it).
* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))


### Bug Fixes

* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544)) ([18b495b](https://github.com/tai42ai/tai42/commit/18b495b0f3992e8b13aa7c2a1e98939caad014d0))
* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
* recycle-class env changes respawn through every door and judge the value a worker bound ([efc13d0](https://github.com/tai42ai/tai42/commit/efc13d0f3e99d95db8bba7500ee3f10c517e97b8))
</details>

<details><summary>tai42-backend-celery: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-backend-celery-v4.2.4...tai42-backend-celery-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* **skeleton:** a configuration whose fleet apply window is below three times the tool-runs drain budget refuses to boot, reload or apply (ApplyWindowBelowDrainError); the default apply window is 90 seconds; translate_backend_needs_bus is renamed translate_config_invariants.
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* **skeleton:** the fleet apply window honours the retire's drain budget ([35a3fd4](https://github.com/tai42ai/tai42/commit/35a3fd476bde0a731892a413c50089893517feed))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-backend-rq: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-backend-rq-v4.1.9...tai42-backend-rq-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-channel-slack: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-channel-slack-v4.4.4...tai42-channel-slack-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.

### Features

* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-channel-telegram: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-channel-telegram-v4.4.4...tai42-channel-telegram-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* storage-local has no default root; the kit logging registries replace the per-feature hooks; the curl client's pool-key parameter is renamed.

### Features

* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* content-agnostic storage, kit-owned log redaction, one error-metadata span builder ([#534](https://github.com/tai42ai/tai42/issues/534)) ([71d93e8](https://github.com/tai42ai/tai42/commit/71d93e833e95df0a1d034681c8550acfcbd11374))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-channel-twilio: 6.0.0</summary>

## [6.0.0](https://github.com/tai42ai/tai42/compare/tai42-channel-twilio-v5.3.4...tai42-channel-twilio-v6.0.0) (2026-10-13)


###   BREAKING CHANGES

* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.

### Features

* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-channel-web: 7.0.0</summary>

## [7.0.0](https://github.com/tai42ai/tai42/compare/tai42-channel-web-v6.5.2...tai42-channel-web-v7.0.0) (2026-10-13)


###   BREAKING CHANGES

* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.

### Features

* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-channel-whatsapp: 7.0.0</summary>

## [7.0.0](https://github.com/tai42ai/tai42/compare/tai42-channel-whatsapp-v6.5.3...tai42-channel-whatsapp-v7.0.0) (2026-10-13)


###   BREAKING CHANGES

* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.

### Features

* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))


### Bug Fixes

* **channel-whatsapp:** the form Flow's privacy notes and tests name the send's recipient ([34708a8](https://github.com/tai42ai/tai42/commit/34708a86808518ecd4cbf4ac1c8c93c90fc5d4f4))
* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-connector-atlassian: 3.0.0</summary>

## [3.0.0](https://github.com/tai42ai/tai42/compare/tai42-connector-atlassian-v2.1.5...tai42-connector-atlassian-v3.0.0) (2026-10-13)


###   BREAKING CHANGES

* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.

### Features

* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
</details>

<details><summary>tai42-connector-google: 3.0.0</summary>

## [3.0.0](https://github.com/tai42ai/tai42/compare/tai42-connector-google-v2.1.5...tai42-connector-google-v3.0.0) (2026-10-13)


###   BREAKING CHANGES

* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.

### Features

* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
</details>

<details><summary>tai42-connector-github: 2.0.0</summary>

## [2.0.0](https://github.com/tai42ai/tai42/compare/tai42-connector-github-v1.1.5...tai42-connector-github-v2.0.0) (2026-10-13)


###   BREAKING CHANGES

* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.

### Features

* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
</details>

<details><summary>tai42-connector-slack: 2.0.0</summary>

## [2.0.0](https://github.com/tai42ai/tai42/compare/tai42-connector-slack-v1.1.5...tai42-connector-slack-v2.0.0) (2026-10-13)


###   BREAKING CHANGES

* a conversation route is created and read as a union discriminated on door (ApiConversationRouteCreate | ChannelConversationRouteCreate), so ConversationRoute, ConversationRouteCreate and ConversationRouteView are type aliases of unions and TargetBindValidator takes the union; ChannelTemplate, ReplyOption, LinkOption, OptionSection, FormOption, FormData, DisplayBlock, FormPage and LocationElement refuse an unknown key; a connector's return_url defaults to None, resolved to the hosted UI's connectors page (StartConnectRequest, StartReconnectRequest, PatchSubServicesRequest, the CLI's connect, reconnect and sub-services), and the skeleton's start_connect, reconnect and patch_sub_services take origin, redirect_uri and return_url at new positions; tai42_skeleton.connectors.oauth.redirect.CALLBACK_PATH is removed (use callback_uri); ConfigMode.file takes its value from the contract's FILE_CONFIG_MODE; the contract's served documents change.

### Features

* each rule's owner serves it and the CLI imports it ([42e20d5](https://github.com/tai42ai/tai42/commit/42e20d59aa2033502e72b1c072381ee3cd9e768a))
</details>

<details><summary>tai42-identity-redis: 4.0.0</summary>

## [4.0.0](https://github.com/tai42ai/tai42/compare/tai42-identity-redis-v3.0.4...tai42-identity-redis-v4.0.0) (2026-10-13)


###   BREAKING CHANGES

* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-monitoring-langfuse: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-monitoring-langfuse-v4.0.4...tai42-monitoring-langfuse-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* the trace-detail wire loses type and usage and gains kind and the token fields; an unknown LANGFUSE_ variable refuses boot.
* the monitoring writer protocol and the recorded shapes change; the plugin's SDK-provided settings names are refused; the collector credential is OTEL_COLLECTOR_LANGFUSE_AUTH.

### Features

* monitoring over OpenTelemetry with records stored once and exported through a collector ([#520](https://github.com/tai42ai/tai42/issues/520)) ([ab2000e](https://github.com/tai42ai/tai42/commit/ab2000e7f3f8f30046164e8460546a24b082191f))
* monitoring read side: span kinds and tokens, GenAI messages, references resolved on request ([#529](https://github.com/tai42ai/tai42/issues/529)) ([8ea9e65](https://github.com/tai42ai/tai42/commit/8ea9e650dbfc2114349e64c4c5043906d602aeeb))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-sandbox-docker: 2.0.0</summary>

## [2.0.0](https://github.com/tai42ai/tai42/compare/tai42-sandbox-docker-v1.4.7...tai42-sandbox-docker-v2.0.0) (2026-10-13)


###   BREAKING CHANGES

* OperationError and UnavailableError move from tai42_skeleton.operations to tai42_contract.errors; tai42_skeleton.template ContentPart and ResourceManager.normalize_media are replaced by tai42_contract.template.ImageReference and resolve_image; StructuredOutputPlan.bound_typed_dict is removed; LeadingUserMiddleware no longer has before_model/abefore_model; roll_cache_marks requires mark_key; RetrievalToolsGraph requires a provider; default_ttl_seconds is removed from SandboxDispatchSettings, DockerSandboxSettings and SandboxLocalSettings, and a stale SANDBOX_<PROVIDER>_DEFAULT_TTL_SECONDS refuses boot; tai42_e2e.settings.LLM_PROVIDERS changes.

### Features

* structured output binds the authored schema; provider-keyed history; the sandbox seam ([f2675f8](https://github.com/tai42ai/tai42/commit/f2675f8511fc14a5dcda4b9a63eaa6a85a1949c0))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-sandbox-local: 2.0.0</summary>

## [2.0.0](https://github.com/tai42ai/tai42/compare/tai42-sandbox-local-v1.3.7...tai42-sandbox-local-v2.0.0) (2026-10-13)


###   BREAKING CHANGES

* OperationError and UnavailableError move from tai42_skeleton.operations to tai42_contract.errors; tai42_skeleton.template ContentPart and ResourceManager.normalize_media are replaced by tai42_contract.template.ImageReference and resolve_image; StructuredOutputPlan.bound_typed_dict is removed; LeadingUserMiddleware no longer has before_model/abefore_model; roll_cache_marks requires mark_key; RetrievalToolsGraph requires a provider; default_ttl_seconds is removed from SandboxDispatchSettings, DockerSandboxSettings and SandboxLocalSettings, and a stale SANDBOX_<PROVIDER>_DEFAULT_TTL_SECONDS refuses boot; tai42_e2e.settings.LLM_PROVIDERS changes.

### Features

* structured output binds the authored schema; provider-keyed history; the sandbox seam ([f2675f8](https://github.com/tai42ai/tai42/commit/f2675f8511fc14a5dcda4b9a63eaa6a85a1949c0))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-storage-github: 1.2.10</summary>

## [1.2.10](https://github.com/tai42ai/tai42/compare/tai42-storage-github-v1.2.9...tai42-storage-github-v1.2.10) (2026-10-13)


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-storage-local: 2.0.0</summary>

## [2.0.0](https://github.com/tai42ai/tai42/compare/tai42-storage-local-v1.2.9...tai42-storage-local-v2.0.0) (2026-10-13)


###   BREAKING CHANGES

* storage-local has no default root; the kit logging registries replace the per-feature hooks; the curl client's pool-key parameter is renamed.

### Features

* content-agnostic storage, kit-owned log redaction, one error-metadata span builder ([#534](https://github.com/tai42ai/tai42/issues/534)) ([71d93e8](https://github.com/tai42ai/tai42/commit/71d93e833e95df0a1d034681c8550acfcbd11374))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-storage-s3: 2.0.0</summary>

## [2.0.0](https://github.com/tai42ai/tai42/compare/tai42-storage-s3-v1.2.11...tai42-storage-s3-v2.0.0) (2026-10-13)


###   BREAKING CHANGES

* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544))
* storage-local has no default root; the kit logging registries replace the per-feature hooks; the curl client's pool-key parameter is renamed.

### Features

* content-agnostic storage, kit-owned log redaction, one error-metadata span builder ([#534](https://github.com/tai42ai/tai42/issues/534)) ([71d93e8](https://github.com/tai42ai/tai42/commit/71d93e833e95df0a1d034681c8550acfcbd11374))


### Bug Fixes

* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544)) ([18b495b](https://github.com/tai42ai/tai42/commit/18b495b0f3992e8b13aa7c2a1e98939caad014d0))
* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-toolbox: 5.0.0</summary>

## [5.0.0](https://github.com/tai42ai/tai42/compare/tai42-toolbox-v4.2.9...tai42-toolbox-v5.0.0) (2026-10-13)


###   BREAKING CHANGES

* tai42_contract.agent.SubAgentSpec and tai42_skeleton.agent.SubAgentSpec are removed (the agents plugin keeps its own); PresetSpec.name must match PRESET_NAME_PATTERN; the suspended-interaction marker moves from tai42_contract.interactions.continuation to park_markers, and suspended_interaction_marker requires resume_owner and the keyword-only interaction_ids and caller_interaction_ids; reserved_thread_namespace_error takes the agent class first; CLIENT_TOOL_NAME_MAX_LEN is removed (use TOOL_NAME_MAX_LEN); the agents' run and astream no longer accept free keyword arguments, and claude_code's build_options_payload and build_payload take proxy_tools in place of tool_names; the contract's served documents change.
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* storage-local has no default root; the kit logging registries replace the per-feature hooks; the curl client's pool-key parameter is renamed.

### Features

* content-agnostic storage, kit-owned log redaction, one error-metadata span builder ([#534](https://github.com/tai42ai/tai42/issues/534)) ([71d93e8](https://github.com/tai42ai/tai42/commit/71d93e833e95df0a1d034681c8550acfcbd11374))
* the agent surface is framework-neutral: client tools, one run signature, declared parks ([37578fc](https://github.com/tai42ai/tai42/commit/37578fc51dea9232ae4b2bcc095dce3103ba1b72))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-tools-github: 1.1.10</summary>

## [1.1.10](https://github.com/tai42ai/tai42/compare/tai42-tools-github-v1.1.9...tai42-tools-github-v1.1.10) (2026-10-13)


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-tools-stripe: 2.0.0</summary>

## [2.0.0](https://github.com/tai42ai/tai42/compare/tai42-tools-stripe-v1.2.9...tai42-tools-stripe-v2.0.0) (2026-10-13)


###   BREAKING CHANGES

* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.

### Features

* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-tools-twilio: 1.1.10</summary>

## [1.1.10](https://github.com/tai42ai/tai42/compare/tai42-tools-twilio-v1.1.9...tai42-tools-twilio-v1.1.10) (2026-10-13)


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-tools-whatsapp: 1.2.10</summary>

## [1.2.10](https://github.com/tai42ai/tai42/compare/tai42-tools-whatsapp-v1.2.9...tai42-tools-whatsapp-v1.2.10) (2026-10-13)


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-webhook-verifier-github: 1.3.6</summary>

## [1.3.6](https://github.com/tai42ai/tai42/compare/tai42-webhook-verifier-github-v1.3.5...tai42-webhook-verifier-github-v1.3.6) (2026-10-13)


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-webhook-verifier-stripe: 1.3.6</summary>

## [1.3.6](https://github.com/tai42ai/tai42/compare/tai42-webhook-verifier-stripe-v1.3.5...tai42-webhook-verifier-stripe-v1.3.6) (2026-10-13)


### Bug Fixes

* **ci:** make every plugin's coverage floor bind and run the celery live-broker test in CI ([#518](https://github.com/tai42ai/tai42/issues/518)) ([9a72934](https://github.com/tai42ai/tai42/commit/9a7293428ef430ad671e1f3d4c7a6598f010ff37))
</details>

<details><summary>tai42-e2e: 17.0.0</summary>

## [17.0.0](https://github.com/tai42ai/tai42/compare/tai42-e2e-v16.6.2...tai42-e2e-v17.0.0) (2026-10-13)


###   BREAKING CHANGES

* **e2e:** the inbox resync waits for the last interactions stream the page opens ([#584](https://github.com/tai42ai/tai42/issues/584))
* the inbound MCP door: protected-resource metadata, the challenge and the request resource
* POST /api/schedules refuses schedule_kwargs and a vehicle tool_name (pass the base tool and a cadence); GET /api/schedules answers ScheduleListRow rows; normalize_schedule and parse_crontab_expr return the typed cadence; SCHEDULE_NAME_KEY moves to the contract; the skeleton's and the CLI's create_schedule drop schedule_kwargs and take the typed cadence; BackupMode is ScheduleImportMode; the backends' ScheduleRecord classes and celery's core/schedule module are removed, and the arq, celery and rq schedule helpers (derive_cron_or_interval, apply_normalized_schedule, crontab_string, interval_seconds) take the typed cadence, and ARQ_SCHEDULE_OPTS, CELERY_SCHEDULE_OPTS and RQ_SCHEDULE_OPTS change shape; BackendDispatchSettings.task_timeout must be positive; tai schedules add drops --schedule-kwargs/--schedule-kw for --every/--cron/--cadence/--schedule-name; derived schedule names change for the same inputs; the arq schedule store must be reset (keys matching arq:schedule:*); celery tasks are cut at task_timeout and no longer retried.
* AppHttp.custom_route gains success_media_types and the source-text media derivation is gone; SHIPPED_FAMILY_BUDGETS is removed and RateLimitSettings.budget_for takes the declared budget; cancel_parks_for_person and rekey_parks_for_merge are replaced by cancel_parks_for_thread(also_subjects=, reason=) with reason required and rekey_park_subjects; the bridge thread prefixes move to tai42_skeleton.conversations.thread_ids; tai42_skeleton.routers.conversations no longer exports reload_gate and tai42_skeleton.routers.interactions.parse is removed (use read_json_object / parse_json_object); the shared 400 texts are "invalid JSON body", "body must be a JSON object" and "invalid <what>: <field>: <rule>"; channel turns refused during a reload answer 503; channel-web's upload family constant moves to routes/limits.py.
* AppInteractions gains callback_url_prefix() and react() takes a FormReactionEvent; a verifier binding is {"verifier", "config"} everywhere (ask, ask_external config, stored interactions) and AskValidation requires its verifier; the skeleton's TopicVerifierBinding and ThreadQueueOverflowError move to the contract; RADIO_OPTION_THRESHOLD, evaluate_visible_when and hidden_fields move from tai42_skeleton.interactions.form_schema to tai42_kit.interactions.form_vocabulary; tools-stripe drops CallbackTargetRefusedError and its public-base setting; a form-reaction event with an extra key is refused; the contract's served documents change.
* **agents:** every parking agent kind parks under a holder and sends its caller ask up
* OperationError and UnavailableError move from tai42_skeleton.operations to tai42_contract.errors; tai42_skeleton.template ContentPart and ResourceManager.normalize_media are replaced by tai42_contract.template.ImageReference and resolve_image; StructuredOutputPlan.bound_typed_dict is removed; LeadingUserMiddleware no longer has before_model/abefore_model; roll_cache_marks requires mark_key; RetrievalToolsGraph requires a provider; default_ttl_seconds is removed from SandboxDispatchSettings, DockerSandboxSettings and SandboxLocalSettings, and a stale SANDBOX_<PROVIDER>_DEFAULT_TTL_SECONDS refuses boot; tai42_e2e.settings.LLM_PROVIDERS changes.
* the run-tool door and a background parked record answer a RunAnswer object discriminated on kind, and AppInteractions.park_answer is replaced by run_answer; the chained-park, park-kill, failed-outcome and suspended-marker helpers in tai42_contract.interactions move to tai42_kit.interactions, and build_inbound_media_params and inbound_media_placeholder to tai42_kit.conversations; the execution-identity registration slots (register_execution_identity_accessor, register_execution_identity_binder, current_execution_identity, bound_execution_identity_for_fire) are removed from tai42_contract.interactions; notify_in_order is removed from tai42_contract.channels; REGISTRY_MUTATING_FLEET_OPS, POOL_TURNOVER_FLEET_OPS and TEMPLATE_EVICTION_FLEET_OPS are replaced by each op's declared effects; deliver_tool_completion takes thread_id in place of delivery_thread_id.
* tai42_contract.agent.SubAgentSpec and tai42_skeleton.agent.SubAgentSpec are removed (the agents plugin keeps its own); PresetSpec.name must match PRESET_NAME_PATTERN; the suspended-interaction marker moves from tai42_contract.interactions.continuation to park_markers, and suspended_interaction_marker requires resume_owner and the keyword-only interaction_ids and caller_interaction_ids; reserved_thread_namespace_error takes the agent class first; CLIENT_TOOL_NAME_MAX_LEN is removed (use TOOL_NAME_MAX_LEN); the agents' run and astream no longer accept free keyword arguments, and claude_code's build_options_payload and build_payload take proxy_tools in place of tool_names; the contract's served documents change.
* **skeleton:** a configuration whose fleet apply window is below three times the tool-runs drain budget refuses to boot, reload or apply (ApplyWindowBelowDrainError); the default apply window is 90 seconds; translate_backend_needs_bus is renamed translate_config_invariants.
* tai42_e2e.settings.REAL_SERVICES gains the connector-slack and connector-github real-service seams.
* **e2e:** TaiStack._wait_backend_census and readiness.wait_backend_census are replaced by TaiStack._wait_fresh_lives and readiness.wait_fresh_lives.
* **e2e:** the browser suite's idempotent probes resend once on a stale keep-alive reset
* the AppAdmin protocol gains reprobe_failed_mcps(titles); tai42_e2e.topology.StackResources takes langfuse_host, langfuse_public_key and langfuse_secret_key at new positions; pass them by keyword.
* tai42_skeleton.connectors.token_injection.call_with_auth no longer takes auth (its parameters are config, transport, tool_name, arguments, mcp_client) and handle_token_expired is removed (folded into with_managed_auth); tai42_skeleton.app.epoch.EpochAdmissionApp is no longer reachable from the epoch module; import it from tai42_skeleton.app.admission, where it is defined.
* ProviderDescriptor.icon_url and ProviderCatalogEntry.icon_url widen from str to str | None; the ConnectionService.complete_connect Protocol takes code: str | None and the new iss and error keywords, so an external implementer of the Protocol must add them; tai42_skeleton.connectors.oauth.client build_authorize_url, exchange_code, refresh and revoke take the required resolved and client arguments.
* tai42_skeleton.app.epoch.attach_boot_serving_app takes a required keyword-only stateless_http argument.
* resolve_trace_context and bind_run_trace raise InvalidRunTraceError for an explicit monitoring_trace_id that is not 32 lowercase hex (not all zeros) or a monitoring_parent_span_id that is not 16 lowercase hex (not all zeros).
* **skeleton:** DELETE /api/auth/principals/{id} of a principal whose login record a provider keeps answers 409 naming that provider's member actions; GET /api/auth/members joins each principal's role (principals[].role) and derives the row role from them. In the contract, MemberEntry.role is removed and MemberPrincipalState.role is required.
* an env write of a recycle-class key is refused on a bare deployment and rolls a fleet recycle on a supervised one, on every env write door; after a converged roll the serving worker exits (drains and is respawned) after a projected tool call too (MCP tools/call, tai tools run, an agent, a preset, a schedule, a hook), and a backend worker exits and is respawned after a job that wrote a recycle-class env key; write_env, set_mcp_secret_env, import_backup, marketplace_install, marketplace_update and marketplace_uninstall return an OperationResponse instead of a plain payload; tai42_skeleton.app.graceful_exit replaces request_serve_graceful_exit, request_backend_graceful_exit and graceful_exit_for with request_graceful_exit(pid); ConfigService.apply_replace_env no longer takes orchestrate; and tai42_skeleton.marketplace.manifest_apply.reload_report is removed (the installer hands its ApplyResult to the operation, which renders it).
* a reload answers its caller on every transport; the bus heartbeat survives the rebuild freeze ([#551](https://github.com/tai42ai/tai42/issues/551))
* the Langfuse e2e tests select their own trace by a time window and their marker ([#550](https://github.com/tai42ai/tai42/issues/550))
* a profile apply reports a stopped recycle roll; the working signal keeps its cadence ([#549](https://github.com/tai42ai/tai42/issues/549))
* an e2e leg proves a door binding save resolves its template references in one batch ([#548](https://github.com/tai42ai/tai42/issues/548))
* an explicit null template default survives the write; a null tool argument reaches its server ([#546](https://github.com/tai42ai/tai42/issues/546))
* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545))
* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540))
* the accounts archive is version 2 and refuses version 1; the accounts plugin drops its role column, guard and lock; the contract gains the admin services.
* the readiness and drain registries live on the app instance; the connector token store's listing and restore seams replace the SQL that backup and re-encrypt carried; the kit's pinning transport constructor changes.
* the states facet and the pending-saves routes change shape; the PUT state answer carries held.
* the trace-detail wire loses type and usage and gains kind and the token fields; an unknown LANGFUSE_ variable refuses boot.
* the two access-control path-list settings default to empty and authenticated_always_allowed_paths is removed.
* the contract's access-control surface drops the unused PolicyEnforcer protocol; route rows change shape.
* the policy enforcer is served by the accessor, not constructed per call; the tool-surface generation is part of the tools facet.
* the park records and the resolution record change shape; the LLM_PROVIDER_CHECKPOINT_TTL_MINUTES and FLOW_CHECKPOINT_RETENTION_DAYS variables are refused.
* the contract's template models are typed and the states facet's render methods change shape; the declarations and templates tables take their version from a sequence; the closed-unit error is typed.
* the engine's checkpoint TTL setting is removed in favour of the platform retention setting; stored checkpoints in the previous format are refused at boot and must be reset; the kit declares redisvl in its llm extra.
* INTERACTIONS_NOTIFICATIONS_* and INTERACTIONS_SEND_RECEIPT_* move to CHANNELS_*; the config env door body is {env, secret_keys}; the recycle constants are removed in favour of TAI_SUPERVISED_PINNED_KEYS; an unknown name under an owned prefix refuses boot.
* the monitoring writer protocol and the recorded shapes change; the plugin's SDK-provided settings names are refused; the collector credential is OTEL_COLLECTOR_LANGFUSE_AUTH.
* **skeleton:** BaseHooksManager.register is concrete and backends implement _store_hook; the door statuses and the backup section order change.
* **kit:** a settings class that sets model_config["env_file"] is refused; get_mcp_transport returns the keep-alive transport type.
* the identity and accounts registration modules move from the contract to the kit; fleet-op handlers are called as handler(op_name, budget); BUS_APPLY_TIMEOUT_* and the kit's env-read budget are removed; a callback job must carry its gate state.

### Features

* a flow declares its own checkpoint life within the platform's ceiling ([#545](https://github.com/tai42ai/tai42/issues/545)) ([77ea6e2](https://github.com/tai42ai/tai42/commit/77ea6e2898d2c8d743920cb4567a61e4748e16fc))
* a transactional state outbox, the after-reply call, pending-save doors and card ([#531](https://github.com/tai42ai/tai42/issues/531)) ([e9af951](https://github.com/tai42ai/tai42/commit/e9af951ea7dd70b8ce9f031aac088a50389ad4f1))
* channels and interactions read the platform's rules from their owners ([fc3b58c](https://github.com/tai42ai/tai42/commit/fc3b58cd65d30847b67d3cd76a3efb1f488e3c1e))
* connectors own their token rows; readiness and drain registries per app; public seams ([#533](https://github.com/tai42ai/tai42/issues/533)) ([1bd4cd1](https://github.com/tai42ai/tai42/commit/1bd4cd1ada2b50977850483eac1908b1c0b5c7e1))
* **kit:** env-file cache, keyed settings, orjson jsonb reads, MCP keep-alive, read_connection ([#516](https://github.com/tai42ai/tai42/issues/516)) ([886da9e](https://github.com/tai42ai/tai42/commit/886da9edd901fd8456212abb320ccea8da0fa2a3))
* monitoring over OpenTelemetry with records stored once and exported through a collector ([#520](https://github.com/tai42ai/tai42/issues/520)) ([ab2000e](https://github.com/tai42ai/tai42/commit/ab2000e7f3f8f30046164e8460546a24b082191f))
* monitoring read side: span kinds and tokens, GenAI messages, references resolved on request ([#529](https://github.com/tai42ai/tai42/issues/529)) ([8ea9e65](https://github.com/tai42ai/tai42/commit/8ea9e650dbfc2114349e64c4c5043906d602aeeb))
* mounted MCP tools keep their server's display fields; real legs for the vendor connectors ([ac3105a](https://github.com/tai42ai/tai42/commit/ac3105a2c4771ac42f340b8f1d0dd54cb4c0ae0b))
* one canonical route path on every door, per-writer policy versions, SSE bare-prefix record ([#525](https://github.com/tai42ai/tai42/issues/525)) ([25f713b](https://github.com/tai42ai/tai42/commit/25f713b9d63b2346ffe33408105b09efcfed64f5))
* one checkpoint retention setting, faithful Redis codec, durability mode, stale-thread sweep ([#519](https://github.com/tai42ai/tai42/issues/519)) ([b261cea](https://github.com/tai42ai/tai42/commit/b261cea384f09450c548c96b0af3efa025d65fe1))
* one declared scheduling protocol; backends never retry tool bodies; one job budget ([b3fdfac](https://github.com/tai42ai/tai42/commit/b3fdfacc7f2b129086736767d51b1c1906474c69))
* one guarded admin service set, the access-control backup section, throttles, providers ([#535](https://github.com/tai42ai/tai42/issues/535)) ([517048b](https://github.com/tai42ai/tai42/commit/517048b5785671777c3e68c969deeb6ea50ac183))
* one policy enforcer per loop, route/config snapshots, tool-surface generation, graph cache ([#526](https://github.com/tai42ai/tai42/issues/526)) ([58934b2](https://github.com/tai42ai/tai42/commit/58934b2010fc047b60012bec310b9dd2e06e5d4f))
* owned env prefixes, channel settings, one owner for secret marks and env markers, pinned keys ([#521](https://github.com/tai42ai/tai42/issues/521)) ([5c5c046](https://github.com/tai42ai/tai42/commit/5c5c04649afdb0866981127ffe0c0424b04af463))
* park index with token-guarded finalize, chain lineage on every resume door, failed terminal ([#524](https://github.com/tai42ai/tai42/issues/524)) ([e59ec7b](https://github.com/tai42ai/tai42/commit/e59ec7b15d868079e6e04b15c9550cd6665262c0))
* routes declare their budgets and bodies; the skeleton's doors share one rule each ([6ea4e8d](https://github.com/tai42ai/tai42/commit/6ea4e8db362b6983a6f2e577c42e87816f69a130))
* routes declare their reach; the server serves the sub-MCP mount, pattern and public id ([#528](https://github.com/tai42ai/tai42/issues/528)) ([1accaf5](https://github.com/tai42ai/tai42/commit/1accaf55e1c312216e13dd275c0661d46bb7ae86))
* **skeleton:** a member's role is joined per principal; a login-owning provider owns the delete ([26a14e9](https://github.com/tai42ai/tai42/commit/26a14e9008dde22a883fefed9017362d3a88d11f))
* **skeleton:** one write service per definition kind for the doors and the restore ([#517](https://github.com/tai42ai/tai42/issues/517)) ([2e4faec](https://github.com/tai42ai/tai42/commit/2e4faec21969d1dcad82bc018e2eb494ff7b603a))
* **skeleton:** the fleet apply window honours the retire's drain budget ([35a3fd4](https://github.com/tai42ai/tai42/commit/35a3fd476bde0a731892a413c50089893517feed))
* staged registries in the kit, fleet-op budgets, callback gate state, lifecycle at build ([#515](https://github.com/tai42ai/tai42/issues/515)) ([7c60c8c](https://github.com/tai42ai/tai42/commit/7c60c8c077aeead4e4ea6181e426c54f293446d1))
* structured output binds the authored schema; provider-keyed history; the sandbox seam ([f2675f8](https://github.com/tai42ai/tai42/commit/f2675f8511fc14a5dcda4b9a63eaa6a85a1949c0))
* the add-server door: discover, add and remove a discovered MCP server and set its client ([d308b40](https://github.com/tai42ai/tai42/commit/d308b40bcf6fe1106fac9f1dbf8ffcabb51758d1))
* the agent surface is framework-neutral: client tools, one run signature, declared parks ([37578fc](https://github.com/tai42ai/tai42/commit/37578fc51dea9232ae4b2bcc095dce3103ba1b72))
* the build-time state trim, narrowed jq inputs, the $__in envelope sealed at the kit ([#540](https://github.com/tai42ai/tai42/issues/540)) ([73f6cae](https://github.com/tai42ai/tai42/commit/73f6caee2a1d1082d06ff7e0d4b8bb229060ca25))
* the connector discovery seam: discovered providers, one resolver on every OAuth door ([34578ef](https://github.com/tai42ai/tai42/commit/34578ef0abc208624d0ac7cc5572225681c7384c))
* the inbound MCP door: protected-resource metadata, the challenge and the request resource ([5aeb9bc](https://github.com/tai42ai/tai42/commit/5aeb9bcd24a454327e88d0464d2a2a26c5e1fad4))
* the interactions seams: the kit owns the park helpers, typed fleet ops, one run answer ([cfa7ba0](https://github.com/tai42ai/tai42/commit/cfa7ba058d85d4c9d7f3b4dcae2eee5ceac39b07))
* the run-time credential path: one managed-auth seam for every MCP call and listing ([f31a9a2](https://github.com/tai42ai/tai42/commit/f31a9a219f8ce174a860e15398d542edf84d4abc))
* typed state templates, version-keyed catalogs, a compare-and-set unit commit ([#523](https://github.com/tai42ai/tai42/issues/523)) ([51f7aa6](https://github.com/tai42ai/tai42/commit/51f7aa6656b23735d8c364b7e3fef2265470ae2e))


### Bug Fixes

* a profile apply reports a stopped recycle roll; the working signal keeps its cadence ([#549](https://github.com/tai42ai/tai42/issues/549)) ([ac2e8c2](https://github.com/tai42ai/tai42/commit/ac2e8c29289607141dd06702c18f4f46597b0f36))
* a reload answers its caller on every transport; the bus heartbeat survives the rebuild freeze ([#551](https://github.com/tai42ai/tai42/issues/551)) ([d2e24ac](https://github.com/tai42ai/tai42/commit/d2e24acefee6444ac87caa1b318f5f6ef43c45bc))
* a retired settings generation is collected; no door holds settings across an await ([#544](https://github.com/tai42ai/tai42/issues/544)) ([18b495b](https://github.com/tai42ai/tai42/commit/18b495b0f3992e8b13aa7c2a1e98939caad014d0))
* **agents:** every parking agent kind parks under a holder and sends its caller ask up ([244c9e6](https://github.com/tai42ai/tai42/commit/244c9e62ca560837d2a6252cae189a16434c3f2c))
* an e2e leg proves a door binding save resolves its template references in one batch ([#548](https://github.com/tai42ai/tai42/issues/548)) ([6ada66c](https://github.com/tai42ai/tai42/commit/6ada66c563318655110ce34b945c6e170049662a))
* an explicit null template default survives the write; a null tool argument reaches its server ([#546](https://github.com/tai42ai/tai42/issues/546)) ([d1fa7e7](https://github.com/tai42ai/tai42/commit/d1fa7e741f6e2be28543ec3f898bf075a689ae0d))
* declared-public routes marked and unmappable; refusals the docs promise; deterministic tests ([#542](https://github.com/tai42ai/tai42/issues/542)) ([f00f65c](https://github.com/tai42ai/tai42/commit/f00f65c565736a4b80caa96624b536a0c66b0319))
* **e2e:** a serve or embed restart waits for its fresh workers on the bus census ([94f6905](https://github.com/tai42ai/tai42/commit/94f6905f4499c68808e0809bc0e74d70ae0048e5))
* **e2e:** the browser suite's idempotent probes resend once on a stale keep-alive reset ([e91cd1f](https://github.com/tai42ai/tai42/commit/e91cd1f6584ef36bcda0a7b1536dcdc96624316d))
* **e2e:** the inbox resync waits for the last interactions stream the page opens ([#584](https://github.com/tai42ai/tai42/issues/584)) ([14c3451](https://github.com/tai42ai/tai42/commit/14c34518f280c461bff17dd0518790d74b5e58c7))
* recycle-class env changes respawn through every door and judge the value a worker bound ([efc13d0](https://github.com/tai42ai/tai42/commit/efc13d0f3e99d95db8bba7500ee3f10c517e97b8))
* refuse a run whose explicit trace id or parent anchor no writer can record under ([62c9265](https://github.com/tai42ai/tai42/commit/62c92653674f92c626c2180630712a5be5a50bd1))
* sub-MCP apps keep the stateless boot mode; the e2e relay keeps idle connections open ([b99af59](https://github.com/tai42ai/tai42/commit/b99af59c800e1cbae98547b17aa9f1c3a695a574))
* the Langfuse e2e tests select their own trace by a time window and their marker ([#550](https://github.com/tai42ai/tai42/issues/550)) ([6936eb3](https://github.com/tai42ai/tai42/commit/6936eb31b67c294419408df9c645ee85a241e93a))
</details>

---
This PR was generated with [Release Please](https://github.com/googleapis/release-please). See [documentation](https://github.com/googleapis/release-please#release-please).