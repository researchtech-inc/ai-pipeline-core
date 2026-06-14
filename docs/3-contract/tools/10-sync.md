# `ai-pipeline sync`

Synchronizes run records between local storage and a shared store. It is the operator face of the database API in
`advanced-api/2-database.md` for moving records between the configured stores.
`6-tools-and-the-development-loop.md` lists the loop.

## Synopsis

```text
ai-pipeline sync <run-id> [--to local|shared] [--json]
```

## Options

- `<run-id>` — the run whose record to move.
- `--to local|shared` — the destination store; sync pulls a shared run local to work against it, or pushes a local
  run to a shared store for others to inspect.
- `--json` — emit the structured result: the synchronized records and their identities.

## Output

The synchronized records. The structured form lists the records moved and their preserved identities.

## Examples

Pull a production run into a local snapshot to benchmark and replay it offline:

```text
ai-pipeline sync review-2026-06-14 --to local
```

Push a local run to a shared store for others to inspect:

```text
ai-pipeline sync review-2026-06-14 --to shared --json
```

## Guarantees

- A synchronized record preserves run identity, document identity, and provenance, so a run inspected after a sync
  is the same run, and rehydrating a document by identity resolves to the same document.

## Does not guarantee

- Anything about which backend backs either side; backend selection is configuration (`5-configuration.md`), and the
  synchronized behavior is identical across backends.
