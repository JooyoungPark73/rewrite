# Feedback to Claude Code — rewrite audit

2026-10-18. Audited against `PLAN.md`, the phase briefs in `.agents/scratch/`, and the implementation. Three concurrent GPT-6.1 Sol reviewers at max reasoning covered runners, spec/git, and frontend; the primary reviewer checked compiler/source resolution, acceptance records, and consolidated the findings.

**Assessment:** the implementation covers the planned architecture and its existing automated suites pass. The claim that all phases are implemented and verified is too strong. There are reproducible paths that lose prose or overwrite another paper's spec, incomplete review scopes, and a clean-context isolation defect. Fix these before treating the workbench as ready for real papers. macOS acceptance remains outstanding under the unchanged plan.

This is audit feedback, not an implementation patch. No source fixes or commits were made. Locations below refer to the audited working tree. P1 means fix before relying on the app for paper editing/review; P2 means a workflow or correctness defect that also needs repair.

**Verification performed in this audit**

| Check | Result |
| --- | --- |
| Backend `.venv/bin/python -m pytest -q` | **559 passed, 3 skipped**, 81.21 s. The skipped tests are Docker-gated. |
| Backend with `REWRITE_TEST_DOCKER=1`, `tests/test_docker.py` | **19 passed**, 11.98 s, including the three gated real Docker checks. |
| Ruff lint and format check | Passed; 79 files already formatted. |
| Frontend typecheck, lint, production build | Passed. |
| Existing frontend Playwright suite | **54 passed**, using its mock backend. |
| Targeted reproductions | Real backend functions in temporary repositories; native LaTeX compile; actual frontend stores with controlled API timing; real React/CodeMirror browser probes with intercepted API responses; offline Codex prompt inspection. |

The sandbox stalled FastAPI's TestClient at its AnyIO portal and blocked browser-server binding/Docker access. Authorized runs outside that sandbox completed. These environment failures are not counted as application bugs. Abandoned test processes were stopped.

No paid model requests, pushes to external remotes, or macOS tests were performed. The historical live scripts were inspected, not rerun end to end. Passing the mock browser suite does not establish real frontend/backend integration for every workflow.

**1. [P1] Dirty Raw JSON from paper A can overwrite paper B's entire spec**

Locations: `frontend/src/spec/RawJson.tsx:30,40,125-136`; `frontend/src/spec/SpecView.tsx:121`.

The module-level `draft` has no repository identity. The mounted editor also refuses incoming `raw` changes while dirty, including changes caused by opening a different repository. The subsequent Save writes to whichever repository the backend currently has open.

Browser reproduction with the real React/CodeMirror implementation:

1. Open paper A's Raw JSON and make an unsaved, valid spec change.
2. Open paper B and wait until the spec store has loaded B's spec.
3. The editor still displays A's draft. Click Save.
4. The request writes A's complete document into B's `paper.json`.

**Fix and regression:** scope the editor, draft storage, and pending writes by repository identity. Switching repositories must resolve or retain A's draft under A, and mount B's editor with B's content. Verify both switching while Raw JSON stays mounted and leaving/reopening the tab with a retained draft. An A draft must never generate a write to B.

**2. [P1] Opening another repository silently discards unsaved source**

Locations: `frontend/src/state/store.ts:292-295`; `frontend/src/state/store.ts:223-235`.

`openRepo()` changes the backend's repository first, then `adoptRepo()` clears `activeFile`. There is no save of the old buffer. An actual-store probe with dirty `main.tex` in A followed by opening B produced zero source PUT requests and replaced the buffer with B's source.

**Fix and regression:** complete persistence of the current paper's edits before changing the backend repository, or preserve them in an explicitly repository-scoped draft flow. A failed save must leave the current paper and its buffer available. Test both successful and failed persistence during a repository switch. Do not attempt to save A after the backend has already switched to B.

**3. [P1] File navigation loses edits after a failed save or typing during a save**

Locations: `frontend/src/state/store.ts:311-329`; `frontend/src/state/store.ts:367-390`.

`openFile()` awaits `save()`, but `save()` catches errors and resolves successfully. Navigation then replaces the dirty buffer. Even when the request succeeds, edits typed after its content was captured remain dirty only until navigation replaces that buffer.

Both cases were reproduced against the actual store with controlled responses:

- Reject the source PUT: the target file still opens and the old unsaved text disappears.
- Hold the source PUT, type additional text, then release it: the older text reaches disk, but the newer text disappears when navigation completes.

**Fix and regression:** make save outcomes explicit and require the latest buffer revision to be persisted or retained before changing files. Cover rejected saves and typing while the switch's save is pending. Retain the original buffer on failure.

**4. [P1] A Raw JSON save response erases text typed while the request is pending**

Location: `frontend/src/spec/RawJson.tsx:61-70`.

