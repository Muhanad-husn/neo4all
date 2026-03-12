# neo4all — Video Presentation Script

**Target duration**: 27–29 minutes (~130 wpm)
**Audience**: Researchers, analysts, knowledge workers; developers evaluating the tool
**Tone**: Educational, honest, conversational — no marketing language
**Recording format**: Narration over live screen sessions with real test material (`sample_material/`)

---

## Segment 1 — Graph Networks in Everyday Life

**~260 words · ~2 min · No screen recording**

Think about the last time you used a navigation app. You typed in a destination, and within seconds, the app calculated the fastest route through a network of intersections and roads. That network is a graph. Intersections are nodes, roads are edges, and properties like speed limits or traffic density sit on each edge.

Graphs are everywhere. When a social platform suggests people you might know, it walks a graph of friendships. When a streaming service recommends a film, it traverses connections between viewers, genres, and ratings. When a researcher maps citations between papers, that citation network is a graph.

The structure is simple: things, and the relationships between things. A person works at a company. A drug targets a protein. A regulation references a statute. Node, edge, node. Once data lives in that shape, you can ask questions that flat tables struggle with. How many hops separate two people? Which entity connects two otherwise unrelated clusters? Where are the dead ends?

Graph databases like Neo4j are built for exactly these questions. They store relationships as first-class citizens — not as foreign keys buried in join tables, but as direct, navigable connections. The query language, Cypher, reads almost like English: "find all people who work at companies located in Berlin."

So the technology exists. The query language exists. The use cases are clear.

But here is the question nobody talks about enough: where does the graph data actually come from? Someone has to read the documents, identify the entities, and map the relationships. And that is where things get difficult.

---

## Segment 2 — The Bottleneck: Data Entry

**~320 words · ~2.5 min · No screen recording**

Imagine you have a two-hundred-page research report. You need to extract every person, organization, location, and event mentioned in it, then map every relationship between them. Person A funded Organization B. Organization B operated in Location C. Event D involved persons A and E.

The traditional approach looks like this. You read the document. You decide on a schema — what types of entities matter, what types of relationships connect them. Then you open a spreadsheet or a data entry form, and you start typing. Entity by entity. Relationship by relationship. Page by page.

It is like reading a newspaper and hand-writing every name, place, and event on individual index cards, then connecting them with colored string on a corkboard. It works. People have built extraordinary knowledge bases this way. But it does not scale. A single dense report might take days of manual work. A corpus of fifty reports? Weeks. And the consistency problem compounds — different annotators make different decisions about what counts as an entity, how names should be normalized, which relationships are worth capturing.

This is the bottleneck that motivated building neo4all. Not because manual curation is wrong — it is essential. But because the mechanical labor of reading, extracting, and structuring can be assisted by machines, while the judgment calls remain with the human.

Large language models can read a chunk of text and identify entities and relationships with reasonable accuracy. They can propose a schema given a domain description. They can flag probable duplicates and structural inconsistencies. What they cannot do — and what this tool never allows them to do — is make the final decision.

**[Human in the Loop]** Even when the extraction is automated, the human defines what matters. The human approves the schema. The human reviews the proposals. The human clicks "execute." That division of labor is not a limitation of the tool. It is the design.

---

## Segment 3 — The Philosophy: AI as Steerable Tool

**~200 words · ~1.5 min · No screen recording**

Two principles run through every layer of this application.

First: AI is a powerful tool if we know how to steer it. Language models are remarkably good at reading text and producing structured output. They are also capable of hallucinating confidently, drifting off-task, and generating plausible nonsense. The way to get value from them is to constrain their role — give them a specific job, a specific format, and a specific scope, then verify their output before it touches anything permanent.

Second: the human stays in the loop. Not as a rubber stamp, but as the decision-maker at every consequential step.

