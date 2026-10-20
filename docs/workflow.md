# Using rewrite: while writing, and for the full review

rewrite keeps a semantic spec (`paper.json`) next to the LaTeX and runs review passes that
report where the text and the spec disagree. The passes never edit anything and never praise;
an empty findings list is the only "good" they can say. This page is the workflow. For what
each level of the spec means and a filled example, see `examples/sample-paper-with-spec/`.

## The prompt set

`prompts/` is copied into each paper's `.rewrite/prompts/` by `rewrite init`. Every run sends
`system.md` + one pass + the spec outline + the numbered `.tex` source to a fresh CLI process.

`system.md` frames every run: the model screens someone else's submitted draft; problems only,
no rewrites, quote verbatim, judge the text against the spec without assuming either side is
right, stay in scope, calibrate severity, JSON only, no tools. The backend enforces the same
rules in code (schema without a replacement field, rewrite and praise filters).

The review passes, coarse to fine, one per spec level:

| Pass | Level | Default scope | What it finds |
| --- | --- | --- | --- |
| `message-drift` | message | paper | abstract, intro and conclusion arguing a different main point than the message; problem, stakes or thesis never stated; a second contribution competing with the thesis; the three parts disagreeing |
| `claims-evidence` | claim | paper | a claim whose evidence never appears or is only asserted; overclaiming beyond the claim's `scope`; evidence that does not support its claim; a claim argued where the outline does not place it; a strong claim missing from the bank |
| `section-promises` | section | paper | a section not delivering its promise; material serving no promise; a figure or table that does not match `figure_story` or is never referenced; ordering that uses things before introducing them |
| `paragraph-function` | paragraph | section | a paragraph doing two jobs or a different job than its `function`; a first sentence that does not state the local message; content off its `says`; a paragraph that belongs elsewhere or has no job |
| `clarity` | sentence | section | abstract subjects, nominalizations, passives hiding the actor, hedges, filler, repeated openings, sentences too long to hold |
| `epistemic-calibration` | sentence | section | sentences stronger or weaker than the evidence in the claim bank, vague quantities where the paper has the number, claims about prior work without a citation |
| `reviewer-2` | message | paper | the skeptical committee member: novelty, weak baselines or unfair evaluation, unstated assumptions, threats to validity, missing related work |
| `latex-hygiene` | sentence | paper | undefined or duplicate labels, malformed citations, inconsistent notation, missing `~` before `\ref`, hard-coded figure numbers; the only pass allowed to show corrected markup |

Two passes are not offered in the Run menu: `ab-compare` serves the blind A/B compare (two
versions of a paragraph in random order, judged against the local message only), and `triage`
judges one run's findings as keep / wrong / trivial; it is the only prompt that ever contains
earlier findings.

Each pass is a Markdown file with YAML front matter (`level`, default `scope`,
`allow_rewrites`, `model` and `thinking` overrides, `style`) and a Jinja2 body. Files are
hot-reloaded: edit a pass, rerun, compare.

### Writing styles: the field's standard of evidence

