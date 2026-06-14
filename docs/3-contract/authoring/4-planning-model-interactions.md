# Planning the Model Interactions

This step decides which model-mediated judgments the application makes, what each one sees and produces, and how
its prompt body operationalizes the question. It is design guidance; the enforceable contract for prompt contracts,
tools, methodology, validation, and continuation is in `api/4-prompt-contracts.md` and `api/5-model-tools.md`.

## Plan interactions before writing prompts

Plan every model-mediated judgment before writing any prompt. An unplanned interaction has no governing design and
gets shaped by prompt convenience instead of business need. Each planned interaction becomes exactly one
`PromptContract`.

- **Decide whether a judgment needs a model at all.** Use code for values the system already knows, services for
  retrieval and conversion and provider mechanics, and a model only for semantic work: interpreting evidence,
  synthesizing meaning, comparing explanations, classifying open-world material, drafting reasoning-bearing prose.
- **One judgment per contract.** Each contract serves one coherent analytical deliverable a reader can name. One
  judgment is not one sentence, but it is not several unrelated deliverables hidden behind one output model either.
- **Scope evidence deliberately.** Each contract receives exactly the evidence its judgment needs, and the complete
  evidence for that scope. Scoping is not trimming: too much context distracts and biases, too little fabricates
  certainty. Pass evidence as document inputs, not as broad active state.
- **Structured versus narrative output is a design decision.** If code must route, gate, filter, or classify on a
  value, it needs a typed field. If the reasoning matters beyond the immediate typed consequence, keep it as
  narrative — and put the explanation before the decision so reasoning precedes commitment. Do not grow the output
  model to patch a prompt-quality problem; a better body file, better evidence scope, or a follow-up step is the
  first fix, and schema growth is justified only when code needs a new typed value.
- **Model tiering by difficulty and stakes.** Choose the model for each judgment by reasoning difficulty, the cost
  of an error (early high-leverage judgments deserve stronger models even when the output looks simple), constraint
  clarity, volume, and narrative quality. Model choice is configuration owned by the pipeline: declare it on
  `RunConfig`, pass the opaque `AIModelRef` into phases and tasks, and let tasks use the model they were given. A
  task does not select its own model or carry a raw model string (`api/8-pipelines.md`, `api/6-tasks.md`).
- **Different model behavior is a different `AIModelRef`, not a per-call option.** When two judgments need different
  model behavior — stronger reasoning effort, a different temperature, a search-enabled or URL-preserving
  capability — declare distinct `AIModelRef` variants at the configuration boundary and choose among them through
  `RunConfig` tiering. There is no per-call options object on `.execute(...)`: behavior is selected by the variant,
  and per-call framing or instructions belong in the body file or an attached `Methodology`. `AIModelRef` is opaque
  — application code never inspects, compares, branches on, renders, or pickles it; if an output must name which
  model produced a result, that label is a value the author carries deliberately, not read from the handle
  (`5-configuration.md`).
- **Multi-step patterns are declared.** When a judgment needs several turns, plan the pattern: analysis then
  extraction, draft then critique then revision, progressive refinement, generate then validate then correct, or
  fresh-context review. When the model must build on its own prior reasoning, declare conversation continuation
  with `Continues.once(...)` or `Continues.repeating(...)`; when a follow-up needs only the prior answer as data,
  pass that response as a typed input. A reviewing judgment that must stay independent declares no continuation
  lineage to the contract it judges — its independence is then provable at import time
  (`api/4-prompt-contracts.md § Multi-step continuation is declared, not improvised`).
- **Tools are declared on the contract.** A contract names the bounded capabilities the model may call and the
  per-tool call budget; bindings are supplied at execution (`api/5-model-tools.md`). A tool having run is not proof
  of coverage — coverage is recorded in the output model and checked by validation.
- **Plan cited outputs deliberately.** A judgment that must ground claims in evidence returns cited prose whose
  citations resolve to durable documents in scope; plain narrative with citations reconstructed afterward is not
  the same thing. Ensure the evidence such a judgment cites already exists as a document, and do not treat a tool
  or a search having run as equivalent to cited support (`api/4-prompt-contracts.md`, `api/5-model-tools.md`).

Runtime control, delivery, and observation are not `PromptContract` concerns. A `PromptContract` owns one
model-mediated judgment; launching a run, watching it, halting or recovering it, delivering its outputs, or
notifying an outside system belongs to the runtime surface (`runtime-api/`), not to prompt structure.

## Write the prompt body file

Each non-exempt `PromptContract` has a paired markdown body file that supplies the analytical instructions the
model reads. The class metadata frames the judgment (`purpose`, `returns`, `success_criteria`); the body file is
where the judgment is operationalized. The discovery rules, the `.md`/`.md.j2` distinction, the no-H1-header rule,
and the template context a `.md.j2` file may reference are normative in
`api/4-prompt-contracts.md § Paired body file supplies the operational instructions`.

The authoring judgment the body file carries is operationalizing the question so it does not fork. A judgment
defined only in soft words — relevant, strong, reputable, good enough — produces a different honest answer on every
run, and that divergence is a defect in the question, not the model. Make the question answerable: give the rubric,
the thresholds, the boundary cases, and worked examples that pin down what a correct judgment is. Keep
case-specific evidence in the contract's input fields, not in the body file; keep reusable analytical guidance in a
`Methodology` attached to the contract. The body file states what to decide and how to decide it, not what the
framework does with the result.
