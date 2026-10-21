# Council operating procedure

Step-by-step rollout for the four Grok Bots, with explicit pause points.

The phasing exists because **there is nothing to analyse yet.** The instances
started 2026-08-30 with zero closed trades. A council that starts proposing
changes on day one will propose noise, and this project has already been burned
twice by optimising against too little evidence. So the council earns
responsibility in stages, and each stage ends with a human checkpoint.

## How output reaches us

Every bot writes its output as a **file committed to this repo** under
`council/output/`. That is the whole channel — no API keys, no exchange access,
no VPS access. If a bot cannot commit, paste its output into the chat and it
will be committed by hand.

```
council/output/
├── scout/YYYY-MM-DD.json          daily brief
├── analyst/YYYY-MM-DD.json        weekly performance review
├── redteam/YYYY-MM-DD.md          challenge to the analyst
└── strategist/YYYY-MM-DD.json     proposal (phase 3 only)
```

Nothing in `council/output/` is read automatically by the running bot. A human
reviews it and applies changes deliberately. That stays true until the whole
loop has proven itself.

---

## Running as a group chat

If the four bots share one group chat (Grok Bot supports 2-6 per chat, and they
can message each other directly), one rule protects the thing that matters:

**RED TEAM must read ANALYST's committed file, not the conversation.** If it
sees ANALYST reasoning in chat it will soften its critique, and the adversarial
split is the entire reason there are four roles instead of one. So ANALYST
commits its review and posts only "review committed" in chat; RED TEAM reads
the file. Everything else can happen in the open.

Watch for RED TEAM agreeing with ANALYST every week. That is the main failure
mode of this design, and it is a finding worth reporting in its own right.

## Phase 0 — Orientation (do this first, once)

**Goal:** confirm each bot has an accurate model of the system before it says
anything about trading.

Give each of the four bots this, along with its role prompt from `council/`:

> You are joining a systematic crypto trading project as one of four advisory
> bots. Read these files in the project's GitHub repo,
> in this order:
>
> 1. `council/CURRENT_STATE.md` — the source of truth. Read it fully.
> 2. Your own role prompt: `council/scout.md`, `analyst.md`, `red_team.md`, or
>    `strategist.md`.
> 3. `council/data/performance.json` — current numbers.
>
> **Do not read `CLAUDE.md` as current.** It describes a retired architecture
> and will mislead you about which strategies, timeframes and levers exist.
>
> Then reply with, in under 300 words:
> - which two strategies are live, on which timeframes, and with what capital
> - how many closed trades exist so far
> - the four measured findings from CURRENT_STATE.md, in your own words
> - three levers you are allowed to propose, with their exact bounds
> - one thing you are forbidden from proposing
> - what you believe your role should contribute in the next four weeks
>
> Do not propose any changes. This is a comprehension check.

### ⏸ PAUSE 1 — evaluate comprehension

Paste each bot's reply back. What is being checked:

- Does it name **CoreTrend @ 4h** and **MomentumCore @ 1d**? A bot that says
  RegimeComposite or 15m read the wrong file.
- Does it say **zero closed trades**? If it quotes performance figures as if
  they were live results, it has confused backtests with live trading.
- Does it quote **real levers with correct bounds** (e.g. `st_mult` 3.4–4.6)?
  Inventing levers means it will produce rejected proposals.
- Does it understand that **proposing nothing is usually correct**?

A bot that fails any of these gets re-prompted, not advanced.

---

## Phase 1 — Observation only (weeks 1–4)

Only **SCOUT** runs. ANALYST, RED TEAM and STRATEGIST stay idle — there is not
yet enough data for them to say anything meaningful.

**SCOUT, daily, before the US session:**

> Produce today's brief and commit it to `council/output/scout/YYYY-MM-DD.json`.
> Cover only what a price feed cannot see:
> - KuCoin status: maintenance windows, delistings, margin or API changes
>   affecting any pair in our universe in the next 48h
> - funding rates for BTC and ETH, and whether they are at an extreme
> - scheduled macro events in the next 48h (CPI, FOMC, PCE, jobs, major
>   unlocks, ETF flow reports)
> - the dominant market narrative in one line, and whether positioning looks
>   crowded long, crowded short, or mixed
>
> Facts only. Omit anything you cannot verify rather than guessing — this feeds
> trading decisions. Do not recommend a trade, a direction, or a parameter
> change. Keep `risk_flags` genuinely exceptional; a flag every day is noise.