On success, `save()` unconditionally calls `showText()` with the canonical response and clears the dirty flag. The CodeMirror editor remains editable during the request.

Browser reproduction: save a valid spec containing `FIRST SAVE`, hold the HTTP response, change it to `SECOND UNSAVED EDIT WHILE SAVING`, then release the response. The editor returns to `FIRST SAVE`, clears its dirty indicator, and loses the later edit.

**Fix and regression:** capture the submitted editor revision and apply canonical text only if that revision is still current. Otherwise preserve later typing and update only the saved baseline. Test delayed saves through both the button and keyboard shortcut.

**5. [P1] Marker adoption can overwrite an already successful source save**

Locations: `backend/app/skeleton.py:571-591`; `backend/app/api/files.py:90-95`; `backend/app/api/spec.py:401-430`.

Marker insertion checks source against its scan, constructs edited text, then writes it later. Spec operations hold `spec_lock`, but ordinary `/api/file` saves do not participate in that lock. The source check therefore does not protect the subsequent write.

Reproduced with real functions and a thread barrier solely to force the interleaving: pause `place_marker()` immediately before its write, save new prose through `write_text_file()` (the writer used by `/api/file`), then resume adoption. The new prose was present after the successful save and absent after adoption, which restored the old prose plus a marker.

**Fix and regression:** coordinate source saves and marker read/modify/write operations with shared per-repository synchronization, and reject stale source revisions. Include Git/source-changing operations in the coordination where applicable. A barrier-based regression must demonstrate that both operations either preserve the new prose and marker or return an explicit conflict.

**6. [P1] Undo after an external spec reload silently removes incoming changes**

Locations: `frontend/src/state/spec.ts:300-309,228-245`; `backend/app/api/spec.py:244-252`.

External reload replaces the current spec while retaining the undo/redo stacks. Those stacks contain full old documents. Undo then PUTs an old document without any current-revision precondition.

Reproduction from the verified code path: make one outliner edit; change a different spec field externally or through Git pull; allow `spec.changed` to refresh the UI; press Undo. The restored document predates the external change and removes it as well as the user's own edit.

**Fix and regression:** invalidate incompatible history when external content arrives, or rebase undo operations onto the new document. Add revision/hash preconditions to whole-spec replacement so a concurrent change cannot be silently overwritten. Test an external edit to an unrelated field and assert it survives undo, or that undo is explicitly unavailable/conflicted.

**7. [P1] The sandbox working directory does not isolate Codex from paper-level instructions**

Locations: `backend/app/runner/base.py:416-424`; `backend/app/config.py:122-125`.

The runner changes cwd to `.rewrite/sandbox/`, but the installed Codex also discovers ancestor `AGENTS.md` files. This contradicts the plan's premise that the only project instructions visible to a review are the generated sandbox instructions. The already documented global `AGENTS.md` caveat does not cover this separate paper-repository leak.

Offline reproduction: a temporary paper with `.git/`, a root `AGENTS.md` containing `ROOT_PAPER_HISTORY_CANARY`, and sandbox `AGENTS.md` containing `SANDBOX_REVIEW_CANARY` was inspected using:

```text
codex --disable shell_tool --disable unified_exec debug prompt-input 'Review the submitted paper.'
```

Both canaries appeared in the same instruction block. The test isolated user configuration with an empty temporary Codex home and made no model request. **Evidence limit:** the debug command does not accept exec-only `--ignore-user-config` and `--ignore-rules`; installed exec help describes those as suppressing `config.toml` and execpolicy `.rules`, respectively, not ancestor instruction files. The actual configuration contains no ancestor-document suppression mechanism.

Paper-level guidance can contain writing history, authorship, or conflicting review instructions. It is absent from the assembled prompt shown/stored by the app and from its cache key.

**Fix and regression:** establish a verified instruction-discovery boundary while retaining the planned sandbox location. Do not claim isolation based on cwd or shell-tool removal alone. Use root/ancestor/sandbox canaries to verify the supported installed CLI's effective input with the production arguments. If the CLI cannot provide the required boundary, document the precise limitation and surface it rather than silently promising clean context. Recheck Claude independently; this audit's concrete runtime proof is for Codex.

**8. [P2] Section-scoped reviews omit section bodies stored in included files**

Locations: `backend/app/prompts.py:636-647`; related physical-file slicing in `_resolve_ends()`.

The resolver finds the heading in the document scan, then reads only the heading's physical file. A common layout reproduces the omission:

```tex
\documentclass{article}
\begin{document}
\section{Introduction}
\input{intro-body}
\section{Conclusion}
Closing words.
\end{document}
```

With a unique sentence in `intro-body.tex`, whole-paper scope includes it, but Introduction scope contains only numbered lines 3-4 of `main.tex`: the heading and `\input` directive. The argument itself never reaches the reviewer. Section fan-out inherits this problem; the introduction/conclusion portion of the ends scope uses the same physical-file approach.

