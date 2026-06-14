# Designing the Document Model

This step decides which state becomes a durable document, what each document means, and how documents move between
phases. It is design guidance; the enforceable document contract — factories, revisions, provenance, bundles,
attachments — is in `api/2-documents.md`.

Documents are the durable memory of the run. The framework persists every document, records its provenance, and
makes it available for replay and inspection — a strength for meaningful state and a liability for scaffolding.

- **Document or transient value?** Apply the investigator test: would someone investigating this run later care to
  read this artifact on its own? If yes, it is a document. If no — transport wrappers, prompt scaffolding, local
  accumulators, lookup tables, convenience groupings — keep it as a `FrozenBaseModel` value or a Python local.
  Promoting scaffolding into documents creates false lineage and buries real evidence.
- **Durable record versus active state.** Every meaningful document is preserved durably; only the documents a
  phase names in `f.outputs` become live context for later phases. A document can be worth preserving without being
  handed forward. Design handoffs because a later phase genuinely needs them — not because persistence otherwise
  feels uncertain — and let the phase output boundary stay narrow. A task does not forward an unchanged input as
  cargo; `api/6-tasks.md` rejects that shape.
- **Provenance carries meaning, not just ancestry.** Choose the correct factory for each relationship: content
  sources (material that contributed content), causal sources (work that triggered creation without contributing
  content), external source (content that entered from outside), prior revision (a later revision of the same
  artifact). The factory names are the provenance kinds; `api/2-documents.md` is normative. Provenance improves
  inspectability, not correctness — a perfectly traceable wrong document is still wrong; its value is that a
  reviewer can see where the error entered.
- **Design for document-backed citations.** Evidence-grounded prose is modeled with `CitedText`, whose citations
  resolve to durable documents in the run (`api/3-content-models.md`); a bare identifier in prose is not a
  citation. Capture external evidence as a document before generated prose cites it, and decide which outputs carry
  cited claims so the evidence they rest on exists as a document in scope.
- **Separate content from control.** When a document carries both analytical meaning and typed fields code routes
  on, keep explanatory content and routing fields distinct, with explanatory fields first. A routing decision
  expressed as a bare flag with no explanation is a design smell; a control document should pass the investigator
  test for *why* the run took a path.
- **Operational side effects become receipt documents.** A task that submits, exports, notifies, or schedules
  external work returns a receipt document recording what was done and the identity needed later, not a
  fire-and-forget call (`api/6-tasks.md § Side effects produce receipt documents`).
- **Root inputs enter at the boundary, not from tasks.** A run's root inputs are documents supplied from outside at
  the run boundary; the driver, tool, or runner constructs them and hands them in. Task code never
  mints a root document — tasks create only non-root documents through provenance-recording factories. Identify
  your run's root inputs as part of the document model, knowing they arrive from outside the pipeline
  (`api/2-documents.md`).
- **Composite handoffs.** When several artifacts always travel together and form one unit of downstream state,
  group them in a `DocumentBundle`, not a loose collection — but only when they are genuinely one unit of meaning.
- **One document is one artifact.** A document carries one meaning. When several artifacts mean different things and
  are consumed separately, they are distinct document types; when they always travel together as one unit of
  downstream state, they are a `DocumentBundle`. The framework selects documents by type, not by name, so a single
  type cannot stand in for several differently-meaning parts distinguished by a filename (`api/2-documents.md`).
- **Descriptive and summary prose is content, not wrapper metadata.** A document's wrapper carries its framework
  identity, optional name, and provenance — not descriptive prose. A human-readable summary or description that
  travels with a document is a field on its typed content model, where it is typed, validated, and visible to a
  reader (`api/2-documents.md`).
- **Preserve ambiguity.** Design document types that can hold provisional, contradictory, or unresolved state.
  Search silence is not absence; agreement is not proof of independence. Forcing early certainty into the record
  turns a fabricated clean artifact into a stable upstream lie for every later phase. Represent abstention as a
  typed response and carry it forward (`api/4-prompt-contracts.md § Abstention is a valid response, not a failure`
  and `api/6-tasks.md § Insufficient evidence is carried forward, not overwritten`).
- **Design externally read documents to stand on their own.** Some documents are internal working state; others
  are part of what an outside reader or the delivered result may consume. For a document that may be read outside
  the run, carry its meaning in typed content so a reader that cannot load the producing application's code can
  still interpret it. Decide this per document type as you design the model.
- **Cross-run reuse is deliberate.** Documents whose meaning is valuable, comparable, and trustworthy beyond the
  run that produced them are rehydrated by identity into later runs through the advanced database and runner surface
  (`advanced-api/`). Design those reusable meanings explicitly; do not plan to mine arbitrary historical artifacts
  later, and do not over-promote every local artifact into long-lived scope.