These two principles produce a concrete architecture. Every change to the graph flows through the same pipeline: Proposal, then Approval, then Deterministic Diff, then Execution, then Audit. The language model composes the proposal. A human approves or rejects it. A non-LLM diff builder translates the approved proposal into concrete graph operations. A tools-only execution agent applies those operations. An immutable audit record captures what happened.

**[Human in the Loop]** This pipeline is not optional. It is not a feature you can toggle off. It is the architecture. The LLM never writes to the database. It writes memos. You decide what to file.

---

## Segment 4 — Prerequisites and Setup

**~380 words · ~3 min**

Before we touch the application, you need two external accounts. Both are free to start with, and both put you in control of your own infrastructure.

The first is Neo4j Aura. This is a cloud-hosted graph database. Neo4j offers a free tier that is more than sufficient for experimenting. You will get a connection URI, a username, and a password. Think of it as renting a storage unit for your graph — you decide what goes in, and you hold the key.

`[PLACEHOLDER: Show Neo4j Aura console — creating a free-tier instance, copying the connection URI and credentials]`

The second is an OpenRouter API key. OpenRouter is a unified gateway to language models from multiple providers — Anthropic, OpenAI, Google, Meta, Mistral, and others. Instead of signing up with each provider separately, you get a single API key and a single billing dashboard. Think of it as a travel booking site for AI: you tell it what you need, it routes to the right provider at the right price.

`[PLACEHOLDER: Show OpenRouter dashboard — creating an API key, briefly showing the model list and pricing page]`

Why these two services specifically? Neo4j Aura because the application uses Cypher and the Neo4j driver natively — there is no abstraction layer that could swap in a different graph database. And OpenRouter because the application assigns different models to different tasks, and OpenRouter makes it straightforward to switch between them without managing multiple API keys.

You will also need Docker installed on your machine. The application runs as a set of containers, so Docker Desktop or Docker Engine is the only local dependency.

That is the full list. A Neo4j Aura instance, an OpenRouter API key, and Docker. No Python installation required on your machine, no virtual environments to manage, no dependency conflicts to resolve. Everything else runs inside containers.

**[Human in the Loop]** Choosing your infrastructure is your first decision. You pick the graph database instance. You pick the LLM provider. You control the API keys. The tool connects to your services — it does not provision them on your behalf or make choices about where your data lives.

---

## Segment 5 — Why OpenRouter: Model Assignment and Cost

**~320 words · ~2.5 min · No screen recording**

A quick note on why the application uses OpenRouter rather than calling a single model directly. Different tasks have different requirements.

Schema proposal needs a model that is good at understanding domain descriptions and producing structured JSON. It benefits from a large context window but does not need to be the fastest model available.

Extraction — where the model reads a chunk of text and identifies entities and relationships — runs once per chunk. If you have a hundred chunks, that is a hundred LLM calls. Here, cost per token matters more than raw capability, because the task is relatively constrained: read this text, fill out this structured form.

The agent pipeline — where AI reviews curation candidates and composes proposals — benefits from stronger reasoning, because the model needs to weigh evidence and make judgment calls about whether two entities are truly duplicates.

OpenRouter lets the application assign different models to different jobs. The defaults are sensible starting points, but you can override them. If you want to use a cheaper model for extraction and a more capable one for curation, you can. If you want to run everything through a single model for simplicity, you can do that too.

The practical benefit is cost awareness. Language model pricing varies by orders of magnitude — a call that costs a fraction of a cent on one model might cost several cents on another. For a pipeline that might make hundreds of calls per run, those differences compound. OpenRouter's dashboard shows you exactly what each call cost, broken down by model and by task.

**[Human in the Loop]** You can change the model at multiple points in the UI — during schema proposal, before extraction, and for the agent pipeline. The application suggests defaults. You make the call. If a model produces poor results for your domain, you switch it. That steerability is the point.

---

## Segment 6 — Installation and Running

**~240 words · ~2 min**

