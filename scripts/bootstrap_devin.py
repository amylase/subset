"""Register the remediation playbook and the weekly scan schedule with Devin.

Run once after configuring `.env`:

    python scripts/bootstrap_devin.py

Two things are set up here.

**A playbook.** The per-session prompt stays short and points at the issue; the standing rules —
diagnose before changing, add tests, never weaken CI, and what to do about ambiguity — live in the
playbook so they are identical across every session and visible in the Devin dashboard rather than
buried in a string literal.

**A schedule.** The assignment lists both event-driven and periodic triggers. GitHub's own
``schedule:`` workflows do not run on forks, so the periodic path uses Devin's Schedules API
instead: a weekly audit that files new issues, which then re-enter through the webhook path. The
loop feeds its own input.
"""

from __future__ import annotations

import asyncio
import sys

from app.clients.devin import DevinClient
from app.config import get_settings
from app.core.prompts import PLAYBOOK_BODY, PLAYBOOK_NAME, SCAN_SCHEDULE_PROMPT

WEEKLY_MONDAY_09_00 = "0 9 * * 1"


async def main() -> int:
    settings = get_settings()
    devin = DevinClient(settings.devin_api_key, settings.org_base)
    try:
        existing = await devin.list_playbooks()
        items = existing.get("playbooks", existing) if isinstance(existing, dict) else existing
        names = {p.get("name") for p in items or [] if isinstance(p, dict)}

        if PLAYBOOK_NAME in names:
            print(f"playbook already registered: {PLAYBOOK_NAME}")
        else:
            created = await devin.create_playbook(PLAYBOOK_NAME, PLAYBOOK_BODY)
            print(f"playbook created: {created.get('playbook_id') or created}")
            print("  -> put its id in DEVIN_PLAYBOOK_ID to attach it to new sessions")

        schedules = await devin.list_schedules()
        sched_items = (
            schedules.get("schedules", schedules) if isinstance(schedules, dict) else schedules
        )
        titles = {s.get("title") for s in sched_items or [] if isinstance(s, dict)}
        title = f"Weekly audit: {settings.github_repo}"

        if title in titles:
            print(f"schedule already registered: {title}")
        else:
            prompt = (
                f"Repository: https://github.com/{settings.github_repo}\n\n{SCAN_SCHEDULE_PROMPT}"
            )
            created = await devin.create_schedule(
                prompt, WEEKLY_MONDAY_09_00, timezone="UTC", title=title
            )
            print(f"schedule created: {created.get('schedule_id') or created}")
    finally:
        await devin.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
