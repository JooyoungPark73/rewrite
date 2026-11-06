---
name: draft-rewrite
kind: draft             # a drafting template (prompts/drafts/), never a review pass
level: sentence
scope: selection        # informational: a rewrite targets one span, in a paragraph or across several
allow_rewrites: true    # rewriting is the point
model: null             # null → config default for this level
thinking: null
---
{#-
  Rewrite one span of the manuscript on a comment (an author's, or a review finding's). The
  span lies in one paragraph or runs across several paragraphs of one section.
  Variables beyond the usual: comment, comment_why, original (the span as it reads now),
  span_kind ("the quoted span", "the whole paragraph" or "the whole paragraphs"),
  paragraphs (each paragraph the span touches, in order: id, says, function, claims_text,
  source), kept_markers (the ids of the `% @p` lines inside the span, which the answer keeps),
  paragraph_source (the paragraphs as they read, with the markers between them),
  section_source, labels, bib_keys, draft_schema (the output format, prose).
  Still set for copies of the older template: node_id / says / function / claims_text (the
  first paragraph's spec entry).
-#}
{%- set several = paragraphs | length > 1 -%}
## Rewrite: {{ span_kind }} {% if several %}across {{ paragraphs | length }} paragraphs{% else %}in paragraph {{ node_id or "(not in the spec)" }}{% endif %}

### The comment to act on

{{ comment }}
{%- if comment_why %}

Why it matters: {{ comment_why }}
{%- endif %}

### The text to rewrite ({{ span_kind }})

{{ original }}

{% for p in paragraphs if p.says -%}
### {% if several %}Paragraph {{ p.id }}'s{% else %}The paragraph's{% endif %} entry in the spec

- local message (what the paragraph must say): {{ p.says }}
- function (its job in the argument): {{ p.function or "not stated" }}
- linked claims (the only claims it may make, each within its scope):
{{ p.claims_text or "  (none)" }}

{% endfor -%}
### The {% if several %}paragraphs as they read{% else %}paragraph as it reads{% endif %} now

{{ paragraph_source }}

### The section as it reads now

{{ section_source or "(no section text)" }}

### The spec

{{ spec_outline }}

### Labels in the paper

{{ labels or "(none)" }}

### Bibliography keys in the paper

{{ bib_keys or "(none)" }}

### What to do

Rewrite {{ span_kind }} so that it answers the comment, and change nothing the comment does
not ask for: the same claims within the same scope, the same facts, numbers, citations and
references, the authors' voice and notation. If the comment asks to split into two
paragraphs, separate them with one blank line. If the comment asks to drop text, the rewrite
may be shorter than the original{% if not kept_markers %}, or empty when the comment says the span is not needed at
all (then return an empty block){% endif %}. Do not touch text outside {{ span_kind }}.
{%- if kept_markers %}

The text runs across paragraphs of the spec, and each of them after the first starts with
its own `% @p` line ({% for m in kept_markers %}`% @p {{ m }}`{% if not loop.last %}, {% endif %}{% endfor %}). Those lines link the text to
the spec: keep every one of them exactly as it is, on a line of its own, in the same order,
each above its own paragraph's text. Within that, do what the comment asks across the
boundary: a bridge from one paragraph into the next, a sentence moved to the paragraph whose
job it serves, a transition that no longer repeats what the previous paragraph said. Leave
some text under every `% @p` line, and keep each paragraph doing the job its spec entry
gives it; a paragraph the comment would merge away or drop is for the authors to remove
from the spec (say so in the notes). A paragraph the comment does not concern stays word for
word as it is.
{%- endif %}

### Output

{{ draft_schema }}

The block holds the replacement for {{ span_kind }} only, as LaTeX: {% if kept_markers %}the
`% @p` lines above kept as they are and no other,{% else %}no `% @p` line,{% endif %} no heading,
no new `\label` (keep every `\label` the text already has: other parts of the paper refer to
it). The `Notes:` line, when needed, is at most two sentences: what the comment asked that
the spec or the evidence does not allow, or a choice the authors should check. Leave it out
when there is nothing to say.
