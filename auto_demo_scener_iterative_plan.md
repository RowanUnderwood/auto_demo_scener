# Auto Demo Scener: Iterative Tool-Driven Generation Plan

Date: 2026-10-18  
Repository: https://github.com/RowanUnderwood/auto_demo_scener  
Reviewed baseline: `f990d03a3c54874a9cf19a7e822980d90e7295a9`  
Status: implementation proposal; no application changes or performance measurements have been made.

## 1. Goal and recommendation

Add an optional iterative generation mode that lets the model write a demo, run it, inspect diagnostics and visual output, make targeted changes, and submit a validated revision. Preserve the current generation mode for fast operation and comparison.

Evolve the existing Flask application, browser display, validator, and archive pipeline. A wholesale rewrite or a general-purpose coding-agent framework is unnecessary for the first implementation.

The expected benefit comes from feedback, continuity, and selective editing. Native tool-call syntax alone will not improve demos. More turns are useful when each turn can resolve a specific uncertainty about the result.

Primary goals:

- Recover from runtime and shader failures without losing the original brief.
- Improve demos that run successfully but have weak composition, motion, contrast, or performance.
- Preserve working code and restore earlier revisions when experiments fail.
- Keep generation bounded enough for unattended kiosk operation.
- Support provider differences without assuming that an OpenAI-compatible endpoint implements native tools or every multimodal input format.

## 2. Findings in the current implementation

The application already has a useful feedback loop: generation, browser audition, and a configurable number of repairs. Ninfer can also receive a recording for a visual-improvement pass.

| Area | Observed behavior | Proposed change |
| --- | --- | --- |
| Repair context | `_do_repair()` creates a fresh request containing the error and latest HTML. | Preserve the original brief and a compact record of attempted fixes. |
| Code changes | Repairs and video improvements regenerate complete HTML files. | Permit exact, revision-checked edits as well as complete writes. |
| Visual review | `_do_video_improve()` receives a recording and HTML and immediately produces a replacement. | Separate observation from editing; inspect the revised output afterward. |
| Revision acceptance | A visual revision replaces the original when runtime validation succeeds. | Also check visual regression; preserve a working fallback. |
| Validation timeout | `_audition()` treats a missing response as OK. | Return an infrastructure timeout and retry or fall back. |
| Diagnostics | Browser audition reports a broad result and an error string. | Return structured JavaScript, shader, rendering, and timing evidence. |
| FPS | The probe counts its own animation callbacks. | Distinguish browser heartbeat rate from actual demo rendering activity. |
| Visibility | The probe samples a 4-by-4 pixel grid. | Treat this as a weak signal; add visual inspection and better sampling. |
| API client | Streaming yields content deltas, while non-streaming returns content text. | Add a response representation that retains tool calls, finish reasons, and usage. |

These observations come from code inspection, not an execution benchmark. Refresh the baseline before implementation if upstream has changed.

## 3. Scope of the first release

Include stock, creative, and update generation modes. Keep replay behavior and standalone archived HTML compatible with the existing player.

Keep the retro editor presentation: show initial code as it streams, display edits as they are applied, and use short status messages such as `RUNNING CHECK`, `INSPECTING MOTION`, and `APPLYING FIX`.

Do not expose shell execution, arbitrary filesystem access, package installation, or archive deletion to the model. The required tools operate only on the active demo and its observations.

For the first release, run previews through the existing display browser. This measures behavior on the actual kiosk hardware and avoids requiring a second browser process on a Raspberry Pi. Headless batch evaluation can be added later if useful.

## 4. Proposed architecture

### Application responsibilities

The orchestrator chooses the mode and model, starts a session, enforces budgets, coordinates the display, and archives the final result. A new session runner manages the model/tool loop. A restricted dispatcher implements demo tools using the existing audition and recording infrastructure.

The model proposes actions. The application owns source revisions, validates arguments, executes tools, and decides whether a revision is eligible for display and archiving.

### Session state

Maintain the following outside the model conversation:

- Session ID, provider, model, original prompt, and generation mode.
- A short creative brief, including constraints that must survive repair.
- Current source and monotonically increasing revision ID.
- Last runtime-valid revision and, when available, last visually accepted revision.
- Diagnostics and visual observations keyed to the exact revision tested.
- Brief change history: observed problem, change attempted, and result.
- Turn, tool-call, output-token, elapsed-time, and video-review budgets.
- Cancellation state and terminal reason.