With your Neo4j Aura credentials and OpenRouter API key ready, starting the application is one command.

`[PLACEHOLDER: Show terminal — running `docker compose up`, watching the six containers start: api, ui, worker, redis, qdrant, rustfs]`

`docker compose up` starts six services. The FastAPI backend on port 8000 handles all business logic — domain services, graph writes, validation. The Streamlit frontend on port 8501 is the UI you will interact with. Redis on port 6379 serves double duty as the task queue and the cache layer. Qdrant on port 6333 is the vector store where document chunks get indexed for similarity search. RustFS on port 9000 provides S3-compatible object storage for artifacts like manifests and audit records. And the ARQ worker runs the background jobs — extraction, the agent pipeline — outside the web request cycle.

Notice what is missing: Neo4j. There is no local Neo4j container. The application connects to your Aura cloud instance. This is intentional — Neo4j Aura handles persistence, backups, and scaling. The local containers are stateless workers that process your data and write results to your cloud graph.

Once all six containers report healthy, open your browser to `localhost:8501`. You are looking at the landing page.

---

## Segment 7 — Landing Page and Session Management

**~200 words · ~1.5 min**

`[PLACEHOLDER: Show Streamlit landing page — Phase 0 credential form with fields for Neo4j URI, username, password, and OpenRouter API key]`

Phase 0 is session initialization. You enter your Neo4j Aura connection URI, username, password, and your OpenRouter API key. The application validates the connection — it will not let you proceed if the credentials are wrong.

Once connected, the application derives a session key from your Neo4j credentials. This means your session state — your schema, your documents, your extraction results — persists across browser refreshes and even across restarts, as long as you connect with the same credentials.

The sidebar shows where you are. Phase indicators track your progress through the workflow. A view selector lets you switch between the main pipeline and supporting views like the graph explorer or the monitoring dashboard. The run summary shows your current run ID and schema version once you have one.

**[Human in the Loop]** You choose which Neo4j instance to connect to. You choose which API key to use. The application does not store your credentials beyond the active session in Redis — they are not written to disk, not logged, not transmitted anywhere except to the services you specified.

---

## Segment 8 — Step 1: Domain Description

**~300 words · ~2.5 min**

`[PLACEHOLDER: Show Phase 1 — domain description text area. Type a domain description for the sample material, at least 2–3 sentences describing the domain, key entity types, and relationships of interest]`

The first real step is telling the application what your domain looks like. This is a free-text description — plain English, no special syntax. You describe the subject area, the kinds of entities you expect to find, and the relationships that matter to you.

This description is not a label. It is the input to the LLM that will propose your schema. The quality and specificity of your description directly shapes the quality of the schema you get back.

Think of it as writing a job brief for a consultant. If you write "analyze this data," you will get a generic response. If you write "this corpus contains reports on pharmaceutical clinical trials; key entities include drugs, target proteins, medical conditions, and research institutions; important relationships include which drugs target which proteins, which institutions sponsored which trials, and which conditions each trial addressed" — now the model has enough context to propose something useful.

The minimum is ten words. But more is better. Name the entity types if you know them. Mention the relationship types that matter to your analysis. If there are domain-specific terms, include them. The model works with what you give it.

**[Human in the Loop]** The words you type here determine the vocabulary of your graph. The AI will propose a schema based on this description, but the description itself is entirely yours. There is no template, no dropdown menu, no predefined ontology. You define the scope. This is the moment where your domain expertise enters the system.

---

## Segment 9 — Step 2: Schema Proposal and Approval

**~420 words · ~3 min**

`[PLACEHOLDER: Show Phase 1 continued — click "Propose Schema," watch the LLM generate node types and edge types displayed in editable tables]`

After you submit your domain description, the LLM proposes a schema. It comes back as two editable tables: node types and edge types.

Each node type has a name, a description, and a `primary_property`. Each edge type has a name, a source node type, a target node type, and a description.

