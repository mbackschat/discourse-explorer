# Host compatibility

The skills and runbooks in this repository describe workflow intent with semantic operations such as “ask the user,” “invoke the skill,” and “delegate execution.” Bind those operations to the current host as follows.

## Roles

| Role | Responsibility | Claude Code binding | Codex binding |
|---|---|---|---|
| `ROUTER` | Resolve user decisions, routing, scope, cost gates, and persistence. | Main conversation | Main conversation |
| `EXECUTOR` | Run resolved commands, handle large output, format results, and persist artifacts. | `Agent` tool with `subagent_type: "general-purpose"` and `model: "sonnet"` | `spawn_agent` with `fork_turns: "none"` and explicit `model: "gpt-5.6-terra"` |

## Ask the user

- **Claude Code:** Use `AskUserQuestion`. Keep every user decision in the main conversation because subagents cannot prompt the user.
- **Other hosts:** Use the host's equivalent structured interaction when available. Otherwise, ask a concise direct question in the main conversation.

Never guess when the answer affects cost, destructive actions, the data directory, provider or model selection, or output persistence. Present a recommendation and its rationale with every choice.

## Invoke a skill

- **Claude Code:** Use the `Skill` tool or the skill's `/skill-name` command.
- **Other hosts:** Use the host's native skill invocation mechanism. If none is available, invoke the workflow through a matching natural-language trigger.

When another document shows `/skill-name`, treat it as the Claude Code spelling of that skill and translate it to the current host's syntax when necessary.

## Delegate execution

Delegation has two independent payoffs: the executor tier is cheaper than the router tier, and large command output stays out of the main context. The second holds even when the session already runs on the executor tier, so delegate whenever a workflow says to unless it matches an explicit skip-delegation condition.

- **Claude Code:** Use the `EXECUTOR` binding above as a durable family alias, never a pinned version ID. Before the first delegation of a run, check `printenv CLAUDE_CODE_SUBAGENT_MODEL`. An empty value or `inherit` allows the per-invocation binding to apply. A value from the same model family preserves the intended executor tier. Any other value overrides the binding, so say which override is active and ask whether to proceed. Residual risk, not detectable from inside a session: an organization `availableModels` allowlist that permits no version from the configured executor family makes the subagent fall back to the main conversation's model with no error.
- **Codex:** Use the `EXECUTOR` binding above. Omitting its explicit model inherits the main model; a full-history fork cannot accept a model override. If the spawn is rejected, report the exact error and ask before retrying without the override.
- **Other hosts:** Use an equivalent capable lower-cost subagent when the host supports one. Otherwise, execute in the main conversation and state that the executor-tier optimization was unavailable.

Resolve every user decision before delegating. The subagent's prompt must be self-contained: data directory, exact commands, persistence path, output format, and return contract. It must never infer a routing decision.