For creative mode, let the initial response establish a concise concept before writing code. Preserve that concept throughout the session. For update mode, retain the source demo and the requested relationship to it.

## 5. Tool contract

Use a small tool set. All source mutations and observations must identify their revision. Reject stale edits rather than silently applying them to a newer document.

| Tool | Arguments | Result and rules |
| --- | --- | --- |
| `get_environment` | None | Three.js version, supported imports, viewport, pixel ratio, renderer capabilities when available, and configured performance target. Return only verified capabilities. |
| `read_demo` | `revision`, optional `start_line`, `end_line` | Numbered source and revision. Limit response size. |
| `write_demo` | `expected_revision`, `html`, `change_summary` | Create or replace the current document atomically; return the new revision. Use revision 0 for the initial empty document. |
| `edit_demo` | `expected_revision`, `old_text`, `new_text`, `change_summary` | Replace one exact occurrence. Reject zero or multiple matches, empty search text, or stale revision. Return the new revision. |
| `run_demo` | `revision`, `seconds` | Run the exact revision and return structured validation evidence. Enforce a server-side duration cap. |
| `inspect_demo` | `revision`, `kind`, bounded capture options | Capture screenshots or a short video from the active preview. Return the capture's revision and timing. Only advertise supported modalities. |
| `finish_demo` | `revision`, `summary` | Request completion. Reject untested or failed revisions. Application performs final eligibility checks and owns archiving. |

Use separate native tool definitions with strict argument validation. The dispatcher can use a common internal envelope, illustrated below; this envelope is also suitable for a JSON-action fallback.

```json
{
  "action": "edit_demo",
  "arguments": {
    "expected_revision": 3,
    "old_text": "uniforms.u_speed.value = 0.1;",
    "new_text": "uniforms.u_speed.value = 0.5;",
    "change_summary": "Increase tunnel motion after preview showed almost no movement."
  }
}
```

Successful results should include `ok: true`, session ID, revision ID, and relevant data. Failures should include `ok: false`, a stable error code, and a concise explanation the model can act on. Never apply a partially parsed write or an incomplete streamed tool call.

Start with one edit per call. Atomic batches of non-overlapping edits can be added after the basic path is reliable.

## 6. Interaction loop and stopping rules

1. Supply the original brief, environment, tool descriptions, and budgets.
2. Let the model create the initial implementation.
3. Execute requested checks and return actual results.
4. Let the model diagnose and edit, then run the changed revision again.
5. If supported and useful, inspect visual output and perform a polish pass.
6. Compare the polished output with the previous working revision.
7. Accept `finish_demo` only for an eligible revision; otherwise return actionable feedback while budget remains.
8. On budget exhaustion, use the best eligible revision or discard if none exists.

The application must require a run after every source change. It may enforce this when `finish_demo` is requested, even if the model forgets to ask for one. A model's claim that a demo works is not validation evidence.

Suggested starting limits, to be tuned by measurement:

| Budget | Initial value |
| --- | --- |
| Model responses | 8 per session |
| Total tool executions | 24 per session |
| Repair edit rounds | Up to 3 after the initial implementation |
| Visual polish rounds | 1 |
| Video inspections | Up to 2: before and after polish |
| Ordinary audition | 5 seconds |
| Requested extended audition | Up to 15 seconds |
| Video inspection | 6–10 seconds by default, with a configurable cap |
| Overall session deadline | 10 minutes, configurable for slower local models |

A model response can contain multiple tool calls, but dependent actions must execute in order. If two writes use the same expected revision, the second should fail as stale. Do not parallelize mutations against the same demo.

Stop early when the demo satisfies the brief and checks. Also stop repeated identical edits or repeated identical failures when no new evidence is being gathered. User skip/discard must cancel pending work and prevent late results from being archived.

## 7. Strengthen runtime validation first

### Correlate browser responses

Give every audition and capture a request ID, session ID, and revision ID. Accept responses only from the designated display connection and active iframe. Ignore stale responses from previous loads or other tabs.

The current validation synchronization uses shared state. Longer sessions make accidental cross-session or multi-tab responses more consequential, so correlation belongs in the first milestone.

### Return structured evidence

Collect:

- JavaScript exceptions and unhandled promise rejections, with useful stack information.
- Bounded console warnings and errors.
- Shader compile and program-link failures with logs where available.
- Heartbeat rate, draw activity, viewport, WebGL context loss, and load status.
- Frame-time samples tied to actual rendering where feasible.
- Visibility and temporal-change indicators, with confidence limitations.
- Explicit load timeout, audit timeout, cancellation, and disconnect states.