The `primary_property` deserves special attention. This is the field that the system uses to deduplicate entities of that type. If your node type is "Person" and the primary property is "name," then two nodes both named "Alice Johnson" will be flagged as exact duplicates. If the primary property is "email," then two nodes with different names but the same email will be flagged instead.

Think of the primary property as a Social Security Number for each entity type. It is the field that answers the question: "when are two records actually the same thing?" Choose it carefully. A poor choice here means duplicates slip through or distinct entities get merged.

**[Human in the Loop]** The AI proposed this schema, but you can edit every field. You can rename node types, change primary properties, remove edge types that do not matter to your analysis, or add ones the model missed. The proposal is a starting point, not a final answer. You decide what matters.

`[PLACEHOLDER: Show editing a table — rename a node type, change a primary_property, delete an unnecessary edge type. Then click "Approve Schema"]`

When you click approve, the schema locks for this run. This is the most consequential decision in the entire workflow. Every subsequent step — extraction, candidate detection, curation — operates against this schema. The schema version gets hashed and embedded in every artifact. If you realize later that you need a different schema, you start a new run.

**[Human in the Loop]** Once approved, the schema cannot be changed for this run. This is intentional. A locked schema means every artifact produced downstream is traceable to a specific set of definitions. There are no silent schema migrations, no retroactive changes. If the schema needs revision, you make that decision explicitly by starting fresh.

Take your time with this step. Review every node type. Verify every primary property. Check every edge type's source and target. The extraction will only be as good as the schema it follows.

---

## Segment 10 — Step 3: Document Upload and Ingestion

**~240 words · ~2 min**

`[PLACEHOLDER: Show Phase 2 — upload one or more PDF/DOCX files from sample_material/. Show the document list populating after upload]`

With your schema locked, you move to document ingestion. Upload your files — the application accepts PDF and DOCX. Each document gets a deterministic identifier derived from your run ID, the filename, and a hash of the file content.

Behind the scenes, the application runs a three-tier parser. Tier one, Docling, provides full structural parsing — it recognizes titles, paragraphs, tables, images, and captions. If Docling fails or is unavailable, tier two, Unstructured, provides broad format support at somewhat lower fidelity. If both fail, tier three falls back to raw text extraction using PyPDF2 or python-docx — no structure, only the words on the page. Every chunk produced by the raw fallback carries a quality flag so you know the structural parsing did not succeed.

The system also supports incremental reruns. If you upload the same document again with the same content, the application recognizes the matching content hash and parser configuration, and skips re-parsing entirely. Changed the document? It re-parses. Changed parser settings? It re-parses. Same inputs? Cached result.

**[Human in the Loop]** You choose which documents to feed in. The application does not crawl, scrape, or fetch documents on its own. Every document in the pipeline is one you explicitly uploaded. You control the input corpus.

---

## Segment 11 — Chunking

**~260 words · ~2 min · No screen recording**

Once a document is parsed into structural elements, the chunking service splits it into pieces sized for an LLM to process. This is where the application gets opinionated, and for good reason.

Think of it like cutting a newspaper into clips. You would never cut through the middle of a table. You would not split a heading from the paragraph beneath it. You would keep related content together while making sure no single clip is so large that it loses focus.

The chunking service follows three strategies. First, structure-respecting boundaries: tables are always standalone chunks — never merged with adjacent text. Images stay standalone too, with their captions pulled into the same chunk. Headings act as hard boundaries — when the chunker encounters a heading, it flushes whatever it has accumulated and starts a fresh chunk seeded with that heading.

Second, content accumulation: paragraphs and list items accumulate into the current chunk until the next element would push it past a thousand characters or two hundred fifty tokens. Then the chunk flushes and a new one begins.

Third, quality flagging. If any element in a chunk had OCR confidence below 0.7, the chunk gets a `low_ocr_confidence` flag. If the total text is under twenty characters, it gets `low_text_density`. Headers and footers — the kind that repeat on every page — are silently skipped as page noise.

