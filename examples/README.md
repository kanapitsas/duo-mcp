# Real end-to-end runs

Two tiny demo repos:
- `stale-cart/`: a failing test with two stacked causes (a mutable default argument, and a memoized price that goes stale).
- `invoice-rounding/`: failing "round half up" tests. One cause is binary float representation, the other is ties-to-even rounding.

Each was copied to a temporary directory and `git init`-ed, so these are arbitrary repos, not duo-mcp itself.
Transcripts are condensed; model output is quoted verbatim where shown. Recorded 2026-09-22 with
Claude Code 2.1.273 and codex-cli 0.155.0-alpha.9.2.

## 1. Codex CLI → `review_with_claude`, with disagreement resolved from evidence

Setup: an uncommitted "fix" to `stale-cart` that fixes both bugs but also swaps `round(x, 2)` for
`int(x * 100) / 100` ("faster on hot paths"). Both tests pass. The primary was `codex exec`, given the
`AGENTS.md` snippet and the prompt "get them reviewed by Claude with review_with_claude, then verify each of
Claude's findings yourself".

Codex called `review_with_claude(cwd=…, instructions="…regressions, edge cases, whether the fix is complete")`.
Claude ran `git status` / `git diff` itself and returned (26s, `model=claude-opus-5`):

> **[High] `cart.py:12`: switching from rounding to truncation is a regression.** […] `1.15 * 100 == 114.99999999999999`, so an item priced 1.15 totals **1.14** […]
> **[Medium] Tests depend on each other through global state (`test_cart.py:12`).** […] If `test_basic_total` runs after it (test randomisation, xdist, or `-k` selection), it fails […]
> **[Low] `cart.py:6`: `lines or []` quietly replaces a caller's empty list.** […]
> **Verdict:** ship after reverting the `int()` truncation […]

Codex then checked each claim at runtime (`1.15 → 1.14`, `0.29 → 0.28`, reordering the tests, and aliasing
with `Cart([])`) and confirmed them all: *"Verdict: do not ship unchanged. Claude's high-severity rounding
finding is confirmed."*

**Disagreement and follow-up.** The primary pushed back on the severity of the test-isolation finding with
`continue_claude(session_id=…)`: "pytest runs tests in file order by default; is it overstated?" Claude:

> You're right that it's latent: in pytest's default file order `set_price` runs last, and `-k` selection wouldn't trigger it either, so I'd downgrade it from medium to low. It's still worth a small fixture […] because pytest-randomly, xdist, or any test added after `test_new_cart_sees_updated_price` would see apple at 2.5 and fail […]

The peer conceded the point the evidence didn't support (including its own `-k` claim). It kept the part
that held, with a concrete reason.

## 2. Claude → Codex debate: a wrong pushback is refuted

After an independent `ask_codex` on `stale-cart` had found both causes, the Claude side deliberately pushed
back with a plausible but wrong argument: `continue_codex`, "memoize is only called with the item name, and
set_price mutates the same dict object, so the cached value should track it. Challenge me." Codex:

> Your argument confuses caching a dictionary reference with caching a value retrieved from that dictionary. In `pricing.py:14`, `return _PRICES[item]` returns a **float** […] `util.py:11` stores that float in its separate cache […] If the cached price tracked the update, they would total **7.5**.

It held its position on evidence (file:line plus the arithmetic of the observed `3.0`) instead of deferring.

## 3. Claude CLI → Codex, independent mode

In `invoice-rounding/`, a `claude -p` primary with the `CLAUDE.md` snippet first wrote down **MY INITIAL VIEW**
(float representation for `2.675`, ties-to-even for `0.125`, fix with `Decimal(str(x))` + `ROUND_HALF_UP`).
It then called `ask_codex` **without** revealing that view. The question was neutral, and the context held only
the file contents and the pytest failures. Codex (49s) independently reached the same two causes. It added one
detail the primary hadn't stated: convert to `Decimal` *before* multiplying, and sum the rounded lines as
decimals. It also raised a spec question: round per line or per invoice? The primary still verified the key
facts itself before concluding:

```
2.675 exact value: 2.67499999999999982236431605997495353221893310546875
round(0.125,2): 0.12
fixed 2.675: 2.68      fixed 0.125: 0.13
```

A similar independent run on `stale-cart` ("which fix: drop memoization or invalidate?") also converged: both
said drop `@memoize`. Codex's version copied a caller-supplied list (`list(lines)`) where Claude's aliased it,
a nuance the primary glossed over as "same fix".

**Honest note.** In these runs the models never disagreed spontaneously on the core diagnosis; the demo bugs
are within both models' reach. The disagreements above arose on severity, in deliberate debate, and on
secondary details, and each was settled from code, runtime checks and arithmetic rather than by deference.
