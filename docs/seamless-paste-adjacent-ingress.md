# Adjacent image ingress research

Date: 2026-07-14
Scope: mobile sharing, drag-and-drop, and clipboard history only. This report
does not change the one-key paste default or authorize an implementation graph.

## Recommendation

Rank the adjacent paths in this order:

| Rank | Ingress | Audience value | Feasibility | Privacy risk | Reuse of core substrate | Decision |
|---:|---|---|---|---|---|---|
| 1 | Drag a local image onto a managed Ghostty pane | High for screenshots and Finder assets | High on macOS | Low when the drop itself is the gesture | Typed snapshot, route registry, transfer, adapter, receipts | Next candidate after core closeout |
| 2 | Mobile share sheet to an operator-owned relay | Medium-high for phone photos and screenshots | Medium; needs pairing and offline queue semantics | High because it introduces a network receiver and mobile credential lifecycle | Artifact receiver, digest checks, route selection, cleanup | Separate opt-in graph only |
| 3 | Explicit clipboard-history picker | Medium for repeated assets | Medium | High; history storage conflicts with the no-polling/no-retention default | Adapter and transfer only; capture and authorization differ | Do not build into core |

## 1. Drag-and-drop

The best extension is a repo-owned macOS drop target scoped to a managed
terminal surface. A drop is already an explicit user gesture and supplies file
URLs without reading unrelated clipboard state. The target should capture the
same stable terminal or pane identity as paste, validate one or more files,
then call the existing content-addressed transfer and agent adapter. It must
not interpret filenames as shell text or synthesize Enter.

Open questions for a separate graph are Ghostty surface identity, multi-file
ordering, hover/focus races, and whether a small overlay can remain completely
inactive outside a drag session. Acceptance must include uninstall and proof
that normal application drag-and-drop is untouched.

## 2. Mobile share sheet

A mobile sender is valuable when the source never reaches the Mac clipboard.
The safe shape is a separately paired, operator-owned relay on the Tailnet or a
local Mac receiver—not a public upload endpoint. The mobile share gesture
authorizes one artifact; the receiver should issue a short-lived nonce, bind it
to a chosen route, and require an explicit terminal focus confirmation before
injection. An offline upload may be retained only in an encrypted, bounded
queue and must never guess the eventual pane.

This path adds pairing, revocation, device loss, background networking, and
multi-device routing concerns. It therefore needs its own threat model and
Beads graph. It must reuse the artifact protocol without changing the desktop
paste default.

## 3. Clipboard history

Automatic clipboard history is intentionally incompatible with the core
contract: the core reads non-text bytes only after a paste gesture and retains
no general history. A future picker could be safe only if the operator
explicitly enables retention, chooses the items to store, sees age/size, and
can purge them. OS-native history may be referenced by an explicit selection,
but Skillbox should not poll or mirror it in the background.

The likely value does not justify the new sensitive-data store today. Keep
`clipimg-put` and ordinary OS history as explicit recovery paths; revisit only
after measured user demand.

## Reusable boundary

All three candidates may reuse media validation, content-addressed storage,
exact route registration, attachment adapters, bounded receipts, TTL/quota
cleanup, and fail-closed injection. None may reuse a stale route, bypass an
explicit gesture, start a public listener, or silently turn retained content
into an agent attachment. Each implementation requires a separate plan and
live proof; this report is the complete deliverable for the research bead.
