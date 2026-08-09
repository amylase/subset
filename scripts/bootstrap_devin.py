"""Register the remediation playbook and the weekly scan schedule with Devin.

Run once after configuring `.env`:

    python scripts/bootstrap_devin.py              # playbook only
    python scripts/bootstrap_devin.py --schedule   # playbook + weekly audit

Two things are set up here.

**A playbook.** The per-session prompt stays short and points at the issue; the standing rules —
diagnose before changing, add tests, never weaken CI, and what to do about ambiguity — live in the
playbook so they are identical across every session and visible in the Devin dashboard rather than
buried in a string literal.

**A schedule** (opt-in via ``--schedule``). The assignment lists both event-driven and periodic
triggers. GitHub's own ``schedule:`` workflows do not run on forks, so the periodic path uses
Devin's Schedules API instead: a weekly audit that files new issues, which then re-enter through
the webhook path. The loop feeds its own input. It is behind a flag because a recurring schedule
spends ACUs every week whether or not anyone is watching.
"""

from __future__ import annotations

import asyncio
import sys

from app.clients.devin import DevinClient, collection_items
from app.config import get_settings
from app.core.prompts import PLAYBOOK_BODY, PLAYBOOK_TITLE, SCAN_SCHEDULE_PROMPT

WEEKLY_MONDAY_09_00 = "0 9 * * 1"


async def main(create_schedule: bool) -> int:
    settings = get_settings()
    devin = DevinClient(settings.devin_api_key, settings.org_base)
    try:
        titles = {p.get("title") for p in collection_items(await devin.list_playbooks())}
        if PLAYBOOK_TITLE in titles:
            print(f"playbook already registered: {PLAYBOOK_TITLE}")
        else:
            created = await devin.create_playbook(PLAYBOOK_TITLE, PLAYBOOK_BODY)
            playbook_id = created.get("playbook_id") or created.get("id")
            print(f"playbook created: {playbook_id}")
            print(f"  -> set DEVIN_PLAYBOOK_ID={playbook_id} to attach it to new sessions")

        if not create_schedule:
            print("skipping schedule (pass --schedule to create the weekly audit)")
            return 0

        schedule_title = f"Weekly audit: {settings.github_repo}"
        existing = {s.get("title") for s in collection_items(await devin.list_schedules())}
        if schedule_title in existing:
            print(f"schedule already registered: {schedule_title}")
        else:
            prompt = (
                f"Repository: https://github.com/{settings.github_repo}\n\n{SCAN_SCHEDULE_PROMPT}"
            )
            created = await devin.create_schedule(
                prompt, WEEKLY_MONDAY_09_00, timezone="UTC", title=schedule_title
            )
            print(f"schedule created: {created.get('schedule_id') or created.get('id')}")
    finally:
        await devin.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--schedule" in sys.argv)))
