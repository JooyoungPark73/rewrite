# sample-paper-with-spec: a filled-in `paper.json`

The same paper as `../sample-paper`, with its semantic spec written out. Open it in the app
(`make init REPO=examples/sample-paper-with-spec && make start REPO=examples/sample-paper-with-spec`)
and look at the **Spec** tab next to the sources; or read `paper.json` directly. This page
explains what each part is for and how it was filled, so you can do the same for your own paper.

## The idea in one paragraph

The spec is the JSON-like semantic state from the "writing as hierarchical iterative refinement"
sketch: `message` → `claims[]` → `sections[]` → `paragraphs[]`, with the sentences living only in
the `.tex` files. You write the state coarse to fine; the prose is a realization of it. When a
review pass (or a reviewer) says "the real takeaway is X, not Y", you do not patch the ending:
you change the state at the level where the critique lands (`message.thesis`, a claim's `scope`,
a section's promise, a paragraph's `says`), re-read the descendants that depend on it, and
re-realize the text. Critique is reconciled upward into the state; text is re-rendered downward.
The LLM passes read the spec and the text and report mismatches; only you edit the spec.

## What goes where

Every node has the same shape: `id`, `level`, `says`, `fields`, `claims`, `children`, `anchor`,
`notes`. `says` is the one sentence that node exists to get across; `fields` differ per level.

