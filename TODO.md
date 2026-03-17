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

### 6. Adaptive classify→checkpoint cycles
Currently the coordinator plans one big classify (all unclassified posts) then one checkpoint at the end. The lexicon is empty for the entire classify run, so every batch is flying blind.

Better workflow: classify a batch → check the flag rate → if too many unknowns, checkpoint so the analyst can build the lexicon → classify next batch with the improved lexicon → repeat. Each cycle benefits from the previous one's corrections.

The coordinator should decide when to checkpoint based on the flag rate, not fixed batch sizes:
- High flag rate (e.g., 40%+ of posts flagged) → lexicon is weak, stop and checkpoint
- Low flag rate (e.g., <10%) → lexicon is solid, keep classifying
- The batch size can grow naturally as the flag rate drops

This is how the feedback loop was always meant to work — the coordinator just needs to be taught to interleave instead of doing everything in one pass.

Fix:
- `skills/coordinator.md`: teach the coordinator to plan iterative classify→checkpoint cycles, checking flag rate between cycles to decide whether to checkpoint or keep going
- `samovar.py`: expose flag rate in `state.summary()` so the coordinator has the data to decide

**Files:** `skills/coordinator.md`, `samovar.py` (`state.summary()`)

### 7. Checkpoint UX: separate review types and show more context
The checkpoint currently dumps ALL flagged items (240!) in one pile — low confidence, unknown terms, medium/high severity all mixed together. Too noisy to review effectively.

Fix:
- Separate into review queues: (1) unknown terms to add to lexicon, (2) low-confidence classifications needing analyst judgment, (3) high-confidence items should NOT appear in checkpoint at all
- Show the post URL so the analyst can verify the source
- Show the full Russian text (currently truncated at 200 chars)
- Decode unknown_terms_json properly — currently showing raw unicode escapes instead of Cyrillic
- Show the evidence_en alongside the original text so the analyst can verify the translation/context

**File:** `lib/checkpoint.py`

### 8. Coordinator: re-plan after classify instead of static upfront plan
The coordinator makes one plan at the start of `samovar run` and the harness executes it. But after classify completes, the state has changed significantly (flagged items, severity distribution) and the coordinator should re-evaluate whether to investigate, checkpoint, or go straight to review. Currently requires running `samovar run` multiple times to advance the pipeline.

Fix: either re-plan mid-run after major steps, or document that multiple `samovar run` calls is the expected workflow.

**Files:** `samovar.py` (`cmd_run`), `skills/coordinator.md`
