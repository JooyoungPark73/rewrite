---
name: draft-rewrite
kind: draft             # a drafting template (prompts/drafts/), never a review pass
level: sentence
scope: selection        # informational: a rewrite targets one span of one paragraph
allow_rewrites: true    # rewriting is the point
model: null             # null → config default for this level
thinking: null
---
{#-
  Rewrite one span of the manuscript on a comment (an author's, or a review finding's).
  Variables beyond the usual: comment, comment_why, original (the span as it reads now),
  span_kind ("the quoted span" or "the whole paragraph"), paragraph_source (the paragraph
  the span sits in), node_id / says / function / claims_text (the paragraph's spec entry, when
  it has one), section_source, labels, bib_keys, draft_schema (the output format, prose).
-#}
## Rewrite: {{ span_kind }} in paragraph {{ node_id or "(not in the spec)" }}

### The comment to act on

{{ comment }}
{%- if comment_why %}

Why it matters: {{ comment_why }}
{%- endif %}

### The text to rewrite ({{ span_kind }})

{{ original }}

{% if says -%}
### The paragraph's entry in the spec

- local message (what the paragraph must say): {{ says }}
- function (its job in the argument): {{ function or "not stated" }}
- linked claims (the only claims it may make, each within its scope):
{{ claims_text or "  (none)" }}

{% endif -%}
### The paragraph as it reads now

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
may be shorter than the original, or empty when the comment says the span is not needed at
all (then return an empty block). Do not touch text outside {{ span_kind }}.

### Output

{{ draft_schema }}

The block holds the replacement for {{ span_kind }} only, as LaTeX: no `% @p` line, no
heading, no `\label`. The `Notes:` line, when needed, is at most two sentences: what the
comment asked that the spec or the evidence does not allow, or a choice the authors should
check. Leave it out when there is nothing to say.
