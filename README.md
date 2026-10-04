# langfuse-trace-tagging

Agent-skills for tagging [Langfuse](https://langfuse.com) traces/sessions **after the fact** — handles credential discovery/setup, and works around real Langfuse limitations most people find out about the hard way.

There are **two skills, one per Langfuse major version**, because the two versions are fundamentally incompatible when it comes to tags:

| Your Langfuse | Skill | How tags are stored |
| --- | --- | --- |
| **v3** | [`langfuse-v3-trace-tagging`](skills/langfuse-v3-trace-tagging/SKILL.md) | Real tags, written through the v3 ingestion API (append-only: tags can be added, never removed) |
| **v4** | [`langfuse-v4-trace-tagging`](skills/langfuse-v4-trace-tagging/SKILL.md) | Tags are immutable after creation in v4, so tag-like **labels** are stored as categorical scores (named `tag`) through a bundled helper script — which can also remove and query them |

Not sure which you run? `GET <your-host>/api/public/health` returns `{"version": "3.x"}` or `{"version": "4.x"}`. Each skill checks this itself and stops if it's the wrong one.

> **Upgrading from the old name:** the original `langfuse-trace-tagging` skill is now `langfuse-v3-trace-tagging` (unchanged behaviour, plus a v3-only guard). Remove an old `~/.claude/skills/langfuse-trace-tagging` before installing. The macOS Keychain service name `langfuse-trace-tagging` that stores your credentials is **unchanged**, so existing stored credentials keep working in both skills.

The skills are written agent-agnostically: each step states the underlying requirement, with a concrete illustrative implementation for **[Claude Code](https://claude.com/claude-code)** today. PRs adding callouts for other agents (Cursor, Copilot, Codex, Gemini CLI, etc.) are welcome.

## Langfuse v3: `langfuse-v3-trace-tagging`

### The gotcha this skill exists for

**Langfuse trace tags can be added via the API, but never removed or replaced.** Repeated tag-bearing events for the same trace ID are merged as a *unique union*, not a replace — there's no way to represent "this tag was removed." Confirmed directly by Langfuse's own maintainers: a request to support tag updates/removal was closed [`not planned`](https://github.com/langfuse/langfuse/issues/8937), and several earlier requests for the same thing went unaddressed ([#4301](https://github.com/orgs/langfuse/discussions/4301), [#4322](https://github.com/orgs/langfuse/discussions/4322), [#4500](https://github.com/orgs/langfuse/discussions/4500), [#8479](https://github.com/orgs/langfuse/discussions/8479)).

In practice this means: if you tag broadly now and try to narrow it down later, the narrower pass just unions with the first and nothing shrinks. The only way to actually "undo" a tag is to destructively delete and recreate the trace — losing its spans, timing, and I/O in the process.

This skill designs around that constraint from the start: it always builds a full proposed tag table and gets it confirmed by you *before* touching the API, rather than applying anything provisionally.

### What it does

- Discovers or helps you set up Langfuse API credentials (public key shared in chat is fine — it's not secret; the secret key goes through a scratch-file → macOS Keychain flow, never pasted directly into chat, and never written to Keychain without your explicit confirmation)
- Proposes `topic:issue`-style tags for a session's traces as a markdown table, grouped by trace/turn range where relevant, and gets it confirmed before applying anything
- Fetches trace metadata safely — deliberately avoids pulling raw trace `input`/`output` content to disk, since that content can contain secrets pasted during a session (API keys, passwords, tokens)
- Applies tags via batched ingestion API calls, computing the full union client-side since the API can't replace/remove

## Langfuse v4: `langfuse-v4-trace-tagging`

### Why a separate skill

In Langfuse v4 each observation is written once to an append-only table and carries a copy of its trace-level attributes, tags included. Per Langfuse's docs, "tags can't be added or edited in the UI after they're created", and re-ingesting an existing trace ID creates **duplicates** — so the v3 approach (re-send the trace with more tags) no longer works, and the v3 endpoints it uses (`GET /api/public/traces`, `trace-create` ingestion events) are removed or rejected. The stated reason is query speed (no joins or deduplication at read time; see the [v4 announcement](https://langfuse.com/changelog/2026-08-17-langfuse-v4)). For classifying traces *after* they exist, Langfuse's own guidance is to use **scores** ("Should I use scores or tags?" in the scores docs).

### What it does

Each tag becomes one **categorical score** named `tag` whose value is the tag string (e.g. `traefik:routing-priority`); several tags on a trace are several scores. A bundled, standard-library-only Python helper (`scripts/lf_labels.py`) does the work: `traces` (list a session's traces with their real tags and labels), `apply` (dry-run first, creates only missing labels, can `--prune`, writes an undo file), `remove`, and `query` (finds traces by tag, returning the **union of label scores and real tags**, so historic tags still count). The skill keeps the same confirm-the-proposal-before-applying flow as the v3 skill, and likewise never fetches trace `input`/`output`.

### Things worth knowing

- **Labels are scores, not tags**: they show up in Langfuse's Scores views and the v4 filter bar (`scores.tag:"<value>"`, per the Langfuse docs), not in the tag filter chips. Real tags already on traces are untouched.
- **Removal works but is slow through the API**: each `DELETE /api/public/scores/{id}` is its own queue job and the worker runs one job per 2 minutes by default (self-hosted), so removing N labels takes about 2N minutes.
- **Dates**: Langfuse ignores a client-supplied score `timestamp`, so a label carries the date it was *created*, not the trace's date. That's also why the helper never relies on overwrite for idempotency — it reads existing labels and creates only what's missing.
- **Langfuse doesn't validate score targets**, so a score for a nonexistent trace ID still succeeds; the helper checks trace IDs against the session first.
- **Not covered**: ingesting a chat export as new traces on v4 (that needs the OTLP endpoint, with tags set at creation).

## How the two skills differ

The two skills are **not** feature-equivalent. Some differences are forced by Langfuse, others are gaps still to be closed.

| Capability | v3 skill | v4 skill |
| --- | --- | --- |
| What's stored | **Real tags** | **Labels as categorical scores** (not tag chips; dated when created) |
| Remove or correct afterwards | **No**: v3 tags are append-only | **Yes**, but slowly through the API (about 2 minutes per label) |
| Find traces by tag | Not provided | **Yes** (`query`: labels plus real tags, any/all) |
| Dry run, undo file, prune | No | **Yes** |
| Ingest a chat export as new traces | **Yes** (step 4, with `metadata.source_url`) | **No** (planned; needs the OTLP endpoint) |
| Credential discovery | Full: memory, then macOS Keychain / Linux Secret Service / Windows DPAPI, then guided first-time setup and storing | Condensed: memory, macOS Keychain, otherwise export the variables yourself |
| Propose tags, with the optional suggested-tag argument | Yes | Yes |
| "Confirm the table before applying" rule | Hard rule (nothing can be undone) | Kept, but softer (labels can be removed) |
| Uses a helper script | No (raw `curl` and `lf`) | Yes (`scripts/lf_labels.py`) |
| Works on | Langfuse v3 | Langfuse v4 |

- **Forced by Langfuse:** real tags can't be added after creation on v4, so the v4 skill has to use scores; and only the v4 skill can remove anything, because v3 tags are append-only. In the UI a v3 tag shows in the tag filters, whereas a v4 label shows in the Scores views and `scores.tag:"…"` filters (the v4 `query` hides this by returning real tags and labels together).
- **Gaps still to close:** export ingestion on v4, and credential-discovery parity (the Linux and Windows paths and the detailed first-time setup were condensed in the v4 skill). Aligning the two skills more closely is on the maintainer's backlog.

## Install

### Claude Code

```bash
# Langfuse v3
npx skills add tilusnet/langfuse-trace-tagging-skill --skill langfuse-v3-trace-tagging --agent claude-code
# Langfuse v4
npx skills add tilusnet/langfuse-trace-tagging-skill --skill langfuse-v4-trace-tagging --agent claude-code
```

Or manually (copy the whole skill directory — the v4 skill includes its `scripts/` helper):
```bash
git clone https://github.com/tilusnet/langfuse-trace-tagging-skill.git
cp -r langfuse-trace-tagging-skill/skills/langfuse-v3-trace-tagging ~/.claude/skills/   # Langfuse v3
cp -r langfuse-trace-tagging-skill/skills/langfuse-v4-trace-tagging ~/.claude/skills/   # Langfuse v4
```

### Other agents

Each `SKILL.md` has no Claude-Code-only content in its *requirements* — only its illustrative implementation notes are Claude-Code-specific, clearly marked as callouts. Point your agent's skill-loading mechanism at the same file (and, for v4, keep `scripts/lf_labels.py` next to it), and swap in your agent's own equivalent for the callout steps (persistent notes/memory, secret storage, confirmation gating) as needed.

## Why this exists

Built after a long real debugging session where a blanket tag pass on ~150 traces couldn't be walked back to a more granular set — only discovered the append-only limitation *after* the fact. The v3 skill packages that lesson so it doesn't have to be relearned. When Langfuse v4 then made tags fully immutable, the v4 skill was added to keep the same after-the-fact workflow going.
