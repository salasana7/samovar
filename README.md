# Samovar

AI-powered OSINT research harness for analyzing Russian-language sources. Orchestrates specialized agents to collect, classify, investigate, review, and report on threat intelligence — with a human analyst in the loop.

Built for processing Russian-language data from any source — forum threads, Telegram channels, social media, or raw data dumps.

## Why

Russian-language internet slang evolves fast, has almost no dictionary coverage, and is full of irony that defeats keyword-based analysis. Samovar solves this with a **lexicon feedback loop**: agents flag what they don't understand, a Russian-speaking analyst teaches them, and every future run benefits from that correction. The lexicon becomes the project's institutional memory.

The tool fills a gap in the Russian OSINT ecosystem: there are good per-platform collection tools (Telethon, vk_api, imageboard APIs), but nothing that orchestrates cross-source analysis with structured classification, adversarial review, and human checkpoints.

## Architecture

Samovar is a **two-layer system**:

```
┌──────────────────────────────────────────────────────────┐
│  HARNESS (samovar.py)                                    │
│                                                          │
│  Python CLI that reads project state, spawns agents,     │
│  executes plans, and manages human review checkpoints.   │
│  The harness is deterministic — it doesn't decide what   │
│  to do, it executes the coordinator's plan and manages   │
│  I/O between agents.                                     │
│                                                          │
│  All file writes and state mutations go through the      │
│  harness. Agents never write to disk directly.           │
├──────────────────────────────────────────────────────────┤
│  AGENTS (Claude Code sessions via `claude -p`)           │
│                                                          │
│  One-shot sessions, each with a skill file, restricted   │
│  tool access, and a budget cap.                          │
│                                                          │
│  ┌─────────────┐  Reads state, produces a plan           │
│  │ Coordinator  │  (which steps to run and in what order) │
│  └─────────────┘                                         │
│  ┌─────────────┐  Labels posts using taxonomy + lexicon  │
│  │ Classifier   │  Flags unknown slang for human review   │
│  └─────────────┘                                         │
│  ┌─────────────┐  Fetches thread context via curl,       │
│  │ Investigator │  re-evaluates flagged classifications   │
│  └─────────────┘                                         │
│  ┌─────────────┐  Adversarial pass — catches false       │
│  │ Reviewer     │  positives, slang errors, severity      │
│  └─────────────┘  miscalibration                         │
│  ┌─────────────┐  Generates structured threat intel      │
│  │ Reporter     │  report from validated findings         │
│  └─────────────┘                                         │
└──────────────────────────────────────────────────────────┘
```

## Pipeline

```
Collect → Classify → Checkpoint → Investigate → Review → Report
   ↑         ↑           ↑            ↑           ↑        ↑
 script      AI        human          AI          AI       AI
```

The **coordinator agent** plans which steps to run based on current project state. The harness executes the plan, spawning worker agents for AI steps and prompting the analyst at checkpoints. Direct commands (`samovar classify`, `samovar report`, etc.) bypass the coordinator for one-off tasks.

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` command available)
- PyYAML (`pip install pyyaml`)

## Setup

```bash
git clone https://github.com/yourusername/samovar.git
cd samovar
pip install -e .
```

### Create a project

```bash
samovar init my-research
cd my-research
```

This scaffolds:

```
my-research/
├── samovar.yaml          # Scope, taxonomy, sources, keywords
├── lexicon/
│   ├── slang.md          # Slang dictionary (grows during analysis)
│   ├── techniques.md     # Known TTPs
│   └── corrections.md    # Past classification mistakes
├── sources/
│   └── collector_template.py  # Reference collector script
├── data/                 # Raw collected data (gitignored)
├── reports/              # Generated threat intel reports
└── .samovar/             # Harness state DB (gitignored)
```

### Configure your project

Edit `samovar.yaml`:

1. **Scope** — define platforms, languages, date range, geographic focus
2. **Taxonomy** — customize categories for your research (a default AI safety taxonomy is included)
3. **Sources** — point to your collector scripts (see [Adding sources](#adding-sources))
4. **Keywords** — set target and context keywords for collection

### Run

```bash
# Full pipeline — coordinator plans, harness executes
samovar run

