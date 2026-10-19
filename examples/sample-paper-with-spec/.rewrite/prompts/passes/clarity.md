---
name: clarity
level: sentence
scope: section          # default scope
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
{# Sentence clarity: characters as subjects, actions as verbs, nominalizations, passives, hedges, filler, repetition. #}
## Pass: sentence clarity

Scope: {{ scope_name }}

### The spec (context: what each passage is trying to say)

{{ spec_outline }}

### What to check

Read each sentence in the source. A clear sentence names its main characters as
grammatical subjects and puts their actions in verbs. Report sentences where:
- the subject is an abstraction or a nominalization while the real actor is buried
  elsewhere in the sentence;
- the main action is a noun ("performs an evaluation of") instead of a verb;
- a passive construction hides who acts, when the actor matters;
- hedges ("somewhat", "arguably", "it seems that") or filler ("in order to", "it is
  important to note that", "basically") add words without meaning;
- the same phrase or sentence opening repeats close enough to be noticed;
- the sentence is long enough that the reader loses its subject before the verb.

Do not report choices that are matters of taste. Describe the problem; do not supply a
reworded sentence.

### Source

{{ source }}

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

Use `"level": "sentence"` and one of these categories: `abstract-subject`,
`nominalization`, `passive`, `hedge`, `filler`, `repetition`, `long-sentence`. Quote the
sentence (or its offending clause). Set `json_ref` to the id from the nearest `% @p` line
above the sentence, or null. If you find no problems, return `{"findings": []}`.
