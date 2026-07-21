# Spec C — Task 7: CLI Audit (executor calls vs current omnidroid)

**Date:** 2026-07-21
**Result: CLEAN — every `engine_*` call in `main.py` validates against the
current omnidroid subparser surface. Zero drift found; no fixes needed.**

Method: extracted every `run_engine([...])` argv from `omni-executor/main.py`
and checked each command + flags against `omnidroid/omnidroid/engine.py`'s
`add_parser(...)` definitions.

| Executor call (argv) | omnidroid signature | Verdict |
|---|---|---|
| `version --json` | `version` + `--json` (now also returns `modes`, Task 1) | ✅ |
| `doctor --json` | `doctor` + `--json` | ✅ |
| `bases --json` | `bases` + `--json` | ✅ |
| `use-base <tag>` | `use-base` positional `tag` | ✅ |
| `setup` | `setup` | ✅ |
| `list --json` | `list` + `--json` (account listing comes from here) | ✅ |
| `login` (browser) | `login` (default interactive browser) | ✅ |
| `login --token-file <path> --json` | `login` + `_token_args` (`--token-file`) + `--json` | ✅ |
| `start <name> --json [--mode M] [--place P]` | `start` + name + `--mode` + `--place` + `--json` | ✅ |
| `stop <name> --json` | `stop` + name + `--json` | ✅ |
| `remove <name> --json` | `remove` + name + `--json` | ✅ |
| `view <name> --start` | `view` + name + `--start` | ✅ |

## Removed / no-longer-called (Tasks 3 & 5)
- `create <name>` — the executor no longer calls it (it was removed from
  omnidroid); replaced by `login` (browser / `--token-file`).
- `accounts` / `session` as `run_engine` commands — no longer invoked
  (`session` was only inside the deleted embedded-viewer path; account data
  comes from `list --json`).

## Fixes applied by this audit
None — no drift remained after Tasks 1–6.

## Notes for the live smoke (Task 8)
- The executor bundles a PREBUILT `omnidroid(.exe)`. The Task-1 `modes` field
  (and any other recent engine change) reaches the running GUI only after that
  binary is rebuilt, OR by pointing `OMNIDROID_ENGINE` at the updated omnidroid
  source. Do this before the smoke so `engine_modes()` shows `farming`.
