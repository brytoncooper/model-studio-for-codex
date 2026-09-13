# Cursor compatibility preparation

The Architecture checkout exposes one reusable preparation boundary in
[`cursor_agent.py`](cursor_agent.py):

```python
payload, aliases = build_cursor_payload(request, metadata, api_key)
```

The helper prepares one new Cursor generation. It starts no SDK process, creates
no manager/session, and does not mutate request or metadata inputs. It reuses
`reject_encrypted_agent_messages`, `local_router.flatten_tools`, and
`_prompt_message`; it does not duplicate prompt, image, alias or compaction logic.

The returned payload retains exactly the existing fields: `model` (without the
`cursor/` prefix), `api_key`, `tools`, `message`, `reasoning`, and `service_tier`.
Absent reasoning/tier remain `None`. The existing SDK broker still interprets
reasoning and Fast settings. Aliases retain their legacy shape:
wire name → `(namespace, original_name)`. Mapping that identity to an authorized
engine tool name belongs to later host composition, not this helper.

The helper preserves existing validation: reject encrypted inter-agent messages,
unsupported tools through the shared flattening helper, tools without thread
identity, non-Cursor model IDs, and remote compaction triggers. Tool choice `none`
removes callback tools. Local summarization framing and supported image handling
remain in the existing prompt helper. The payload includes a private credential;
only the trusted process-factory composition should receive it.

`CursorAgentManager._session_for` calls the helper only for new generations.
Paused callback reuse, identity/cancellation checks, session ownership, process
construction and cleanup remain with the manager. Its early encrypted-message
check also remains, protecting the callback-reuse path.

## Intentional failure-order correction

The manager now completes pure preparation **before** closing superseded paused
sessions. Previously, tool/model/compaction checks happened first, but prompt/image
validation happened after those sessions were closed. A rejected new image or
prompt now leaves the prior paused run available for a valid continuation. This
change was explicitly approved; successful requests still supersede the same
sessions before starting their new process.

## Verification and remaining integration

Run the isolated legacy fixtures from this checkout:

```sh
python -B -m unittest test_cursor_agent
```

The 23 tests cover existing callback routing plus direct helper payload/defaults,
reasoning/tier preservation, no process creation, input nonmutation, validation,
compaction framing, manager delegation, and invalid-prompt continuation safety.
No live SDK, network request, installation or app operation is exercised.

Later compatibility composition can inject this helper into the accepted
`CursorProcessRuntime.prepare_payload` callback. Trusted captured request context,
authorized alias identities, usage normalization, normalized engine options and
bootstrap wiring remain separate work. This extraction does not claim full B14
integration or change engine schemas.
