# Local verification — version 1.5 — 2026-09-11

Historical development QA record for one local installation. These results are not a compatibility guarantee for other accounts or app versions. Earlier sections describe earlier builds, not necessarily current behavior. No hosted CI or general release certification is claimed.

## Unified catalog and responsive window

- Corrected obsolete runtime selection: validated ChatGPT.app is preferred over legacy Codex.app. Current runtime is codex-cli 0.153.4; the old runtime omitted Astra. Catalog, usage, bridge, and launcher now share discovery.
- Independent finalizer passed all 68 tests, optimized build, plist, deep strict signature, and unchanged credential-helper hash. Read-only catalog matches six native visible models (including Astra) and seven registered OpenRouter models.
- Current-runtime mock inference checks preserved two-turn same-provider history and rejected cross-provider turn/settings changes. Global config bytes were unchanged. No paid inference or existing user-task migration was performed.
- Installed to Applications; installed deep strict signature passed. Parent inspected actual native Settings UI: Astra visible, Qwen search filters to two entries, provider/usage details update, and compact/expanded window layouts resize and scroll without horizontal clipping.
- Actual Codex dropdown remains unverified: computer-use access to the host app is restricted. Source supports ordinary visible-model filtering, but account/internal filtering may differ. User-controlled host restart through Model Studio remains necessary to activate the updated bridge.
- Existing local Codex tasks can continue on their original provider. Cross-provider migration and conversion of ordinary ChatGPT chats are not implemented. The submitted screenshot displays a Full access permissions warning, not evidence of an inference error.

## Version 1.4

## Clear model workflow and richer usage

- Models now reads actual local registrations via the new read-only `list_models` operation. The UI shows how to launch/use a selected model, copies its precise delegation request only on explicit action, and separates adding another model from using one already configured. Saved shortcut data is preserved but its competing UI is removed.
- Usage groups simultaneous windows by stable pool ID, uses provider display names, and explains General/Spark versus unknown pools without inventing model eligibility. Percentages are not added or converted to dollar costs.
- Added latest 14 available daily token records, an accessible native chart, peak/lifetime token counts, streaks, reset countdowns, OpenRouter USD tiles, key-cap details, and separate BYOK reporting. Missing data remains unknown; duplicate/malformed history is rejected.
- Independent finalizer: 54 tests, optimized Swift build, plist and strict signature passed. The credential helper retained its exact prior hash. A read-only live collector returned both providers, four windows, 14 daily records and all three added summary fields in 1.20 seconds.
- Parent actual installed-app QA: seven registered models displayed; choosing a different entry changed its natural-language usage prompt; reasoning disclosure opened/closed; explicit usage refresh displayed current limits, the 14-day chart, streak metrics and USD spending. No credential prompt or model inference was triggered.
- One post-QA display-only repair prefers an unknown pool's supplied name over its internal ID (for example, `gpt-reserve` instead of `base_model_inference`). Raw IDs remain secondary metadata.
- Source/review boundaries: routing, native agent registration behavior, global provider configuration, and credential helper remain protected. Main Desktop model-picker restart remains a separate verification boundary.

## Version 1.3

## Model Studio and usage

- Redesigned Overview, Models, API keys, Usage, and Advanced as separate scrollable native pages.
- Independent finalizer passed the 35 existing tests and 11 usage tests, including missing values, explicit unlimited versus unknown key budgets, sanitized errors, provider independence, and fixed-endpoint/no-redirect behavior.
- Optimized Swift build, plist validation, strict signature, and stable credential-helper hash passed. Helper SHA-256 remains `f425e401e11f0474de16bd865f72837680819664c0b7e81ed0943c0677a0338f`.
- A read-only live collection succeeded for both providers in 1.21 seconds, returning four subscription windows. No inference was requested and no keys or raw account data were printed by the verifier.
- Actual installed-app UI inspection covered all five pages. Usage displayed live subscription progress bars/reset dates and selected-key OpenRouter USD spending. The inspection identified a long-label layout issue; wrapping headings and equal-width cards were added for the final build.
- Final installed build was reopened and refreshed successfully: equal-width provider cards, wrapping window titles, selected navigation accent, and visible footer were confirmed in the actual native screenshot. The Usage page was left open for the user. Installed strict signature passed.
- No global routing configuration or credential-helper source changed. Desktop mixed-model picker verification still requires an integrated Codex restart; settings UI verification does not prove that separate boundary.