**Fix and regression:** derive section content from document order across includes, preserving each source file's line labels. Test a heading in the main file with its body in an included file, nested includes, and section boundaries across files. Verify section, fan-out, and ends prompts include the body and exclude the next section.

**9. [P2] Already staged deletions and renames prevent commits and snapshots**

Location: `backend/app/gitops.py:518-548`.

`changed_paths()` includes both sides of a rename. `commit()` then runs `git add --` on every chosen path, including a deleted path already removed from the index.

Reproduced separately in isolated Git repositories after `git rm old.tex` and after `git mv old.tex new.tex`. Status reports the change correctly, but the default app commit fails with:

```text
fatal: pathspec 'old.tex' did not match any files
```

Dirty revision snapshots use this same commit function. Existing tests cover unstaged deletion and rename status, but miss committing an already staged deletion/rename.

**Fix and regression:** handle index-only deletions/rename sources without trying to add nonexistent paths. Preserve the intended selected-path behavior and unrelated staged changes. Test commit and snapshot with both staged deletion and staged rename, including a selected subset.

**10. [P2] Duplicate unlabeled headings create confidently wrong section anchors**

Locations: `backend/app/skeleton.py:502-508`; `backend/app/anchors.py:192-205`.

`section_marker()` uses a label when a duplicate heading has one; without a label it still returns the ambiguous heading text. Resolution chooses the first match.

Direct reproduction: put two `\section{Discussion}` headings in the same file at lines 3 and 6. Adopt line 6. Its new node resolves to line 3 and is reported linked. Repeated adoption of line 6 creates more nodes anchored to line 3, while line 6 remains unadopted. Section review and coverage can consequently refer to the wrong text.

**Fix and regression:** reject ambiguous adoption or require/create a stable unique label through a human editor action. Do not report an ambiguous anchor as linked. Verify duplicate headings with and without labels, plus repeated adoption of the same intended heading. Preserve PLAN's prohibition on position-based anchors.

**11. [P2] Valid unbraced `\input` silently disappears from the editor and review scopes**

Locations: `backend/app/files.py:178-180,245-255`; `backend/app/prompts.py:600-616`.

The include matcher requires braces. A minimal document using `\input section` compiles successfully through `NativeLatexmk`, and the compiler recorder includes `section.tex`, but `resolve_tex_tree()` returns only `main.tex` and no missing-file warning. Reproduced in a temporary initialized paper with the actual native compiler.

The same resolver supplies the file tree, skeleton traversal, and whole-paper prompt, so a successfully compiled part of the paper can be absent from editing/adoption and from a supposedly whole-paper review.

**Fix and regression:** support literal unbraced `\input` paths in the shared include parser. This does not require general TeX macro expansion. Compare tree/skeleton/prompt coverage against a compiled minimal fixture, and ensure a section-body change affects the assembled scope and cache input.

**Acceptance records and verification gaps**

- `PLAN.md:261` says a phase ends when its manual checks pass on macOS. `.agents/scratch/00-overview.md:5-7` weakens this to running checks and listing unverified items, then marks all phases done. `AGENTS.md` and `CLAUDE.md` repeat completion. The per-phase notes openly acknowledge the missing macOS work. Mark the state as implemented/Linux-verified with macOS acceptance pending, unless the user explicitly changes the plan. Do not silently redefine the completion gate.
- Phase 1 still begins with `Status: in progress` while its later entry says complete. Make the status records internally consistent after applying the acceptance distinction.
- `frontend/e2e/live/p1-e2e.mjs` logs booleans such as `SAVED to disk` and `ERROR LISTED` but does not assert them or return failure when they are false. Its clean exit alone is not a passing manual check. Turn required checks into assertions/nonzero exits and retain actual outcomes when recording completion.
- The existing suites passing is credible and was independently reproduced. Their gaps are failed writes, delayed responses with continued editing, repository transitions, concurrent source/spec operations, and alternate valid include layouts. Add targeted regressions for the findings above instead of using the current test count as the acceptance argument.
- Optional Tauri packaging being documented but unbuilt is consistent with the plan. No additional LLM-output-to-`paper.json` write path was found in this audit; the existing invariant tests passed. The A/B judge prompt inspected uses randomized versions and reduced fresh context. These observations do not remove the editing and isolation defects above.

**Requested next pass for Claude Code**

Fix the P1 data-loss issues first, then verify the effective CLI context boundary and repair scope/Git/anchor correctness. Keep the architecture and `paper.json` ownership rule from `PLAN.md`. Add a regression that fails on the current implementation for each repaired issue, rerun the relevant suites, and record exact results plus remaining manual checks. Update the completion records honestly; do not mark macOS acceptance complete based on Linux/headless substitutes.