These are not cosmetic labels. Quality flags travel with the chunk through extraction and curation. A reviewer can see at a glance which chunks came from clean structural parsing and which came from degraded source material.

---

## Segment 12 — Indexing

**~190 words · ~1.5 min · No screen recording**

After chunking, each chunk gets embedded into a 384-dimensional vector using a sentence-transformer model and stored in Qdrant, in a collection scoped to your run.

Think of this as building a library card catalog. Each card summarizes what a chunk contains, not in words, but as a point in a high-dimensional space. Chunks about similar topics end up near each other. When the curation layer later needs to find evidence for a candidate — say, all chunks that mention a particular entity — it queries Qdrant by similarity rather than by exact keyword match.

An important distinction: the vector store is evidence-only. It is not authoritative. Neo4j is the source of truth for your graph. Qdrant is a retrieval tool that helps surface relevant chunks when you need to review evidence for a curation decision. Nothing in Qdrant can overwrite or contradict what is in Neo4j. It is a reading aid, not a database of record.

The indexing happens automatically after chunking. There is no user decision here — it is a deterministic pipeline step that prepares the evidence layer for curation.

---

## Segment 13 — Extraction

**~380 words · ~3 min**

`[PLACEHOLDER: Show Phase 3 — click "Run Extraction," watch the progress bar advance as chunks are processed. Show the job status updating in real-time with auto-poll]`

Extraction is where the LLM reads your chunks and identifies entities and relationships. The application creates one background job per chunk, queued through Redis and processed by the ARQ worker. A progress bar in the UI polls automatically, so you can watch the extraction advance.

For each chunk, the model receives the chunk text and your locked schema. Its job is to fill out a structured form: which entities of which types appear in this text, with which properties, and which relationships connect them. The output is structured JSON — never Cypher, never executable code.

This is a critical safety boundary. The LLM does not generate database queries. It fills out a form. The actual graph writes are handled by pre-built tools with injection guards. Every identifier — node labels, relationship types, property names — is validated against a strict regex before it touches a query. All data values flow through Cypher parameters, which are inherently injection-safe. The query templates are static or pre-validated — the LLM never sees them, never modifies them, never generates them.

If the model produces output that does not match the expected structure, it gets rejected. Not corrected, not coerced — rejected. The system logs the failure, and that chunk gets flagged for review. This is the fail-closed behavior described in the architecture: invalid AI output is blocked and logged, never silently accepted.

With a hundred chunks, extraction might take a few minutes depending on your model choice. Faster models cost less per call. More capable models may extract more accurately. This is where the model selection from your OpenRouter configuration makes a practical difference.

**[Human in the Loop]** You chose the model. You triggered the extraction. You can monitor its progress in real-time and review the results before moving to curation. The extraction populates the graph with what the model found, but the curation phase exists specifically to review, correct, and refine those results. Extraction is the first draft, not the final copy.

---

## Segment 14 — Curation

**~380 words · ~3 min**

`[PLACEHOLDER: Show Phase 4 — Layer 1 candidate detection results: a table of flagged candidates grouped by type (exact duplicates, probable duplicates, canonical violations, structural anomalies)]`

Curation is where you clean the graph. It works in three layers, each adding a level of sophistication.

Layer 1 is fully deterministic — no LLM involved. Five detectors scan the graph for issues. Think of this as an automated spell-checker for your knowledge graph.

The exact duplicate detectors find nodes or relationships that share identical deduplication keys. If two "Person" nodes both have the name "Alice Johnson," that is an exact node duplicate, flagged as high severity.

The probable duplicate detector uses Jaro-Winkler string similarity. If two person names score 0.90 or higher — say, "Jonathan Jones" and "Jonathon Jones" — it flags them as probable duplicates at medium severity. Close enough to warrant review, not close enough to auto-merge.

