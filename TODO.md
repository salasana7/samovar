# Samovar — Pending Fixes

## Post-first-run fixes

### 1. Classify skill: use crawler metadata
The classify agent receives posts with `metadata` (attack_vectors, artifacts, reply_count) from the collector but ignores it — classifying from raw text alone. The skill should instruct the agent to use existing enrichment as a starting signal.

**File:** `skills/classify.md`

### 2. Classify skill: enforce unknown term flagging
The agent is guessing meanings of unknown slang (e.g., безжоп) from context instead of flagging them as unknown for the lexicon. The skill already says "Do NOT guess meanings" but the agent disobeys. Need stronger instruction or structural enforcement.

**File:** `skills/classify.md`

### 3. Parallel classify batches
Classify currently runs batches sequentially. Could parallelize like investigations — pre-split posts into batches, dispatch to ThreadPoolExecutor, serialize DB writes in main thread.

**File:** `samovar.py` (`_run_classify`)