Implementation and review: one Swift UI worker, one isolated usage collector/test worker, primary-agent source and visual review, and one independent aggregate finalizer. No unrelated work was modified. Older sections below retain historical version-specific evidence.

## Version 1.2

## Mixed-provider picker bridge

- Independent finalizer passed 35 tests across registration, registry, and bridge behavior.
- Actual native app-server integration sent turns to two separate local mock endpoints under the requested models/providers; prohibited cross-provider changes produced no further inference requests.
- Virtual picker-default writes kept real global config bytes unchanged.
- Built launcher passed CLI version passthrough, stdio initialize/model-list/config-read, seven real registry catalog additions, and EOF shutdown. No real key was requested in these checks.
- Explicit, config-override, and effective-config developer instructions were preserved before the routing guide.
- Strict deep signature passed after running the built bridge. Python runs with `-B` so imports do not add unsealed cache files inside the signed app.
- Stable credential-helper hash remained unchanged; updated app installed and signature/hash checked again.
- Six explicit subscription roles were installed (Astra, Sol, Terra, Luna, GPT-5.5, Spark) without changing global config bytes or reading credentials.
- Earlier live Qwen/OpenRouter lead successfully spawned an OpenAI/Luna child, waited for `SUBSCRIPTION_CHILD_OK`, and returned that answer. The CLI was signed in through ChatGPT; the child role explicitly selected built-in `openai`. Not every registered model has been live-tested.

Remaining user-controlled check: quit Codex when safe, then click **Launch Codex with mixed models** in OpenRouter Settings and verify the real Desktop picker. Native protocol catalog output is verified, but Desktop account-specific filtering and the rendered dropdown have not yet been verified. Existing-task cross-provider switching is intentionally rejected because native unload/resume can reset permissions.

## Keychain prompt-loop hotfix

The first live OpenRouter check failed before inference: external auth timed out repeatedly at five seconds and OpenRouter returned 401. The UI executable's ad-hoc designated requirement was a code hash, so rebuilding changed its credential identity. The test and settings app were stopped.

Keychain operations now belong to a separate helper whose signed bytes survive UI rebuilds. All unattended token reads disable interaction. Foreground Save and Check actions explicitly permit authorization. No ACL was weakened and no credential moved into plaintext storage.

Independent finalizer passed all 14 backend tests, two builds with identical helper SHA256 (`f425e401e11f0474de16bd865f72837680819664c0b7e81ed0943c0677a0338f`), strict deep signature validation, plist, disposable helper and wrapper Keychain round trips with cleanup, and noninteractive nonexistent-key failures with zero stdout in milliseconds. Updated app installed; installed helper hash and signature rechecked. Existing real-key authorization remains a user-controlled foreground step; no further real-key retrieval was attempted.

The app now registers native per-model Codex agent roles instead of switching the global provider. A bounded backend worker owned Python implementation/tests, the lead owned Swift integration and review, and one independent finalizer owned acceptance.

## Passed

- All 14 isolated backend tests: legacy recovery plus native registration, byte-for-byte global config preservation, input validation, OpenAI rejection, ownership collisions, symlinks and repeat updates.
- Optimized Swift build, plist lint, ad-hoc signature verification.
- Disposable Keychain credential save/read and private command-auth subprocess retrieval; disposable value removed.
- Installed Codex CLI 0.145.0-alpha.18 emitted a native collaboration event and sent the child to its configured mock endpoint/model while the parent retained its separate mock endpoint/model.
- Additional priority service-tier probe: custom-provider request bodies omitted the inherited priority field.
- Seven saved non-OpenAI model roles registered; registration asserted real global configuration remained byte-identical. The default remains OpenAI / gpt-5.6-sol.
- Updated app installed in Applications and relaunched; actual accessibility state confirms native registration controls and unchanged OpenAI default.
- Installed executable/backend match the verified build; strict installed signature passes. All seven generated roles have private 0600 permissions.
- A fresh native CLI automatically discovered all seven standalone roles in its spawn schema, without explicit per-role command-line registration. The probe invoked only a mock child, not a live OpenRouter role.

## Boundaries

The routing probe used local mock inference endpoints, not paid OpenRouter inference. Authentication and tool compatibility for each saved model are not established by catalog membership or role registration. A restarted Desktop session still needs an end-to-end delegated request to establish live behavior. No automatic restart interrupted the user's active Codex tasks.

Native roles are distinct from the main model picker, which this utility does not alter. The app remains locally ad-hoc signed and depends on the recorded Python runtime. Version 1.0 global apply remains available only in the backend for compatibility; the UI no longer invokes it.
