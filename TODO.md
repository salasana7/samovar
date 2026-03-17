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