| Level | `says` means | `fields` | How it is anchored to the `.tex` |
| --- | --- | --- | --- |
| **message** (one) | the takeaway a reader should leave with | `problem` (what is wrong today), `stakes` (shown as "Why it matters": who cares, what changes), `thesis` (shown as "Argument": the chain from evidence to takeaway, in one breath) | nothing; it is the root |
| **claim** (flat bank) | the claim, stated as a sentence a reviewer could dispute | `evidence` (where in the paper it is backed, with the number if there is one), `scope` (the conditions under which it holds) | nothing; sections and paragraphs point at claims by id |
| **section** | the promise the heading makes to the reader | `figure_story` (what the section's figure or table must make the reader see; empty is fine) | its `\section{...}` heading text |
| **paragraph** | the local message: what this paragraph says, in one sentence | `function` (the job it does in the argument: set up, concede, define, deliver the result, transition...) | a `% @p <id>` comment line the app inserts above the paragraph |

`notes` is free text for yourself (TODOs, doubts). The example uses it on claim `c2`.

## How this file was filled, in the order that works

1. **Adopt the skeleton.** In the Spec tab, the "Skeleton from the .tex" panel lists every
   heading and paragraph the app finds. Adopting a heading creates a section node anchored to it;
   adopting a paragraph inserts `% @p p-N` above it and creates a paragraph node under the right
   section. (That is the only edit the app ever makes to your `.tex` for the spec.) You can also
   adopt one paragraph at a time while writing, or write the tree first and link later.
2. **Message first.** Fill the takeaway (`says`), Problem, Why it matters and Argument. If you cannot write these in
   four sentences, the paper is not ready to be reviewed against them; that is useful to know.
3. **Claims next.** List what the paper asserts, one sentence each, and for each write where the
   evidence is and what the claim's scope is. Anchor each claim to the sentence that states it
   (select the sentence in the editor, right-click, "Lift to claim"; here every claim is anchored:
   c1 and c2 to the two observations in the introduction, c3 and c4 to the sentences in the
   evaluation that carry the numbers, c5 to the safety sentence in the design). The card then
   says where the claim is stated, and turns stale when that sentence is reworded. Writing `evidence` is where gaps show: claim `c2`
   ("the working set is small relative to the snapshot") turned out to have no measurement
   behind it, so its `evidence` says so and `notes` holds the TODO. The `claims-evidence` pass
   will flag exactly this.
4. **Sections.** One promise per heading. `figure_story` names the figure or table the section
   leans on and the single thing the reader must take from it (`tab:latency`: "Prefetch vs warm
   pool at equal p99: 36 GB vs 96 GB"). Refer to figures, tables and equations by their
   `\label` (`fig:pipeline`, `tab:latency`, `eq:working-set`, or `\ref{fig:pipeline}`), never by
   number: numbers change when a figure moves, labels do not. Every such reference in a spec
   field shows as a chip under the field with the figure's caption; clicking it opens the `.tex`
   at the `\label` line, and an unknown label is flagged.
5. **Paragraphs.** For each paragraph, `says` (its message) and `function` (its job). A paragraph
   whose job you cannot name is a paragraph the `paragraph-function` pass will question; here
   `p-8` ("Point to fig:pipeline") is flagged in its own `function` as foldable.
6. **Link claims.** Drag a claim card from the bank onto a section or paragraph row, or click a
   cell of the **Coverage** matrix (right of the outliner) to link a claim to a section; the × on
   a chip unlinks. The matrix then shows where each claim is argued; an unreferenced claim or a
   section arguing nothing is a hole in the paper, not in the spec. Selecting a sentence in the
   editor and right-clicking offers "Lift to claim", which adds it to the bank and links the
   enclosing paragraph.

The status dot on each row tells you how the spec and the text relate: **linked** (the marker
is in place and the prose has not changed since you last looked), **stale** (the prose under the
marker changed; re-read it, then edit `says` or press Reconcile), **orphaned** (the marker is
gone), **planned** (no anchor yet; a paragraph you intend to write).

Claims have a dot too. "Lift to claim" anchors the new claim to the sentence you selected, so
the card says where the claim is stated (click to open it) and turns **stale** when that
sentence is reworded or deleted; Unlink forgets the anchor.

The loop also runs downward. Edit a claim's statement, evidence or scope, and every section and
paragraph that links it gets a ring on its dot: **re-read** it, because it was written against
the old claim. A changed section promise rings its paragraphs; a changed message rings the
sections. The header counts them ("3 to re-read"); editing a ringed node's `says` or choosing
⋯ → Reconcile ("I re-read it") clears the ring. Links, notes and moves ring nothing.

## Drafting a paragraph from its entry

Every paragraph row's ⋯ menu has **Draft from spec…**: a model writes the paragraph from its
`says`, `function` and linked claims, in the voice of the section, and shows it for editing;
nothing enters the `.tex` until you press Replace paragraph or Insert at cursor. Try it on
`p-8` ("Point to fig:pipeline"): the draft points at the figure and says what it shows, and
after Replace the row is stale until you re-read it. Drafting is separate from review (its own
prompts in `.rewrite/prompts/drafts/`); the review passes then judge the drafted text like any
other.

## Revising a sentence on a comment

Select the second sentence of the second introduction paragraph ("Warm pools trade memory for
latency, and the trade is poor …"), right-click, **Comment…**: "Too verbose; say it in one
sentence." The comment appears as a finding; its **Rewrite…** proposes a shorter sentence, and
Approve writes it as `\aigen{…}` with the original on a `% aigen-was:` line, for your co-authors
to accept in Overleaf by removing the wrapper. The row turns stale until you re-read it.

## Using it with the review passes

This is a systems paper, so its `.rewrite/config.toml` (shipped with the example; `rewrite
init` keeps it) sets `style = "systems"`: the field's standard of evidence from
`.rewrite/prompts/styles/systems.md` is appended to every review prompt, so the claims-evidence
and calibration passes ask for the workload, machine and metric behind every number, which is
what a systems reviewer asks. Change it under Writing style in Settings.

Run passes coarse to fine, each against this spec: `message-drift` (does the abstract, intro and
conclusion say the message?), `claims-evidence`, `section-promises`, `paragraph-function`, then
the sentence-level passes. A finding is a mismatch between the text and a node; decide whether
the text is wrong (edit the `.tex`) or the spec is wrong (edit the node, which may ripple to its
descendants), and rerun. The passes never edit either file.

## Reading the file

`paper.json` is written by the spec editor in a fixed layout (sorted keys, one node per line) so
that a `git diff` after an edit is one or two lines. Edit it through the app or the Raw JSON tab;
hand edits are fine as long as they validate (the Raw JSON tab shows the errors).