Do not label heartbeat frequency as the demo's rendered FPS. Shader diagnostics may require hooks around WebGL compilation/linking or supported Three.js diagnostic hooks; verify them against the vendored version.

### Improve visibility and motion checks

Use downsampled frames or broader sampling rather than only 16 locations. Compare frames over time to detect likely freezes, while allowing slow-moving scenes and intentionally sparse visuals. Treat dark or low-motion output as a reason for inspection rather than an automatic aesthetic failure.

Separate validation instrumentation from performance conclusions. The current probe forces `preserveDrawingBuffer` and uses pixel readback, both of which can affect rendering cost. Keep measurements consistent between modes and document that overhead.

### Handle infrastructure failures correctly

Replace timeout-as-OK with an explicit infrastructure result. Retry once after reconnecting or reloading when appropriate. Do not ask the model to rewrite a demo merely because the display disconnected. If validation remains unavailable, do not mark the candidate as validated.

## 8. Visual review and regression handling

Ask the model to identify at most three concrete issues grounded in the captured output. Examples: an overexposed center, an object clipped by the camera, imperceptible movement, unreadable contrast, or a long static opening.

Require a proposed change linked to each issue. Permit a no-change decision when the demo already works well. Avoid a prompt that requires a rewrite regardless of quality.

After polish, run and inspect the new revision. Compare against the previous working version using the same viewport, capture timing, and seed where the demo supports reproducibility. State uncertainty when random scene variation prevents a clean comparison.

Acceptance criteria:

- The core effect and original constraints remain intact.
- Runtime validation still passes.
- The stated visual problem appears improved without an obvious regression.
- Performance stays within the configured target, accounting for measurement limits.

Prefer the previous accepted revision when the comparison is inconclusive or the change regresses. Automated aesthetic judgment is imperfect; record the choice so the user can review it later.

Use screenshots for layout, framing, and color. Use video when motion or timing matters and the selected endpoint supports it. Verify modality support for the actual provider/model combination. A 2 fps review can reveal broad motion but cannot establish smooth high-frame-rate animation.

For the first implementation, support captures from the existing WebGL canvas. If a demo uses multiple canvases or DOM overlays, report that capture limitation rather than implying the recording shows the entire composition.

## 9. API client and provider compatibility

Extend `lm_client.py` to return structured assistant responses, including content, tool calls, finish reason, and token usage when supplied.

For native streaming tools:

- Assemble tool argument fragments by tool-call index.
- Preserve tool-call IDs and names.
- Wait for complete arguments and a completed response before execution.
- Append the assistant tool-call message and matching tool-result messages correctly.
- Keep multimodal tool observations compatible with each provider's supported message format.
- Preserve provider-required reasoning metadata when necessary; do not assume all reasoning fields are interchangeable.
- Treat length termination and incomplete JSON as explicit failures, not complete code or tools.

Use a per-provider/model capability setting: `native_tools`, `json_actions`, or `disabled`. An optional small probe can help select a mode, but merely accepting a `tools` parameter is not proof that tool use works correctly.

JSON fallback should request one validated action object per response and feed its execution result into the next turn. Use the same dispatcher and budgets as native tools. Allow one corrective format response within the overall limit, then stop or fall back. Never execute arbitrary prose as an action.

Keep the existing content-only client functions for the current mode and inexpensive title generation.

## 10. Context and output budgeting

Keep source files authoritative in application state. The model can read selected sections when needed. Preserve the brief, latest relevant evidence, and a compact change history rather than retaining every complete source revision.

Before each request, reserve room for tool definitions, the expected response, and multimodal input. The current text estimate based on characters divided by four is insufficient for video and tool-heavy conversations. Use provider tokenization when available; otherwise use conservative estimates and lower capture resolution, duration, or count.

Do not truncate HTML mid-file to make a request fit. Reduce old observations and obsolete source snapshots first. If conversation compaction is needed, rebuild a valid message sequence from application state; do not leave orphaned tool results or incomplete assistant/tool exchanges.

Do not repeatedly include the full stock-effect CSV after the creative concept has been chosen. Send relevant constraints and examples, preserving the selected idea.

Track length termination explicitly. Never accept a truncated write as a complete candidate; ask for a smaller implementation or a valid targeted edit if budget remains.

## 11. Configuration and interface

Add an optional `generation_strategy` setting with `classic` as the initial default and `iterative` as the new choice.

Expose:

