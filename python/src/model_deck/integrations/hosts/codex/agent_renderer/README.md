# Codex managed-agent renderer

Pure construction of Codex managed agent TOML from caller-resolved inputs.
This package owns rendering only. It never touches the filesystem, home
directories, credential helpers, the network, or legacy modules at runtime.

## Entrypoint

render_managed_agent takes a RenderRequest and returns a RenderedAgent.

RenderRequest carries explicit provider_model_id, display_name,
reasoning_effort, and registration kind (endpoint or subscription). Endpoint
requests also carry endpoint_name, base_url, optional credential_account_id,
absolute token_helper_path, and caller-resolved billing_description.
RenderedAgent carries a relative filename plus UTF-8 TOML content.
Registration identity and path collision handling stay with the consumer.
This renderer performs no existence or ownership checks.

## Legacy parity

Construction mirrors the legacy register_agent and
register_subscription_agent paths: managed marker first line, role slug and
hash rule, model provider fields with token helper args, timeout 5000 and
refresh interval 300000, default effort mapping to low for endpoints, and
subscription documents with the openai provider and no provider table.
Validation preserves the legacy rules, including the gpt and codex name
reservation and the openai subscription redirect. Constants are extracted
copies; legacy modules are never imported. display_name is validated and
left to the consumer for picker labeling; it is not embedded in the TOML,
which preserves legacy bytes.

## Verification

Tests parse rendered bytes with tomllib and compare endpoint, keyless local,
Cursor, and subscription fixtures against extracted legacy expectations.
Rendering requires tomlkit 0.13.3 as pinned in build.sh. With no network
installs, run the suite with the existing read-only vendor directory on the
Python path, from the Architecture worktree root. See the test module header
for the exact command.
