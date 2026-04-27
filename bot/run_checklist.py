"""Interactive pre-run checklist for network and local settings."""
from __future__ import annotations

CHECKLIST_LINES: tuple[str, ...] = (
    "Stable internet / VPN so traffic can reach the game server",
    "Proxifier (or your proxy split) is running if you route slots via per-account bind_ip",
    ".env with BOT_ACCOUNTS_JSON matches proxy ports and accounts you intend to use",
    "Firewall / antivirus allows outbound TCP to the upstream host and your local proxy ports",
)


def prompt_run_checklist() -> None:
    """Print a checklist and require confirmation before continuing."""
    width = 72
    bar = "-" * width
    print(bar, flush=True)
    print("  Pre-run checklist — network & settings".center(width), flush=True)
    print(bar, flush=True)
    for i, line in enumerate(CHECKLIST_LINES, start=1):
        print(f"  [ ] {i}. {line}", flush=True)
    print(bar, flush=True)
    print(
        "Confirm each item mentally, then proceed only if your setup matches.",
        flush=True,
    )
    try:
        reply = input("All set — continue? [y/N]: ").strip().lower()
    except EOFError:
        reply = ""

    if reply not in ("y", "yes"):
        print("Aborted.", flush=True)
        raise SystemExit(0)