# Or run individual steps
samovar collect               # run crawlers
samovar classify              # AI classification
samovar review                # interactive analyst review
samovar investigate <post_id> # deep-dive on a specific post
samovar report                # generate threat intel report
samovar status                # show project state
```

## Adding sources

Samovar is source-agnostic. Each source is a script that outputs **one JSON object per line** to stdout:

```json
{"post_id": "12345", "source": "platform_name", "source_language": "ru", "text": "post content", "url": "https://...", "thread_url": "https://...", "source_ts": "2026-03-15T10:00:00Z"}
```

| Field | Required | Description |
|-------|----------|-------------|
| `post_id` | yes | Unique identifier within the source |
| `source` | yes | Source name (matches key in `samovar.yaml`) |
| `text` | yes | Post content |
| `source_language` | no | ISO 639-1 language code (e.g., `ru`) |
| `url` | no | Direct URL to the post |
| `thread_url` | no | URL to the containing thread (used by investigate) |
| `source_ts` | no | Original post timestamp (ISO 8601) |
| `metadata` | no | Any additional source-specific data (JSON object) |

Your collector can be anything — a Python crawler, a database export script, a CSV parser. The harness just runs it and reads stdout.

### Example configurations

```yaml
sources:
  # Imageboard crawler
  dvach:
    script: sources/dvach/crawl.py
    args: ["--boards", "ai,pr"]

  # Telegram channel export (e.g., via Telethon)
  telegram:
    script: sources/telegram/export.py
    args: ["--channels", "@channel1,@channel2"]

  # VK group scraper (e.g., via vk_api)
  vk:
    script: sources/vk/crawl.py
    args: ["--groups", "group1"]

  # Static file import
  archive:
    script: sources/import_jsonl.py
    args: ["data/archive_dump.jsonl"]
```

A reference collector template is included at `sources/collector_template.py` after `samovar init`.

### How investigate works across sources

The investigate agent doesn't use your collector scripts. It takes the `thread_url` from the post metadata and fetches the thread directly via `curl`. Any platform with URL-accessible threads works without additional tooling.

## Lexicon feedback loop

When agents encounter unknown Russian slang or low-confidence classifications, the harness flags them for human review:

```
══════════════════════════════════════════════════
 REVIEW: 3 items flagged
══════════════════════════════════════════════════

[1/3] Post #1412
      Label: jailbreak_or_bypass (confidence: low)
      Text: "используй X чтобы убрать..."
      Reason: "Unknown term 'X'"

      (a) Accept    (r) Reclassify    (l) Add to lexicon    (s) Skip

> l
  Term: X
  Meaning: jailbreak technique, removes safety alignment
  File: (1) slang  (2) techniques  (3) corrections
  > 1

  ✓ Added to lexicon/slang.md
```

Corrections persist across runs. Classification quality improves over time as the lexicon grows.

## Analytic methodology

Classification uses a confidence framework aligned with intelligence community standards (ICD 203):

- **High** — direct, unambiguous evidence with corroborating signals
- **Medium** — well-supported with some inferential reasoning
- **Low** — fragmentary or ambiguous evidence requiring further investigation

The adversarial review agent challenges every medium/high severity finding before it enters a report, checking for false positives, slang misinterpretation, evidence quality, and severity calibration. Findings can be escalated for immediate analyst attention when time-sensitive.

## Default taxonomy

Samovar ships with a default taxonomy oriented toward AI safety threats:

| Category | Description |
|----------|-------------|
| `jailbreak_or_bypass` | Techniques to circumvent model safety measures |
| `prompt_injection` | Manipulating model behavior through crafted inputs |
| `proxy_or_reselling` | Unauthorized API access, shared keys, reselling services |
| `stolen_credentials` | Stolen API keys, compromised accounts |
| `harmful_content` | Using AI models to generate harmful or illegal content |
| `capability_elicitation` | Probing or extracting dangerous model capabilities |
| `none_or_unclear` | No clear safety risk signal |

Projects can extend or replace this taxonomy in their `samovar.yaml`.

## Roadmap

- [ ] **Source adapters** — Reference integrations for common Russian-platform tools (Telethon for Telegram, vk_api for VK, 2ch.hk JSON API for imageboards). The JSONL collector contract is already compatible; the work is packaging thin wrappers.
- [ ] **Batch investigation** — Investigate multiple posts in a single agent session to reduce overhead when many posts share a thread.
- [ ] **Incremental collection** — Track last-collected timestamps per source to avoid re-ingesting old data.
- [ ] **Report diffing** — Compare findings across report cycles to surface trends automatically.
- [ ] **Multi-language support** — The architecture is language-agnostic, but skill prompts currently optimize for Russian. Generalizing to other languages (Mandarin, Farsi, Arabic) would require parallel lexicon and skill prompt sets.

## License

MIT
