# Samovar

Samovar is a command-line research tool for analyzing Russian-language open-source material. It connects Python collectors, model-assisted classification and investigation, and analyst review in a local project directory.

Each project has its own sources, taxonomy, lexicon, SQLite database, and reports. The default taxonomy covers AI misuse and safety threats; you can adapt it in `samovar.yaml` for a different research question.

Samovar is an early-stage tool for analyst-led research. Model output is an assessment to check against the source material, not independent verification.

## Workflow

1. **Collect** posts from configured Python scripts that emit JSON Lines.
2. **Classify** posts using the project taxonomy and lexicon.
3. **Review** uncertain classifications, unfamiliar terms, and escalations at an interactive checkpoint.
4. **Investigate** selected posts for additional context.
5. **Validate** medium- and high-severity findings with a separate model review.
6. **Report** the current reviewed findings, with unresolved escalations identified separately.

You can run each step directly. `samovar run` asks a coordinator model to propose a plan, validates that plan, and executes it. The plan depends on the current project state; it is not a fixed sequence of every step.

## Installation

Requirements:

- Python 3.11 or later.
- Git for project initialization.
- Claude Code, Codex CLI, or both, installed and authenticated before running model-assisted commands.

From this repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
samovar --help
```

The Python package installs PyYAML and includes the project templates and agent instructions. Agent CLIs and their service access are separate requirements.

## Choose an agent backend

Samovar supports Claude Code and Codex CLI. Authenticate each CLI you intend to use through its own login flow:

```bash
claude auth login
codex login
```

Claude is the default. To select a backend for one command, put `--agent` before the command:

```bash
samovar --agent codex classify
samovar --agent claude validate
```

For persistent routing, add an `agents` section to `samovar.yaml`:

```yaml
agents:
  default: claude
  review: codex
```

In this example, Claude handles most steps and Codex handles the model review invoked by `samovar validate`. Other skill names are `coordinator`, `classify`, `investigate`, and `report`. To select specific models, set `agents.models.claude` or `agents.models.codex` to a model name available to your account; otherwise the CLI chooses its default. A command-line `--agent` selection overrides project routing for that command.

Each step uses one backend. Configuring both does not run both models on every item or combine their answers. Samovar uses the CLIs' existing authentication independently and does not copy credentials between them. Account access, model availability, usage limits, and billing depend on the selected provider.

Use trusted, current official CLI versions. Samovar requires safety flags, including Claude's `--safe-mode`, and fails if they are unsupported rather than retrying with fewer restrictions. Managed administrator policies still apply.

### Codex global instructions

Samovar refuses to launch Codex if its configuration directory contains `AGENTS.md` or `AGENTS.override.md`. It checks file metadata, not their contents. You can continue using your existing Codex login when that directory has no global instruction files.

If this check blocks analysis, use a dedicated configuration directory and authenticate there. For example, in a shell where `$HOME/.samovar-codex` is a new, empty directory:

```bash
export CODEX_HOME="$HOME/.samovar-codex"
mkdir -p "$CODEX_HOME"
codex login
samovar classify --agent codex
```

Keep the same `CODEX_HOME` set when running Samovar. The directory must not contain global instruction files. Leave your existing instructions and credentials in place; this creates a separate CLI configuration and login.

## Create a project

```bash
samovar init my-research
cd my-research
```

Setup is a local questionnaire, with no model call. It records the research description and analyst name in `samovar.yaml`, saves an optional collector or dataset path as a hint, and appends supplied terminology to `lexicon/corrections.md` as analyst notes. It does not read the supplied path, generate adapters, or execute a collector.

Configure sources yourself before collecting data. The bundled collector is a template, not a working integration with Telegram, VK, or an imageboard. Review the configuration and any collector code before running it.

To make Codex the new project's default backend:

```bash
samovar --agent codex init my-codex-research
```

Run subsequent commands from the project directory or one of its subdirectories.

```bash
samovar collect              # Collect from all configured sources
samovar classify             # Classify posts that have no classification
samovar review               # Resolve items that need human judgment
samovar validate             # Model review of medium/high-severity findings
samovar review               # Handle any new escalations
samovar report               # Write a Markdown report
samovar status               # Show counts, pending work, and the last run
```

`review` and `validate` are different: `review` is the interactive analyst checkpoint; `validate` runs the review agent. Neither command establishes that a source's claims are true.

To let the coordinator choose the next steps:

```bash
samovar run
```

## Configure a source

Each source in `samovar.yaml` names a Python script and its arguments:

```yaml
sources:
  archive:
    script: sources/import_jsonl.py
    args: ["data/archive.jsonl"]