The canonical violation detector checks whether edges follow the direction defined in your schema. If your schema says "Person WORKS_AT Organization" but the graph has the relationship pointing backwards, that gets flagged.

The structural anomaly detector looks for orphan nodes with no connections, degree outliers — nodes with far more connections than average — and entities missing provenance metadata.

`[PLACEHOLDER: Show Layer 2 — click on a candidate, view the evidence panel showing source chunks, compose a proposal (e.g., merge two duplicate nodes), submit for approval. For a high-risk merge, show the two-phase approval: first approve returns a confirmation token, then confirm executes]`

Layer 2 is the manual curation interface. For each candidate, you can view the evidence — the source chunks that mention the relevant entities. You compose a proposal: merge these two nodes, rename this entity, delete that duplicate. The proposal enters the approval queue.

**[Human in the Loop]** Every proposal passes the same gate. Low-risk operations like renaming get single-step approval. High-risk operations — merges and deletes — require two-phase approval: you approve, then you confirm. This is not bureaucracy. A merge is destructive — it rewires relationships and removes nodes. The extra step ensures you meant it.

Layer 3 is the AI agent pipeline. Agents review candidates, gather evidence, and compose proposals. But their proposals enter the exact same approval queue. No shortcut. No auto-apply. The AI proposes, and you decide.

---

## Segment 15 — Results and the Populated Graph

**~300 words · ~2.5 min**

`[PLACEHOLDER: Show the Graph Explorer — paginated node browser with type filter, paginated edge browser, node counts and edge counts. Switch to Neo4j Browser to show the graph visually — nodes and relationships rendered as an interactive network diagram]`

After extraction and curation, your graph is populated. The Graph Explorer in the UI lets you browse nodes and edges by type, paginated at fifty per page, with counts for each type. This is a structured view — useful for verification and spot-checking.

For the visual experience, open Neo4j Browser. Connect to your Aura instance and run a simple Cypher query to visualize a portion of the graph. Nodes appear as circles, relationships as arrows, properties as labels. You can drag, expand neighborhoods, and explore the structure interactively.

The monitoring dashboard gives you a run summary: how many documents were ingested, how many chunks produced, how many entities and relationships extracted, how many candidates flagged, how many proposals approved and executed. This is your audit trail at the aggregate level.

For the detailed audit trail, every executed proposal produces an immutable audit record stored in S3. The record captures what was proposed, who approved it, what diff was applied, and what the outcome was. These records are append-only — nothing gets modified or deleted after the fact.

**[Human in the Loop]** The graph you are looking at is the product of your decisions. You defined the domain. You approved the schema. You uploaded the documents. You chose the model. You reviewed the candidates. You approved the proposals. The AI assisted at every step, but the decisions were yours. The audit trail proves it.

---

## Segment 16 — Closing: The Loop Stays Human

**~200 words · ~1.5 min · No screen recording**

Let me walk back through what we covered. You described a domain. The AI proposed a schema. You edited and approved it. You uploaded documents. The system parsed, chunked, and indexed them. The AI extracted entities and relationships into a structured form. Deterministic detectors flagged inconsistencies. You reviewed evidence, composed proposals, and approved changes through a gate that requires explicit human action at every step.

This is version 0.8.0. It is functional, but it has rough edges. The extraction quality depends on your model choice and your schema design. The curation detectors catch structural issues but do not understand domain semantics. The UI is utilitarian, not polished. These are honest limitations.

If you want to contribute — report bugs, suggest improvements, or extend the detectors — the repository is open. The architecture is documented. The specs in `docs/specs/` describe every increment in detail.

**[Human in the Loop]** The loop stays human. AI proposes, human decides, system records. That is not a tagline. It is the contract between the tool and the person using it. The most powerful thing about a graph database is that it captures how things relate to each other. The most important thing about this tool is that you remain the one deciding which relationships are real.

---

*End of script.*