- Interaction mode: automatic, native tools, or JSON actions.
- Maximum model turns, total tool calls, and session deadline.
- Maximum repair rounds and visual polish rounds.
- Visual inspection enablement and capture limits.
- Target viewport/resolution and performance threshold.

Snapshot generation settings when a cycle starts so mid-session config edits do not unexpectedly switch the provider or model. Apply ordinary changes next cycle; preserve immediate skip/discard behavior.

Show the active step and revision in the retro status bar. Render applied source in the editor rather than displaying raw tool JSON. If existing code-typing delays make multiple edits sluggish, apply the minimum initial typing delay once rather than to every tool response.

When iterative visual review is enabled, it replaces the existing post-display video-improvement pass for that session. Avoid running two overlapping improvement systems. Keep the old option available in classic mode.

## 12. Files to change

| File | Planned responsibility |
| --- | --- |
| `ai_demoscener/orchestrator.py` | Strategy selection, session lifecycle, cancellation, final display and archive integration. |
| `ai_demoscener/agent_runner.py` (new) | Bounded interaction loop, conversation state, context management, stopping rules. |
| `ai_demoscener/demo_tools.py` (new) | Validated tool definitions, revision store, exact edits, audition/capture dispatch. |
| `ai_demoscener/lm_client.py` | Structured responses, streamed tool assembly, finish reasons, provider adapters. |
| `ai_demoscener/validator.py` | Correlated observations, richer diagnostics, capture and validation synchronization. |
| `ai_demoscener/app.py` | Route and WebSocket changes for correlated requests and results. |
| `ai_demoscener/static/display.js` | Execute auditions/captures, relay structured diagnostics, present iterative activity. |
| `ai_demoscener/static/config.html` and `config.js` | Strategy and budget controls. |
| `ai_demoscener/config.py` | Backward-compatible defaults and iterative system prompts. |
| `ai_demoscener/archive.py` and `stats.py` | Optional session metadata and separate comparative metrics. |
| `README.md` | Configuration, provider limitations, workflow, and troubleshooting. |

Keep the session runner small and independent enough to exercise with simulated provider responses and tool results.

## 13. Implementation sequence

### Milestone 1: Reliable feedback and continuity

- Correct timeout handling and add request/session/revision correlation.
- Return richer runtime diagnostics.
- Retain the original brief and attempted-fix summary in repairs.
- Save a working fallback revision.

Deliverable: better repair behavior using the current request style, before native tools are required.

### Milestone 2: Minimal tool-driven loop

- Add structured API responses and a restricted dispatcher.
- Implement read, write, exact edit, run, environment, and finish tools.
- Add the JSON-action adapter and verify native tools on a chosen backend/model.
- Enforce revision checks, budgets, cancellation, and completion rules.
- Add the strategy toggle and retro status updates.

Deliverable: an optional iterative runtime-repair mode with no required video support.

### Milestone 3: Visual iteration

- Add bounded visual captures for supported providers.
- Separate critique, polish, and post-polish inspection.
- Compare revisions and retain the earlier version on regression.
- Prevent duplicate classic/iterative video-improvement passes.

Deliverable: one complete visual feedback loop, including inspection of the revision it produced.

### Milestone 4: Evaluation and tuning

- Record comparable session metrics.
- Run paired trials against classic mode.
- Tune budgets and select defaults based on results.

Deliverable: evidence for whether iterative mode improves this project's demos enough to justify additional latency.

## 14. Verification and acceptance criteria

Use focused tests for the new stateful behavior:

- Fragmented streamed tool arguments assemble correctly; partial calls never execute.
- Tool-call IDs pair correctly with results.
- Exact edits reject stale revisions and ambiguous matches.
- A changed revision cannot finish using an older revision's validation.
- Timeout/disconnect results do not become successful auditions.
- Late responses and results from another tab cannot complete the active request.
- Skip/discard during generation, audition, or capture prevents later archiving.
- Budget exhaustion restores an eligible fallback, or discards if none exists.
- Malformed JSON fallback responses stop predictably.
- Visual regression can retain the previous source and its metadata.

Manual integration checks should cover a shader syntax error, runtime exception, blank scene, visibly frozen scene, slow rendering, a successful sparse effect, and a failed polish attempt.

Also verify classic generation, replay, title cards, archive playback, and config loading still operate. Test on the intended display hardware; a desktop-only pass does not establish Raspberry Pi performance.

## 15. Evaluation design

Start with six paired smoke trials covering a shader effect, a 3D scene, a motion-heavy effect, a complex stateful effect, a creative prompt, and an archive update. If stable, expand to at least 20 paired trials with repeats for variable outputs.

