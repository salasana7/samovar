# Samovar — Pending Fixes

## Post-first-run fixes

### 1. Classify: pass metadata to agent and use deterministic first pass
The crawler already computes `attack_vectors`, `artifacts`, and `reply_count` for every post. The adapter passes these through as `metadata` in the JSONL and they're stored in state.db. But the classify agent never sees them — it classifies from raw text alone.

Fix:
- `samovar.py` (`_run_classify`): include `metadata` when building the post context for the agent
- `skills/classify.md`: instruct the agent to use pre-computed `attack_vectors` as a starting signal — validate, refine, adjust severity/confidence, catch what regex missed, but don't ignore the deterministic first pass

**Files:** `samovar.py`, `skills/classify.md`

### 2. Classify skill: enforce unknown term flagging
The agent is guessing meanings of unknown slang (e.g., безжоп) from context instead of flagging them as unknown for the lexicon. The skill already says "Do NOT guess meanings" but the agent disobeys. Need stronger instruction or structural enforcement.

**File:** `skills/classify.md`

### 3. Parallel classify batches
Classify currently runs batches sequentially. Could parallelize like investigations — pre-split posts into batches, dispatch to ThreadPoolExecutor, serialize DB writes in main thread.

**File:** `samovar.py` (`_run_classify`)

### 4. Investigate: follow artifact leads and web research
The investigate agent currently only curls the `thread_url` and reads the thread. It doesn't follow leads from post artifacts (rentry pages, GitHub/GitGud repos, HuggingFace Spaces, Telegram links) or do background web research on operator names, tools, or infrastructure.

In the manual pipeline, Claude actively web searched for proxy operator names, found rentry stats pages, cross-referenced 4chan /aicg/ terminology, and mapped infrastructure — producing the kind of deep intelligence analysis that makes the report valuable.

Fix:
- Add `WebSearch` and `WebFetch` to investigate agent's allowed tools
- `skills/investigate.md`: instruct the agent to follow artifact links from post metadata (domain_links, paste_links, github_links, telegram_links) and research operator names, tools, and infrastructure
- The agent should trace the supply chain, not just read one thread

**Files:** `lib/agent.py` (SKILL_TOOLS), `skills/investigate.md`

### 5. Investigate: use post artifacts from crawler metadata
The crawler extracts `artifacts` (github_links, telegram_links, paste_links, domain_links, api_keys) from every post. These are the leads the investigate agent should follow — rentry proxy pages, GitGud repos, HuggingFace Spaces. Currently not passed to the agent.

**Files:** `samovar.py` (`_run_investigate`), `skills/investigate.md`

### 6. Iterative classify→checkpoint cycles with ramping batch size
Currently the coordinator plans one big classify (all unclassified posts) then one checkpoint at the end. The lexicon is empty for the entire classify run, so every batch is flying blind.

Better workflow: classify a small batch → checkpoint → analyst reviews flagged terms → lexicon updates → classify a larger batch with the improved lexicon → repeat. Each cycle benefits from the previous one's corrections.

Batch size ramp: 50 → 100 → 200. First batch is small because everything is unknown and the lexicon is empty. By the third cycle most terms are known so larger batches are fine — faster and higher confidence.

Fix:
- `skills/coordinator.md`: teach the coordinator to plan iterative classify→checkpoint cycles instead of one classify→one checkpoint
- `samovar.py` (`_run_classify`): support configurable batch size, or let the coordinator specify it per step
- The coordinator should check lexicon entry count and flagged item count to decide when to ramp up

**Files:** `skills/coordinator.md`, `samovar.py`