Subfields of computer science protect different kinds of rigor (Alan Synn, "Same Computer
Science, Different Senses of Reality"): theory asks what can be proved, systems asks what
survives a stated workload, HCI asks what becomes possible for whom. A claim that is sound in
one culture is an overclaim in another. `prompts/styles/` holds one file per culture
(theory-algorithms, pl-formal-methods, systems, machine-learning, ml-systems, computer-vision,
nlp, graphics, hci-design, visualization, robotics), each answering the same five questions:
the field's question, what counts as evidence and the boundary a claim needs, what
performance and contribution mean, the failure modes a reviewer looks for first, and the
vocabulary that fits or misleads.

Pick the paper's style once, in Settings (it is `[paper] style` in `.rewrite/config.toml`).
Its body is appended to `system.md` as "The field's standard of evidence" in every review
pass, so `claims-evidence`, `epistemic-calibration`, `reviewer-2` and the rest judge the draft
the way that field's reviewer would. `latex-hygiene` and `ab-compare` opt out (`style: false`).
A style describes what a reviewer will not accept, never phrasing to use; add a file for your
own venue and it appears in the dropdown.

## Drafting from the spec (separate from review)

Generation and review are two activities with two prompt stacks. The review passes may not
write a word; `prompts/drafts/` does nothing else. On a paragraph row, ⋯ → **Draft from
spec…** asks a model for one paragraph that realizes the node: its local message, its
function, its linked claims (within their evidence and scope), in the voice of the section's
current LaTeX, using only labels and bib keys that exist. The answer arrives in a dialog as
editable LaTeX with the model's notes (what the spec left open, every `\todo` it wrote).
Nothing is written until you choose **Replace paragraph** (the node has a marker; the old text
goes, a `% drafted by …` comment goes in, and the node turns stale so you re-read it) or
**Insert at cursor** (a planned node: the marker and the text are written where the editor's
cursor is, and the node is linked). Drafts are never cached, never appear in the Run menu, and
the field's writing style applies to them as to the passes. Use them for the paragraphs whose
job the spec already states precisely; the review passes then review the result like any text.

## Revising on a comment (yours or a finding's)

Select text in the editor, right-click, **Comment…**, and write what should change ("too
verbose", "these two sentences disconnect; add a bridge", "not needed", "split into two
paragraphs"). The comment becomes a finding of your own: a card in the findings column with a
"comment" badge, the highlight, re-anchoring when the text moves, and the open / addressed /
dismissed states. Every finding card, yours or a pass's, has **Rewrite…**: a model proposes a
replacement for the quoted span or for the whole paragraph, changing only what the comment
asks, within the paragraph's spec entry and the field's standard of evidence. The dialog shows
a word diff and the editable proposal; **Approve** writes it as `\aigen{…}` (one wrapper per
paragraph) with the original kept on a `% aigen-was:` line above, adds the `\aigen` macro to
`main.tex` once, marks the finding addressed, and the paragraph turns stale for re-reading. An
empty proposal drops the span. The header counts "N AI edits pending": the wrappers still in
the paper. Co-authors accept by removing a wrapper and its `% aigen-was:` line (in Overleaf or
here); `latex-hygiene` reports any left before submission, and the commented-out
`\renewcommand{\aigen}[1]{#1}` in the preamble renders them all as plain text for a clean build.

## While writing

1. **Spec before prose, one level at a time.** Fill the message card first (the takeaway,
   then Problem, Why it matters and Argument: the `problem`, `stakes` and `thesis` fields). Then the claim bank, with `evidence` and `scope` for every claim, even for
   experiments not yet run: an empty `evidence` is the to-do list for the evaluation. Then one
   promise per section and its `figure_story`, naming figures by label (`fig:pipeline`), never
   by number. Paragraph rows last, as planned nodes with `says` and `function` and no text yet.
2. **Realize paragraph by paragraph.** Write the paragraph, then adopt it from the Skeleton
   panel (or drag the planned row onto it) so the `% @p` marker links text and node; or let
   ⋯ → Draft from spec… propose the paragraph from its entry and insert it at the cursor. Link the
   claims it argues. The coverage matrix shows a claim argued nowhere or a section arguing
   nothing.
3. **Run narrow passes as you go.** After drafting a section, run `paragraph-function` on it
   (its default scope). Once the intro and conclusion exist, run `message-drift` once. Leave
   `clarity` and `epistemic-calibration` for later: sentence findings on text you will rewrite
   are wasted. A run costs a few cents and about ten seconds with the cheap model.
4. **Reconcile, don't patch.** A finding says the text and a node disagree; decide which side
   is wrong. Text wrong: edit the `.tex`. Spec wrong: edit the node, and the app rings every
   node written against it: a changed claim rings the sections and paragraphs that link it, a
   changed section promise rings its paragraphs, a changed message rings the sections. The
   header counts them ("3 to re-read"); re-read each, edit its `says` or press ⋯ → Reconcile.
   Status dots turn stale the other way round, where the prose changed under a node. Claims
   carry a dot too: "Lift to claim" anchors a claim to its sentence, and the claim turns stale
   when that sentence is reworded.
5. **Snapshot before big changes.** "Flag major revision" in the git panel tags the state.
   After rewriting a paragraph, the blind A/B compare sends old and new to a fresh call with
   only the spec's local message; the verdict does not know which is the edit, so it checks
   whether the rewrite served the point, not whether it reads nicer to you.

## Full review when the draft is done

Run the passes in spec order, whole paper, with a strong model and high or xhigh thinking for
the coarse ones, and fix each level before the next: problems at a coarse level change the
text below, so sentence findings found first are wasted work.

1. `message-drift` and `reviewer-2`, scope paper. These decide whether the paper argues the
   right thing. Expect to edit the message card or the claim bank, then re-read the intro and
   conclusion.
2. `claims-evidence`, scope paper. Every claim is checked against where its evidence appears
   and against its `scope`. An overclaim becomes a weaker sentence or a wider `scope`.
3. `section-promises`, scope paper, then `paragraph-function` with **Fan out**: one call per
   section with the full outline as context, three at a time, plus a message-drift call over
   the abstract, introduction and conclusion.
4. `clarity` and `epistemic-calibration`, per section, cheap model. Use **Run both** (claude
   and codex on the same pass) where a second opinion is worth it: findings both runners
   report come first in the merged list and are the ones to trust.
5. `latex-hygiene` last, once the text is stable.

For each run, press **Triage** before working the list; it marks findings wrong or trivial and
"Dismiss all flagged" clears them. Work the rest with `j` and `k`, marking each addressed or
dismissed. Rerunning an unchanged scope is served from the cache, so after edits you pay only
for what changed. When the coarse passes come back empty, the argument is sound and what
remains is wording.