Use the same model, provider, generation settings, source inputs, viewport, and instrumented browser for each pair. Where supported, record and reuse seeds, while recognizing that identical seeds across different workflows do not guarantee comparable outputs.

Compare both practical defaults and a matched time/token-budget condition. This separates the benefit of the interaction design from simply spending more inference.

Record:

- Initial and final runtime pass rates.
- Completion time, generated tokens, model turns, and tool calls.
- Repair success, failed polish, rollback, and budget-exhaustion counts.
- Visual-review time and input size where available.
- User preference in a shuffled, model/strategy-hidden comparison.
- Whether the effect satisfies the original brief.

Keep these metrics separate from historical failure statistics because validation changes alter what counts as a pass. Do not claim an improvement from a few attractive examples or an unreliable automatic aesthetic score.

## 16. Logging and archive metadata

Record a bounded session trace containing the brief, tool names, revisions, change summaries, diagnostics, timing, budget use, fallback decisions, and final revision. Avoid logging API keys or base64 media payloads.

Add optional archive metadata such as strategy, session ID, turns used, repair count, visual review status, and final validation status. Existing archive entries should continue to load without these fields.

Keep temporary captures subject to a cleanup policy. Archive the accepted standalone HTML through the existing application path. Do not make playback depend on an agent session, model endpoint, or tool server.

## 17. Definition of done

- Both classic and iterative modes are selectable and bounded.
- The model can make a targeted edit and observe its actual result.
- The original brief survives every repair and visual pass.
- Every archived iterative candidate has validation tied to its exact source revision.
- A failed experiment can return to an earlier eligible revision.
- Supported multimodal providers can inspect a polished revision before acceptance.
- Unsupported tool or visual capabilities have an explicit fallback or clear disabled state.
- Skip, discard, replay, and the static archive player retain their intended behavior.
- Paired evaluation results and limitations are documented before changing the default strategy.

## Source references

Reviewed at commit `f990d03a3c54874a9cf19a7e822980d90e7295a9`:

