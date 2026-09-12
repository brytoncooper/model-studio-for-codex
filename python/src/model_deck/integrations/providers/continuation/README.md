# Provider continuation

Extracted, behavior-preserving helpers for provider continuation.

- `compaction.py`: verbatim extract of legacy `context_compaction.py`. Uses wall-clock time and random ids via `time`/`os.urandom`; outputs are not deterministic. Summaries are truncated to `MAX_SUMMARY_CHARS`.
- `translate.py`: verbatim-behavior extract of `local_item_id`, `_make_item_foreign_safe`, `sanitize_openai_input`, `heal_rejected_encrypted_item` from legacy `local_router.py`, plus pure `provider_continuation.py` helpers (`LOCAL_ITEM_PREFIX`, `is_local_item_id`, `_item_identity`, `ContinuationError`). `_make_item_foreign_safe` mutates its argument in place by design.
- No legacy runtime imports; only stdlib plus the sibling `compaction` alias.
- No scope-key rewrite, no Cursor changes, no legacy rewiring.
- Tests in `python/tests/providers_continuation/` assert preserved fixtures for compaction summaries and encrypted-item stripping/healing only; they do not prove full-route parity.

To extend these helpers, keep provider wire cleanup here and inject storage or
host-specific decisions at the caller. Preserve unrelated items and original
request bytes when no transformation applies. Add a fixture for each changed
wire shape, including a case that must remain unchanged. Durable continuation
ownership and provider routing belong to their own adapters.

From `python/`, run `PYTHONPATH=src python -m unittest discover -s tests/providers_continuation`.
The six tests are registered with unittest so repository verification executes
them without requiring a separate test runner.
