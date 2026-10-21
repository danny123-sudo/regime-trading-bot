# Grok Bot strategy council

> **Deprecated 2026-10-04.** The owner is phasing the council out. Nothing live
> depends on it: it never traded, held credentials or touched the VPS. The
> prompts and output in this directory are kept as history and should not be
> treated as an active process.

Four named bots, set up once in the Grok Bot sidebar. Each gets the prompt from
its file below as its standing instructions, plus a schedule.

| Bot | File | Schedule | Writes |
|---|---|---|---|
| SCOUT | `scout.md` | Daily, pre-market | `context/daily_brief.json` |
| ANALYST | `analyst.md` | Weekly | `context/weekly_review.json` |
| RED TEAM | `red_team.md` | Weekly, after ANALYST | verdict inside the review |
| STRATEGIST | `strategist.md` | Weekly, after RED TEAM | `context/strategy_proposal.json` |

## Hard boundaries — repeat these to every bot

- Never place trades. Never log in to KuCoin. Never handle API keys.
- Never edit Python source. Code observations go in `context/code_backlog.md` as prose.
- Only STRATEGIST writes a proposal, and only inside the documented lever bounds.
- A proposal touching any protected value is rejected wholesale and alerts the owner.

## Getting the files to the bot

The council runs on Grok Bot's own cloud computer, which is not the machine the
bot runs on. Two workable patterns:

1. **Git (recommended):** the bot clones a private repo containing an exported
   `trades.csv` and writes proposals back as commits; the trading host pulls
   `context/` on a cron. Nothing sensitive is in that repo — no keys, no source.
2. **Manual paste:** you export the trade log, paste it into the chat, and save
   the returned JSON to `context/` yourself. Fine while validating.

## Signing proposals

`adaptive.py` rejects any proposal whose HMAC signature does not match
`COUNCIL_SECRET`. Generate one with `sign_proposal.py`:

```bash
python council/sign_proposal.py '{"levers": {"composite_threshold": 0.70}, "rationale": "..."}'
```

Give STRATEGIST the secret only if the bot itself is producing signed files. If
you would rather not share it, leave `COUNCIL_SECRET` empty in `.env` — the
signature check is then skipped and every other gate still applies. Signing
matters most once the bot writes proposals unattended.