- [Generation, repair, video improvement, and audition orchestration](https://github.com/RowanUnderwood/auto_demo_scener/blob/f990d03a3c54874a9cf19a7e822980d90e7295a9/ai_demoscener/orchestrator.py)
- [API client](https://github.com/RowanUnderwood/auto_demo_scener/blob/f990d03a3c54874a9cf19a7e822980d90e7295a9/ai_demoscener/lm_client.py)
- [Probe and validation synchronization](https://github.com/RowanUnderwood/auto_demo_scener/blob/f990d03a3c54874a9cf19a7e822980d90e7295a9/ai_demoscener/validator.py)
- [Browser audition and display behavior](https://github.com/RowanUnderwood/auto_demo_scener/blob/f990d03a3c54874a9cf19a7e822980d90e7295a9/ai_demoscener/static/display.js)
- [Defaults and system prompts](https://github.com/RowanUnderwood/auto_demo_scener/blob/f990d03a3c54874a9cf19a7e822980d90e7295a9/ai_demoscener/config.py)
- [Vendored Three.js version](https://github.com/RowanUnderwood/auto_demo_scener/blob/f990d03a3c54874a9cf19a7e822980d90e7295a9/ai_demoscener/download_vendor.py)

## Review notes and decisions (2026-10-18)

Milestone 1 is implemented (display registration and pausing, correlated auditions and recordings, TIMEOUT handling, structured probe diagnostics, brief-preserving repairs). The review of this plan found these issues to settle before Milestone 2:

1. **Revision chaining.** With `expected_revision` mandatory on every edit and `run_demo(revision)` mandatory, a model that edits and then runs in one response must predict a revision ID that doesn't exist yet, and a second edit in the same response is rejected as stale (§6). Make `revision` optional (defaulting to current) and execute mutations sequentially, stale-checking each against the revision the previous call produced.
2. **Streaming vs native tools.** If the HTML lives inside JSON tool arguments, the editor typing animation needs an incremental JSON-string decoder, and small local models often break JSON escaping on 300-line files. **Decision:** the first draft is produced with the existing classic stream; tool-driven iteration starts at revision 1, with `write_demo` kept as a fallback for full rewrites.
3. **Context budget.** The `lm_studio` defaults (16k context, 8k output) don't fit one HTML file, tool definitions and several turns. **Decision:** rebuild every request from session state (brief + current numbered source + latest evidence + short change log) instead of growing a transcript. This also removes the tool-result pairing, reasoning-metadata and compaction concerns in §9–10.
4. **Visual modality.** Video is Ninfer-only in practice (Strata takes images only, LM Studio depends on the model, and video needs ffmpeg). Make screenshots — or a short frame sequence via `canvas.toDataURL()`, which needs no upload or ffmpeg — the primary inspection modality.
5. **"Best eligible revision"** (§6.8) needs a definition: the latest runtime-valid revision unless a visual comparison rejected it.
6. **Smaller simplifications:** fold `get_environment` into the system prompt; drop `read_demo` when the source is in every turn; match `edit_demo` text with whitespace runs normalized (still requiring a unique match). Settings are already snapshotted per cycle (§11). Strata supports native tools and images, which makes it a good first native-tools target.
7. **Infrastructure behaviour (done).** Timeouts no longer count as passes. With no display connected, generation pauses rather than discarding, because nothing can validate a demo. The "designated display" is implemented as a `hello` registration plus first-matching-reply-wins on audition IDs.

## Milestone 2 status (2026-10-18)

Implemented as `generation_strategy: "iterative"` (default stays `classic`): `ai_demoscener/agent_runner.py` (session loop), `ai_demoscener/demo_tools.py` (source store, tools, action parsing), `lm_client.chat_collect()` (structured streamed responses with tool-call assembly), orchestrator/display/config wiring, and `ai_demoscener/tests/test_iterative.py` (22 stdlib tests). It follows the review decisions above: classic first draft as revision 1; stateless turns; no revision arguments on tools (turns are serial); whitespace-tolerant unique edits; one "attention" turn for passing revisions with suspicious diagnostics; fallback to the latest passing revision, re-checked before display.

Verified: native tools and JSON actions both fixed a shader-error demo in one turn against Strata (`qwen3.8-flash-next-coder-iq1_m`, about 5 s per turn); end to end through the kiosk display with a scripted provider (broken draft → one streamed tool edit → archived; clean draft → zero turns). Not yet measured: behaviour on LM Studio and Ninfer models, and quality or latency against classic (Milestone 4).

## Milestone 3 status (2026-10-18)

Implemented as `iterative.visual_review` (off by default). Once a revision passes, the app captures JPEG frames from the running demo (or a short video on Ninfer). A tool-free critique request returns at most three grounded issues; ordinary edit turns polish them; a capture of the polished revision is compared with the baseline, and the baseline is kept unless the model picks the polish. An unfinished or broken polish also keeps the baseline, and decisions are recorded in the archive entry. In-session review replaces the classic Ninfer video-improve pass for that demo. 11 new tests, plus `tests/test_validation.py` covering the Milestone 1 behaviour (44 tests in total).

Verified: end to end through the kiosk display with a scripted vision model. Frames were captured from the hidden iframe and delivered as image parts; the critique led to a polish edit and the before/after compare picked the polish. In another cycle a polish that failed its runtime check fell back to the reviewed revision. Not verified: a real vision model. The Strata server on the LAN was started without its vision encoder and rejects images with HTTP 400, which correctly disables the review. Its `/v1/models` reports `input_modalities`, which could be used to skip the attempt up front.

Follow-up: the classic Ninfer "improve demo via video check" pass (`ninfer.video_check_enabled`) was removed, so visual review is the only visual-improvement path. It is available only with the iterative strategy, and the old pass's settings no longer appear in the config page. Video input is restricted to Ninfer in the config page.

## Milestone 4 status (2026-10-18)

Implemented: comparable per-cycle metrics (`metrics.py`; live cycles → `session_metrics.jsonl`, kept apart from `prompt_stats.json`), paired evaluation runs (`evaluation.py`, `boot_mode: "evaluate"`, `/eval` page), and blind A/B review with per-side brief checks. **Pairing decision:** each trial writes one shared first draft and runs every arm on it. That isolates what each strategy does with a draft from draft-to-draft randomness, and drafts both arms accept unchanged count as identical (auto-ties). Arms: `classic`, `classic_matched` (classic with `max_repair_attempts` = iterative `max_turns`), `iterative`, `iterative_visual`.

### First smoke run: `eval/20261018-082246-smoke-ninfer`

Ninfer `qwen3.8-27b` (thinking effort low, native tools, image input), six trials (Plasma, Solar System, Boids Flocking, Auto-Play Tetris, creative, update), four arms, one repeat. Display: headless Chromium with software rendering (SwiftShader, 960×540), so `min_fps` was 0 and frame rates were not judged. Ninfer reports no token usage, so output size is in characters.

| Arm | Final pass | Changed draft | Median s | Responses | Tool calls | Vision calls | Output chars |
| --- | --- | --- | --- | --- | --- | --- | --- |
| classic | 6/6 | 1/6 | 5.0 | 0.2 | 0 | 0 | 2,619 |
| classic_matched | 6/6 | 1/6 | 5.0 | 0.2 | 0 | 0 | 2,619 |
| iterative | 6/6 | 2/6 | 5.0 | 0.7 | 1.3 | 0 | 1,528 |
| iterative_visual | 6/6 | 6/6 | 93.8 | 2.7 | 12.3 | 4.0 | 11,432 |

Findings:

1. **Drafts were mostly fine.** 5/6 shared drafts passed their first check. The one that failed (update mode, a missing variable) was fixed by every arm: classic with one whole-file repair (45.6 s, including the editor typing animation), iterative with two targeted turns (20.8 s).
2. **The matched budget never mattered.** Classic never used up its two repairs, so `classic_matched` was identical to `classic` in all six trials. It only differs when repairs run out. Leave it out of routine runs unless drafts fail more often.
3. **Without visual review, iterative equalled classic on 4/6 trials** (zero turns). On Solar System it acted on a passing draft because only 1 of 64 sampled cells was lit. It edited blind, broke the scene (BLANK), then repaired it. **Tuned:** "mostly dark" and "slow render" now reach only the visual critique, not the text-only extra look.
4. **The visibility check was flaky for sparse scenes.** The identical Solar System draft scored 1/64 lit (OK) on three runs and 0/64 (BLANK) on the fourth. **Tuned:** when the grid is all dark, the probe reads the full frame once (first 20 s only) and BLANK requires no lit pixel at all. A browser check confirmed a 3-pixel dot on black now passes and a black scene is still BLANK.
5. **Visual review changed every demo and costs about 100 s per demo.** The critiques were concrete and grounded (stretched aspect ratio on Plasma, overexposed highlights, framing), and the edits applied cleanly. But the same model judging its own polish chose "after" in 11 of 12 compares, so **the compare is not an independent quality gate**. Whether visual polish is actually better needs the blind human review (8 rateable pairs are waiting at `/eval`) or a different judge model. With `max_polish_rounds: 2` (the current config) each demo takes two rounds; 1 halves the cost.

Limitations: n = 6 with one repeat, one model, software rendering (frame rate not judged, render timings not representative of the kiosk), no human preference ratings yet, and no token counts.

Initial recommendations (superseded by "Analysis of all evaluation runs" below): keep `classic` as the default. `iterative` without visual review is a low-risk alternative: it matched classic on every trial, fixed the one failure faster with less output, and after the tuning above it no longer edits passing demos on dark/slow signals alone. Treat `iterative_visual` as experimental until blind ratings show the polish is preferred, and prefer `max_polish_rounds: 1`. Next: rate the 8 pairs, then run the plan's 20+ paired trials with repeats on the real kiosk display.

### Analysis of all evaluation runs (2026-10-18)

Three runs on Ninfer `qwen3.8-27b`, 33 shared drafts, 23 blind ratings:

| Run | Display | Trials | Arms | Rated pairs |
| --- | --- | --- | --- | --- |
| `20261018-082246-smoke-ninfer` | headless, software rendering | 6/6 | all four | 8 of 8 |
| `20261018-092931-smoke` | kiosk (Firefox, ~60 fps) | 12/12 | classic, iterative | 3 of 3 |
| `20261018-101218-smoke` | kiosk | 15/36 (stopped) | all four | 12 of 25 |

Most of run 2 (t03–t12) and all of run 3 used the tuned visibility check and attention rules (app restarted 09:56).

**Reliability.** Final pass: classic 32/33 (97%), classic_matched 21/21, iterative 33/33, iterative_visual 21/21. On the kiosk 6 of 27 drafts genuinely failed their first check (22%); in run 3, 5 of 15. Classic used up its two repairs once (run 3 t10, Auto-Play Tetris), where `classic_matched` and both iterative arms recovered. That is the only trial where the matched budget mattered.

**Repair speed on the kiosk** (drafts that failed; wall time after the draft, including classic's typing animation):

| Trial | Classic | Iterative |
| --- | --- | --- |
| run 2 t11 creative | 34.4 s | 12.6 s |
| run 3 t04 Tetris (no draws) | 29.9 s | 57.3 s |
| run 3 t06 update | 38.1 s | 7.9 s |
| run 3 t10 Tetris | 67.2 s, failed | 58.6 s |
| run 3 t11 creative | 24.7 s | 12.3 s |
| run 3 t12 update | 34.2 s | 8.2 s |

Iterative was faster in 5 of 6 and recovered the one classic lost. It also wrote far less text (run 3 mean 1,711 vs 4,093 output chars per demo).

**Blind preference vs classic** (non-identical pairs, rated):

| Arm | Wins | Losses | Ties | Both bad | Identical (auto-tie) |
| --- | --- | --- | --- | --- | --- |
| iterative_visual | 6 | 3 | 3 | 2 | 1 |
| iterative | 1 | 2 | 1 | 2 | 22 |
| classic_matched | 1 | 0 | 1 | 1 | 16 |

- **iterative_visual** won 6 of 9 decisive pairs (Auto-Play Tetris 2–0, Solar System 2–1, creative 1–0, Boids 1–0 with two "both bad", Plasma 0–1, update 0–1). That leans positive but isn't significant at this size (two-sided sign test p ≈ 0.5). The model's own before/after judgement kept its polish in 35 of 39 rounds (90%), much more confident than the human ratings, so it is not a substitute for them. All 4 rejections came in second rounds (round 1: 20 kept, 0 rejected; round 2: 15 kept, 4 rejected). Cost on the kiosk: median 90 s per demo after the draft, against about 5 s for drafts that pass.
- **iterative** produced output identical to classic in 22 of 33 pairs (11 differed, 6 of those rated). Of its 2 losses, one was the pre-tuning blind edit of a dark scene (fixed) and one an update-mode repair where both arms rewrote the demo. Its 1 win (run 2 t04 Tetris) came from a "frozen output" look on a passing draft with 0/64 cells lit. The edits made the game visible, and you marked classic's unchanged version as not meeting the brief.
- **classic_matched** differed from classic in 5 of 21 pairs (3 rated), including the one where classic failed outright.

**Check issues found in this analysis:**
1. *"Frozen" is grid-based and fires on sparse scenes.* It triggered all three looks iterative took at passing drafts on the kiosk: run 2 t04 (Tetris, 0/64 lit), run 3 t14 (Solar System, 0/64 lit, so the samples were always black) and t15 (Boids, small objects moving between sample points). The only rated one (t04) was a clear win, and t14/t15 are unrated. So it stays a text-only trigger for now. Rating run 3's t14/t15 iterative pairs will show whether it should move to visual-review-only like "mostly dark".
2. *Zero-heartbeat BLANK (fixed).* Run 2 t03 (Boids): the classic arm's check got no heartbeats at all ("No activity detected"), while the iterative arm passed the same draft. Classic then rewrote a working demo, and you rated both "bad". A BLANK with zero heartbeats and no errors is now re-run once before it counts.
3. Flaky BLANK on sparse scenes: already fixed after the first smoke run (full-frame check).

**Data caveats:** "Satisfies the brief" ticks were sometimes inconsistent for the same classic output across pairs, and an unticked box can't be told apart from a skipped one, so brief rates aren't used here. Per-category results have one to three ratings each. 13 pairs in run 3 are still unrated.

**Updated recommendations:**
1. **Use `iterative` (visual off) as the strategy.** It is as reliable as classic (it never lost a demo, and classic did once), repairs failures faster with much less output, and it leaves passing drafts alone except for console warnings or whole-run frozen output. Its output was identical to classic in most cases, and blind quality was otherwise indistinguishable. Your `config.json` already uses it. Changing the shipped `DEFAULTS` from `classic` is reasonable, but still yours to decide.
2. **Visual review: optional, with `max_polish_rounds: 1`.** Humans preferred its output more often than not (6–3), with no regression in pass rate, at about 90 s extra per demo. Second rounds were where all the self-rejections happened and they double the time, so 1 round gets most of the benefit. Enable it when polish matters more than throughput. Keep treating the model's compare as a safety net, not as evidence of improvement.
3. **Leave `classic_matched` out of routine runs.** It only matters when classic runs out of repairs (1 of 21 trials).
4. **Next evidence:** rate the 13 open pairs in run 3 (especially iterative t14/t15, which decide the "frozen" trigger), then resume it (`/eval` → resume) with the fixed checks. 21 trials remain, which would bring the visual comparison to roughly 30 rated pairs. Consider a different judge model for the compare step, since a model grading its own polish was nearly always satisfied.
