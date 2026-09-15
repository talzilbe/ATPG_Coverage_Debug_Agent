# Launch profiles

One JSON file per project.  A profile describes how to reach the vendor viewer:
the project-setup wrapper, the environment it needs, the tool binary, and the
ordered commands that load a design.

**Adding a project is dropping a file in here.**  No code changes, and no
project identifier belongs anywhere in `atpg_coverage_debug_agent/` — a test
enforces that.

Set `ATPG_TOOL_PROFILES` to a colon-separated list of directories to add your
own; they are searched before this one, so a file here can be shadowed without
editing it.

## Fields

| Field | Meaning |
| --- | --- |
| `name` | Identifier used by `--vis-profile` and stored in settings. |
| `display_name` | Shown in the GUI profile picker. |
| `shell` | Interpreter for the generated scripts. |
| `psetup.executable` | Project-setup wrapper. |
| `psetup.proj` / `.cfg` | Defaults for the `-proj` / `-cfg` arguments. |
| `psetup.command_flag` | Flag that runs a command inside the setup shell. `-x` keeps the shell open afterwards; `-cmd` exits. |
| `environment` | `setenv` lines written into the inner script. A variable whose name contains `LICENSE`/`LICENCE` is the one the GUI's licence field overrides. |
| `tool.executable` | Vendor tool binary. |
| `tool.dofile_flag` / `.logfile_flag` / `.replace_flag` | How the tool is pointed at the generated dofile and log. |
| `commands.context` | First command inside the tool. |
| `commands.load[]` | Ordered design-loading commands — see below. |
| `commands.open` | The command that opens the viewer. |
| `commands.fault_inspect[]` | Templates for "Inspect in Visualizer". `{fault}` and `{stuck}` are substituted; `{fault}` is brace-quoted for Tcl. |
| `commands.signal_inspect[]` | Structured actions for "Open signal in Tessent Visualizer" — see below. |
| `control` | The live command channel opened inside the running session — see below. |
| `terminal_preference` | Terminal emulators to try, in order. |

## `commands.signal_inspect[]`

Each entry is a **verb plus options**, never a command string:

```json
{ "verb": "add_schematic_objects",
  "label": "Show in the flat schematic",
  "options": { "-display": "flat_schematic", "-highlight": "red" } }
```

The object is supplied at run time and is transmitted as its own field, so
nothing in an object name can be interpreted as code. The first entry is the
default action.

## `control`

| Field | Meaning |
| --- | --- |
| `enabled` | Set `false` to launch without a live channel. |
| `allowed_commands` | Verbs the listener will run. **Enforced inside the tool** — this is the last line of defence, so keep it to viewing commands. |
| `allowed_options` | Option names the listener will pass through. |

The listener binds loopback only, on an OS-assigned port, and every request
carries a per-session token. Widening `allowed_commands` is a deliberate,
recorded act.

Each `commands.load[]` entry:

| Field | Meaning |
| --- | --- |
| `key` | Identifier for the path, used in settings and by the run-directory auto-fill. |
| `command` | Tool command, e.g. `read_faults`. |
| `label` | Shown in the GUI form and in error messages. |
| `switches` | Appended **after** the path, e.g. `["-retain"]`. |
| `required` | Whether a launch may proceed without it. |
| `run_dir_glob` | Glob relative to an ATPG run directory, used by the "derive from run directory" mode. Optional. |

The generated command is `<command> {<path>} <switches...>` — the path is always
brace-quoted so Tcl performs no substitution on it.
