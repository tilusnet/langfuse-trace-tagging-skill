# langfuse-trace-tagging

An agent-skill for tagging [Langfuse](https://langfuse.com) traces/sessions via the ingestion API — handles credential discovery/setup, and (critically) works around a real Langfuse API limitation most people find out about the hard way.

The skill is written agent-agnostically: each step states the underlying requirement, with a concrete illustrative implementation for **[Claude Code](https://claude.com/claude-code)** today. PRs adding callouts for other agents (Cursor, Copilot, Codex, Gemini CLI, etc.) are welcome — see [`SKILL.md`](skills/langfuse-trace-tagging/SKILL.md).

## The gotcha this skill exists for

**Langfuse trace tags can be added via the API, but never removed or replaced.** Repeated tag-bearing events for the same trace ID are merged as a *unique union*, not a replace — there's no way to represent "this tag was removed." Confirmed directly by Langfuse's own maintainers: a request to support tag updates/removal was closed [`not planned`](https://github.com/langfuse/langfuse/issues/8937), and several earlier requests for the same thing went unaddressed ([#4301](https://github.com/orgs/langfuse/discussions/4301), [#4322](https://github.com/orgs/langfuse/discussions/4322), [#4500](https://github.com/orgs/langfuse/discussions/4500), [#8479](https://github.com/orgs/langfuse/discussions/8479)).

In practice this means: if you tag broadly now and try to narrow it down later, the narrower pass just unions with the first and nothing shrinks. The only way to actually "undo" a tag is to destructively delete and recreate the trace — losing its spans, timing, and I/O in the process.

This skill designs around that constraint from the start: it always builds a full proposed tag table and gets it confirmed by you *before* touching the API, rather than applying anything provisionally.

## What it does

- Discovers or helps you set up Langfuse API credentials (public key shared in chat is fine — it's not secret; the secret key goes through a scratch-file → macOS Keychain flow, never pasted directly into chat, and never written to Keychain without your explicit confirmation)
- Proposes `topic:issue`-style tags for a session's traces as a markdown table, grouped by trace/turn range where relevant, and gets it confirmed before applying anything
- Fetches trace metadata safely — deliberately avoids pulling raw trace `input`/`output` content to disk, since that content can contain secrets pasted during a session (API keys, passwords, tokens)
- Applies tags via batched ingestion API calls, computing the full union client-side since the API can't replace/remove

## Install

### Claude Code

```bash
npx skills add tilusnet/langfuse-trace-tagging-skill --skill langfuse-trace-tagging --agent claude-code
```

Or manually:
```bash
git clone https://github.com/tilusnet/langfuse-trace-tagging-skill.git
cp -r langfuse-trace-tagging-skill/skills/langfuse-trace-tagging ~/.claude/skills/
```

### Other agents

`skills/langfuse-trace-tagging/SKILL.md` itself has no Claude-Code-only content in its *requirements* — only its illustrative implementation notes are Claude-Code-specific, clearly marked as callouts. Point your agent's skill-loading mechanism at the same file, and swap in your agent's own equivalent for the callout steps (persistent notes/memory, secret storage, confirmation gating) as needed.

## Why this exists

Built after a long real debugging session where a blanket tag pass on ~150 traces couldn't be walked back to a more granular set — only discovered the append-only limitation *after* the fact. This skill packages that lesson so it doesn't have to be relearned.
