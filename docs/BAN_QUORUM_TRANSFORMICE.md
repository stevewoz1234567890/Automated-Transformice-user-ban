# Transformice room ban quorum (reports)

## What operators see in-game

When a ban vote is progressing, Transformice chat (room channel **`[Sala]`**, etc.) can show system-style lines about how many mice are participating. In practice, **`/ban`** against a player in the room behaves like a **vote / report tally**: observers report that **11 distinct mice** must participate for the sanction to apply.

This is **game-side behavior**, not enforced by this repository. The number could change in a future client build; the bot reads **`BOT_BAN_QUORUM_REPORTS`** (default **11**) for warnings and session reports.

## What the bot does

- Each **healthy slot** (live MAIN or satellite write path) sends **one** `ban <nickname>` command for the chosen target.
- That matches **one report per connected account** in the room.
- To actually reach the in-game threshold, you usually need **at least as many successful sends as the quorum** (default **11**), from **distinct accounts**.

So:

| Accounts in `BOT_ACCOUNTS_JSON` | Typical outcome |
|-------------------------------|----------------|
| Fewer than 11 | You may never reach the quorum in one round unless other **human** mice in the room also `/ban`. |
| 11 | Minimum *if every slot stays live* (`OK` upstream). |
| 12–14 (common farm size) | Headroom when some slots die (`PARTL`) or MAIN drops during room list / ban. |

## Relation to `/ban` from this bot

- **`ok_count`** in the **`BAN RESULTS`** block = commands the proxy believes it sent successfully.
- If **`ok_count < BOT_BAN_QUORUM_REPORTS`**, the target may remain unbanned until more mice report—including manual `/ban` from other players—or you fix upstream health and rerun.

PARTL-heavy sessions often show **`Ban quorum:`** warnings in `log.txt`; those mean “not enough live slots to satisfy the configured quorum,” not a bug in the packet path.

## Optional tuning

- **`BOT_BAN_QUORUM_REPORTS`** in `.env`: set if community-confirmed threshold differs from **11**.
- Stability: see stagger / PARTL docs (`BOT_UI_FLASH_LAUNCH_STAGGER_SEC`, `BOT_PRE_BAN_ROOM_JOIN_STAGGER_SEC`) so slots survive through room join and ban burst.
