# Schema Intake Prompt Template

> **What is this?** A copy-paste prompt you can use in any AI chat platform
> (ChatGPT, Gemini, Claude, etc.) to design a knowledge graph schema for the
> neo4all extraction pipeline. Paste the prompt below into a new conversation,
> then follow the AI's instructions.

---

## How to Use

1. Copy everything inside the **Prompt** section below.
2. Paste it into your AI chat of choice.
3. In the same message (or the next one), provide your sample documents —
   paste text directly, describe your corpus, or attach files if the platform
   supports it.
4. Follow the multi-step workflow. The AI will ask you questions before
   producing the final schema.
5. When done, ask the AI to output the final schema as a JSON file.
   Save it as `schema.json` in your project's `Examples/` directory.
6. In neo4all Phase 1, use the **Upload Schema** tab to load `schema.json`,
   or copy the domain description into the **AI Generate** tab.

---

## Prompt

~~~
You are helping me design a knowledge graph schema for a document extraction
pipeline. I will provide sample documents from my corpus. Your job is to
analyze the samples, ask me clarifying questions, and produce a validated
schema JSON that fits the system constraints below.

This workflow is domain-agnostic. It works for any corpus — legal, medical,
financial, scientific, organizational, technical, or anything else.

─────────────────────────────────
SYSTEM CONSTRAINTS
─────────────────────────────────

The schema must satisfy ALL of the following:

- 1–25 node types, each with a `node_class` from this fixed set:
  Entity, Event, Concept, Document, Location, Asset, Metric, Role
- 1–50 edge types, directed, in SCREAMING_SNAKE_CASE
- Each node type has a `primary_property` (snake_case, extractable from text)
  used for deduplication
- Each edge type has `start_node_type` and `end_node_type` matching defined
  node types
- Properties must be realistically extractable from running text by an LLM
- Node `type` values are PascalCase singular nouns
- No orphan node types — every node must participate in at least one edge

─────────────────────────────────
OUTPUT FORMAT
─────────────────────────────────

The final schema must be a JSON object with this exact structure:

{
  "nodes": [
    {
      "node_class": "<Entity|Event|Concept|Document|Location|Asset|Metric|Role>",
      "type": "<PascalCase>",
      "primary_property": "<snake_case>",
      "qualifier": null,
      "additional_properties": ["<snake_case>", ...]
    }
  ],
  "edges": [
    {
      "start_node_type": "<matches a node type>",
      "end_node_type": "<matches a node type>",
      "type": "<SCREAMING_SNAKE_CASE>",
      "primary_property": null,
      "qualifier": null,
      "additional_properties": ["<snake_case>", ...]
    }
  ]
}

─────────────────────────────────
WORKFLOW — follow these steps in order
─────────────────────────────────

STEP 1 — Receive & Catalog Samples

Read my input (pasted text, file contents, or corpus descriptions).
Produce a corpus catalog:

  CORPUS CATALOG
  ──────────────
  Sources analyzed:     [count and types — e.g. "3 emails, 2 reports, 1 contract"]
  Writing style:        [formal/informal, structured/narrative, technical/general]
  Language patterns:    [how entities and relationships are expressed in the text]
  Information density:  [low / medium / high — how many distinct entities per paragraph]

Then list every distinct category of thing you found in the samples, grouped
naturally (not into pre-set buckets):

  THINGS FOUND IN SAMPLES
  ────────────────────────
  - [category]: [specific examples from the text]
  - [category]: [specific examples from the text]
  - ...

And every distinct type of relationship expressed or implied:

  CONNECTIONS FOUND IN SAMPLES
  ─────────────────────────────
  - [relationship pattern]: [example from text]
  - [relationship pattern]: [example from text]
  - ...


STEP 2 — Ask for Purpose

STOP and wait for my response. Ask me:

  Based on the samples, here's what I found [brief summary].

  Before I design the schema, I need to understand:

  1. What is the purpose of this knowledge graph? What questions should
     it answer? What will people use it for?

  2. What matters most? If I have to choose between capturing [category A]
     in detail vs. [category B], which is more important for your goals?

  3. Is there anything in your full corpus that these samples don't
     represent? (e.g., document types, entity types, or relationships
     that appear elsewhere but not in these samples)

Tailor question 2 to the specific tension you see in the samples. There is
always a tension — the samples will have more entity/relationship variety than
can be cleanly captured. Identify it explicitly.


STEP 3 — Design the Schema

After receiving my answers, produce three sections:

A. Compression decisions — For each category of thing and relationship
from Step 1, state what happens to it:
  - Gets its own node/edge type (and why it earned a slot)
  - Folded into another type via a property (which property, and why)
  - Omitted entirely (and why it's safe to drop)

B. Domain description — A paragraph (150–300 words) optimized for an
LLM-based schema generator, in case I want to use an AI Generate path.
This paragraph should:
  - Name the document types
  - Highlight entity types weighted toward the stated purpose
  - Emphasize the relationship patterns that matter most
  - Mention anything unusual or domain-specific
  - NOT list node/edge types directly

C. Recommended schema — The JSON schema object (using the output format
above). For each type, add a brief note explaining:
  - What it represents and what it absorbs
  - Why the primary_property was chosen
  - What the qualifier/additional_properties capture


STEP 4 — Review

STOP and wait for my response. Present the schema and ask:

  Review the schema above.

  - Any categories I dropped or merged that you need kept separate?
  - Any relationships missing that matter for your stated purpose?
  - Want me to shift the budget (e.g., fewer node types, more edge types)?

  Once you're satisfied, you can:
  1. Copy the JSON and paste it into the Upload Schema tab in Phase 1
  2. Copy the domain description and use the AI Generate tab instead

  I can also iterate — tell me what to change.


STEP 5 — Iterate

If I request changes, adjust and re-present. Repeat until I approve.
On each iteration, show only what changed (diff-style) plus the full updated
JSON.


STEP 6 — Export

Once the schema is approved, output the final JSON as a standalone code block
with a filename annotation so I can save it directly:

  ```json schema.json
  { ... }
  ```

This file should contain ONLY the raw JSON schema object (no comments, no
notes). It is ready to upload into neo4all Phase 1 via the Upload Schema tab.

─────────────────────────────────
PRINCIPLES
─────────────────────────────────

- Discover, don't assume. Extract categories from the actual samples. Do not
  impose a predetermined taxonomy.
- Purpose drives compression. When something must be folded or dropped, the
  user's stated purpose decides what survives.
- Properties over types. When two categories share the same relationship
  patterns and deduplication logic, merge them into one type with a
  discriminating property. Only create separate types when they have genuinely
  different graph connectivity.
- Primary property is king. Every primary_property choice must be justified:
  it must be stable, canonical, extractable, and the natural way a human would
  identify that entity.
- Edges must be extractable. If a relationship can't be detected from text by
  an LLM reading one chunk at a time, don't include it.
- No junk-drawer types. Avoid generic types like "Thing", "Item", "Misc". If
  something doesn't fit cleanly, fold it or drop it — don't create a catch-all.

─────────────────────────────────

Here are my sample documents:
~~~

*(Paste your sample documents after the prompt, or attach files if supported.)*
