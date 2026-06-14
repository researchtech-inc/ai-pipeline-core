# `ai-pipeline deploy`

Packages an application's declared units and publishes them onto the framework's supported runtime, ready to run —
and stands up that runtime topology. It is also how full local runtime/API parity is achieved: deploy to a supported
stack stood up where the work is developed, then operate it through the same runtime surface used in production.
There is no `serve` subcommand. `6-tools-and-the-development-loop.md` lists the loop.

## Synopsis

```text
ai-pipeline deploy <package> [--target <runtime>] [--json]
```

## Options

- `<package>` — the application package to publish.
- `--target <runtime>` — which supported runtime to publish onto; the same command deploys to a locally stood-up
  stack or a remote one, differing only in configuration.
- `--json` — emit the structured result: the published deployment and the result of standing up the runtime
  topology.

## Output

The published deployment and the result of standing up the supported runtime topology. After deploy, the pipeline is
launchable through `ai-pipeline run --remote` and the runtime surface (`runtime-api/`); a deployed pipeline accepts
runs promptly without a per-run rebuild. The structured form carries the deployment identity and the topology
result.

## Examples

Publish an application and stand up the runtime:

```text
ai-pipeline deploy review_app
```

Stand up the supported stack locally and deploy to it for full local API parity, then operate it the same way as
production. Here `--remote` means "against the deployed runtime" rather than in-process — the runtime is the local
stack, but it is operated through the same surface a production runtime is:

```text
ai-pipeline deploy review_app --target local
ai-pipeline run review_app --remote --inputs ./inputs.json --run-config ./run-config.json
```

## Guarantees

- An application that would fail pre-run validation does not deploy — the validation gate that protects a run
  protects publication first (`3-guarantees.md § Pre-run validation`).
- Publishing stands up the framework's maintained, versioned runtime topology — the cooperating services a run
  needs — as one verified whole, so the adopter does not reassemble it by hand (`3-guarantees.md § A supported
  runtime`). The application ships no `__main__` or other entrypoint; the runtime loads and runs the declared units.
- The same `deploy` operation stands up a local stack and a remote one; operating the deployed run is identical
  across placements (`3-guarantees.md § Operational parity across placements`).

## Does not guarantee

- Host networking, external exposure, autoscaling, or the trust boundary — who may operate the deployed run and how
  it is exposed to outside callers stay at the platform edge (`4-limits-and-non-promises.md § Out of scope
  entirely`). The compose files, scripts, ports, and topology mechanics are below the contract; this file commits to
  the promise, not the mechanics.