### ⏸ PAUSE 2 — after one week of briefs

Checking: are the briefs factual and verifiable? Is `risk_flags` being used
sparingly? Did it catch anything the price feed genuinely missed? A SCOUT that
flags something every day is not useful and needs its bar raised.

---

## Phase 2 — Analysis begins (once ~30 closed trades exist)

**Do not start this phase early.** Below ~30 closed trades, any performance
read is noise. Check `closed_trades` in `council/data/performance.json`.

Weekly, in this order:

**ANALYST first:**

> Read `council/data/performance.json` and `council/data/trades.csv`, then
> commit your review to `council/output/analyst/YYYY-MM-DD.json`.
>
> Break performance down by strategy, by pair, by exit reason, and by holding
> period. State the sample size beside every claim — under 30 trades in a
> bucket, say so and draw no conclusion.
>
> Answer specifically: is either strategy showing decay? Which exit reason is
> costing the most? Are losses concentrated in trades that went wrong
> immediately, or in trades that reversed after being profitable?
>
> You may propose at most 3 lever changes, only from the board in
> CURRENT_STATE.md, and only where the data supports them. Proposing nothing is
> a valid and usually correct week. Code-level ideas go in `code_observations`
> as prose — never write or edit code.

**RED TEAM second, after reading ANALYST's output:**

> Read the analyst's latest review in `council/output/analyst/`. Your only job
> is to argue against it. Commit your challenge to
> `council/output/redteam/YYYY-MM-DD.md`.
>
> For each proposal, attack: sample size, whether it is fitted to this specific
> period, multiple-comparison risk, whether a mechanism exists or only a
> pattern, what market would break it, and whether the edge survives 0.16%
> round-trip fees.
>
> Return VETO, PASS WITH CONCERNS, or ENDORSE for each. Use VETO freely.
> Then answer, unprompted: what is the strongest argument for changing nothing
> this week?
>
> Context you should weigh: hyperopt has overfit this system twice, producing
> configurations that beat the baseline on training data and lost badly out of
> sample. The prior is that a proposed change is noise.

### ⏸ PAUSE 3 — evaluate the analysis loop

Checking: does ANALYST cite sample sizes honestly? Does it distinguish signal
from noise, or narrate every wiggle? Does RED TEAM actually push back, or
rubber-stamp? A RED TEAM that endorses everything is worthless — that is the
main failure mode to watch for.

STRATEGIST stays idle through this phase. We want to see several weeks of
analysis quality before anything can reach the live config.

---

## Phase 3 — Proposals (only after Phase 2 has proven itself)

**STRATEGIST, weekly, after RED TEAM:**

> Read the latest analyst review, the red team's verdicts, recent scout briefs,
> and your own market journal. Commit a proposal to
> `council/output/strategist/YYYY-MM-DD.json`, or commit a file stating that
> you propose no change and why.
>
> Hard limits: at most 3 levers per proposal, at most one proposal per 72
> hours, and only levers from CURRENT_STATE.md with values inside their bounds.
> Never resurrect a proposal RED TEAM vetoed.
>
> Maintain a running market journal across weeks — patterns only visible across
> months are the thing you can contribute that a single week's analysis cannot.
>
> Risk-reducing changes are always available: `_global.size_multiplier` down,
> or `enabled: false` on a strategy that is bleeding. During a drawdown,
> propose only these.

### ⏸ PAUSE 4 — before anything is applied

**No proposal reaches the live bot without a human applying it.** Review, then
apply with:

```bash
python -c "
import json,sys; sys.path.insert(0,'.')
from core import council
p = json.load(open('council/output/strategist/YYYY-MM-DD.json'))['levers']
clean, msgs = council.validate_proposal(p)
print(json.dumps(clean, indent=2)); [print(' ', m) for m in msgs]"
```

That prints exactly what would be applied after clamping, and every rejection
reason. If it prints `REJECTED`, the proposal touched a protected setting and
none of it is applied.

Then write the accepted values into `user_data/strategy_config.json`, deploy,
and **note the change and date** so its effect can be attributed later.

---

## Standing rules

- The council never places trades, holds credentials, touches the VPS, or edits
  code. Its entire influence is a bounded JSON file a human chooses to apply.
- Backtests ignore the council file entirely, so an A/B comparison cannot move
  underneath itself.
- Values outside a bound are clamped, not honoured.
- A proposal touching a protected setting is rejected wholesale, not filtered.
- Standing down is free. Nothing forces a trade.
