# Attached sidebar: live Mac qualification

The attached sidebar requires a user-controlled live check. Automated checks use synthetic window geometry and must not be represented as proof of real ChatGPT window following.

## Permission

- Install the final signed build before granting permission. The local build is ad-hoc signed; later rebuilds may require macOS Accessibility reauthorization.
- Open Model Deck's companion and explicitly enable window following. Grant Model Deck access in System Settings → Privacy & Security → Accessibility yourself.
- macOS grants broad Accessibility access. This implementation reads the host window's geometry, role, focus, minimized state, and application visibility, and changes its position/size to reserve or release sidebar space. It must never read chat contents or modify task state.
- Deny or revoke access once to verify the sidebar reports the missing permission and remains recoverable from the standalone settings window.

## Window behavior

1. Move and resize a normal ChatGPT/Codex window. The sidebar should track its edge and height.
2. Collapse and expand. The sidebar keeps the host height. Expansion should narrow ChatGPT to reserve space; collapse should return the width to ChatGPT while retaining the narrow rail.
3. Test a large window and a window near its minimum size. If macOS refuses the requested size, the app must report the failure and not claim that it successfully reserved space.
4. Open a second host window and switch focus. The sidebar should follow the current eligible window.
5. Minimize or hide the host, switch to another app, and return. It should hide and return appropriately, without stale placement over unrelated apps.
6. Test another display, including a display above or left of the primary display, and disconnect a display. Confirm coordinate conversion and recovery.
7. Test fullscreen and separate Spaces. These need device qualification; do not infer support from ordinary window tests.

## Embedded controls and safety

- Models, API keys, and Usage should open inside the expanded sidebar, not spawn a second settings window.
- Open full settings, then return to the sidebar. Selections and feedback should remain consistent; no duplicate controls should appear.
- Opening the sidebar must not restart the host, request inference, refresh spending automatically, or request a saved key.
- Adding a model should leave a truthful restart reminder, not silently restart ongoing tasks.
- Hiding the sidebar and revoking permission must leave full settings accessible.
- Manually resize or move the host after attaching. Subsequent layout changes must use the current geometry, not overwrite the manual layout with an old snapshot. Disabling/quitting should restore reserved space only when the geometry is still owned by Model Deck; otherwise it should avoid overwriting user changes.

Record actual pass/fail observations separately from pure geometry tests and build checks.

## Recovery limits

If a resize or restore is refused, retry explicitly after enlarging the host window or restoring Accessibility permission. Polling must not repeatedly resize a failed window. If cleanup is still unresolved, attaching to another window must wait rather than forget the first reservation.

Restoration is best effort: a terminated host, revoked permission, or quitting Model Deck while the host refuses changes can leave the host narrower. Resize it manually in that case. Recovery must never overwrite a window position or size you changed yourself.
