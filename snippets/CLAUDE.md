## Peer review with Codex (duo-mcp)

You can consult Codex, an independent agent from a different model family, with the `duo` MCP tools
(`ask_codex`, `review_with_codex`, `continue_codex`). It investigates this repository without editing it and
makes different mistakes than you do. Use it when:

- an important decision is ambiguous, or several plausible explanations exist;
- your confidence is low, or debugging is stuck;
- an independent implementation idea would help;
- you have made a substantial or risky change (`review_with_codex` before calling it done).

How:
- For hard questions, ask *before* revealing your conclusion: default `mode="independent"`, a neutral
  question, only facts (errors, repro, paths) in `context`. Use `mode="debate"` or `continue_codex`
  afterwards if you want your own view challenged.
- Always pass `cwd` (your working directory).
- Do not consult it for trivial or mechanical tasks.

If you and Codex disagree: (1) state the exact point of disagreement; (2) inspect the code, tests and
other evidence; (3) test the competing hypotheses when practical; (4) decide from the evidence and tell
the user what you concluded and why. Codex is not an authority, and neither are you.