```

This example assumes you have written `sources/import_jsonl.py`. Start from `sources/collector_template.py` in a new project. A collector can read an export, call an API, or query a database, provided it emits one JSON object per line to standard output. Send logs and progress messages to standard error.

```json
{"post_id":"12345","source":"archive","source_language":"ru","text":"Example source text","url":"https://example.org/posts/12345","thread_url":"https://example.org/threads/42","source_ts":"2026-03-15T10:00:00Z"}
```

| Field | Requirement | Meaning |
| --- | --- | --- |
| `post_id` | Required | Nonempty source-local identifier, as a string or integer. |
| `text` | Required | Post content, as a string. |
| `source` | Optional | Must match the configured source name when supplied. Otherwise the harness fills it in. |
| `source_language` | Optional | Language code, such as `ru`. |
| `url` | Optional | Direct link to the post. |
| `thread_url` | Optional | Link to surrounding discussion for investigation. |
| `source_ts` | Optional | Original timestamp, preferably ISO 8601. |
| `metadata` | Optional | Additional source-specific fields, as a JSON object. |

```bash
samovar collect archive
```

Collectors run with the same Python interpreter as Samovar, from the project directory, with a five-minute timeout. The harness checks the entire output before storing the batch. A nonzero exit, malformed JSON, or invalid record fails collection instead of silently importing only part of it.

Posts are identified by the pair of source name and source-local ID. Repeated collection of the same pair does not create another post or update its existing text. Identical IDs from different sources remain separate.

### Investigate a post

```bash
samovar investigate 12345
```

A source-local ID works when it identifies exactly one stored post. When sources share an ID, use the full internal ID shown in the review output, including shell quotes:

```bash
samovar investigate 'source:["archive","12345"]'
```

Investigation uses the selected post and up to 100 already-collected posts from the same source and thread URL. Agents do not fetch thread URLs or run collectors themselves. A `thread_url` records a source reference; it does not guarantee that the discussion was retrieved. If the available material is insufficient, the investigation should request analyst review. Arrange collection of the needed context through your own collector.

## Review and the project lexicon

The analyst checkpoint includes low-confidence classifications, unknown terms, and unresolved escalations. You can accept a classification, change its label, add a lexicon entry, or leave it pending.

The lexicon consists of three Markdown files:

- `lexicon/slang.md`: terms and their meanings.
- `lexicon/techniques.md`: observed techniques and indicators.
- `lexicon/corrections.md`: previous interpretation errors and corrections.

Investigation agents can propose new terms. These suggestions stay in the investigation records and create an analyst-review escalation; they are not added to the lexicon automatically. Use the checkpoint's **Add to lexicon** action after checking a proposed meaning. The lexicon supplies context to later model calls; it does not train a model or retroactively reclassify existing posts.

Adding a term does not by itself resolve an escalation. Accepting or reclassifying a post records a new classification version and resolves its pending analyst escalation. Run `samovar validate` afterward if that version has medium or high severity: a review of an older version does not validate a later decision.

Confidence and severity are separate fields. The default values are `low`, `medium`, and `high`; they are qualitative judgments, not measured probabilities or a certification against an external analytic standard.

## Reports and state

```text
my-research/
├── samovar.yaml          # Project scope, taxonomy, and source configuration
├── lexicon/              # Terminology and analyst corrections
├── sources/              # Collector scripts
├── data/                 # Files retained by your collectors
├── reports/              # Generated Markdown reports
└── .samovar/state.db      # Posts, decisions, review history, and runs
```

Reports use the latest eligible reviewed classification for each post. Dismissed findings and stale reviews are excluded. Unresolved escalations remain visible as items requiring analyst attention, not confirmed findings.

Reports are written to `reports/YYYY-MM-DD_threat_report.md`. Running the report command again on the same day replaces that file, so preserve a copy if you need multiple versions.

The database keeps classification versions and review history. Existing databases are migrated when opened; older reviews whose classification version cannot be established are retained as history and require fresh validation. Back up existing projects before upgrading. Migration cannot recover posts that an older version lost because two sources shared an ID; reimport the source data if it is still available. The database is a research record, not a tamper-evident evidence archive.

## Default taxonomy

The template includes these categories:

- `jailbreak_or_bypass`
- `prompt_injection`
- `proxy_or_reselling`
- `stolen_credentials`
- `harmful_content`
- `capability_elicitation`
- `none_or_unclear`

Edit `taxonomy.categories` in `samovar.yaml` to change the labels and descriptions. Source coverage, keywords, and collection filters depend on your collectors; configuration fields alone do not implement those filters.

## Data handling and limitations

- Project files and the database are stored locally. Text, lexicon entries, and other context sent to an agent are processed through the selected CLI and its model service. Local storage does not make analysis offline or keep it entirely on your machine.
- Analysis agents receive context through standard input in a temporary working directory. Samovar requests disabled tools and integrations, suppresses inherited customizations, and uses Codex's read-only sandbox. These are layered CLI restrictions, not a guarantee of complete isolation: CLI versions and managed policies can differ. They also do not establish output accuracy or eliminate prompt-injection risk in source material.
- Collector scripts execute code with your user account's access. Inspect them, use a suitable environment, and keep credentials out of datasets, configuration, and model context.
- The project `.gitignore` excludes `data/` and `.samovar/`. Reports, lexicon files, and source code can still contain sensitive material; inspect them before sharing or committing.
- Russian slang, irony, translation, and incomplete context can cause classification errors. A second model review can also be wrong. Verify consequential claims against the original material.
- No platform-specific collectors ship with this package. Collection access, rate limits, authentication, and compliance with source terms are the collector author's responsibility.
- Lexicon matching is case-insensitive substring matching, not linguistic analysis. Keyword discovery, incremental source cursors, and report-to-report trend comparison are not automated end to end.
- A failed run can retain successful earlier steps. Inspect `samovar status` and the error before retrying; the workflow is not one transaction covering collection through reporting.

## Development

```bash
python -m pip install -e .
python -m pip install setuptools wheel
python -m unittest discover -s tests -v
```

Tests use temporary projects and mocked agent responses. The packaging test builds and installs a wheel without downloading build dependencies, which is why `setuptools` and `wheel` must already be installed. These tests do not establish live model quality, source accessibility, complete isolation, or compatibility with every agent CLI version. A live provider round trip has not been verified as part of this test suite.

The main components are `samovar.py` for commands and orchestration, `lib/agent.py` for agent processes, `lib/state.py` for persistence, `lib/validation.py` for output checks, and `samovar_resources/` for packaged prompts and templates.

## License

MIT, as declared in [the package metadata](pyproject.toml).
