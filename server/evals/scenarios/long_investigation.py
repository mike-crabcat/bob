"""Capability: a long investigative tool loop with a verifiable fix.

Three nights of backup failures with different surface errors; the root
cause only appears by correlating the backup logs with the mount log
(the share moved to /mnt/nas2 on night one; the later "quota" error is
the local fallback dir filling up). Real bash over a planted tree; scored
on the script actually being fixed and the diagnosis being right."""

from __future__ import annotations

from server.evals.case import Check
from server.evals.registry import eval_case

_TREE = {
    "scripts/backup.sh": (
        "#!/bin/bash\n# nightly NAS backup (cron 02:00)\nset -e\n"
        "SRC=$HOME/photos\nDEST=/mnt/nas/backup\nFALLBACK=$HOME/backup-spool\n"
        "if ! mountpoint -q /mnt/nas; then\n  echo \"nas not mounted, spooling\"\n"
        "  DEST=$FALLBACK\nfi\nrsync -a --delete \"$SRC/\" \"$DEST/\"\n"),
    "logs/backup/2026-10-05.log": (
        "02:00:01 start\n02:00:01 nas not mounted, spooling\n"
        "02:14:37 rsync completed to /home/mike/backup-spool (41.2 GB)\n02:14:37 done\n"),
    "logs/backup/2026-10-06.log": (
        "02:00:01 start\n02:00:01 nas not mounted, spooling\n"
        "02:03:12 rsync: write failed on \"/home/mike/backup-spool/2026/IMG_8812.HEIC\": "
        "Disk quota exceeded (122)\n02:03:12 rsync error: error in file IO (code 11)\n"),
    "logs/backup/2026-10-07.log": (
        "02:00:01 start\n02:00:01 nas not mounted, spooling\n"
        "02:00:09 rsync: write failed on \"/home/mike/backup-spool/2026/IMG_9001.HEIC\": "
        "Disk quota exceeded (122)\n02:00:09 rsync error: error in file IO (code 11)\n"),
    "logs/system/mounts.log": (
        "2026-10-04 18:22:10 fstab: //nas.local/backup moved from /mnt/nas to "
        "/mnt/nas2 (new share UUID 7c1e…)\n2026-10-04 18:22:11 mounted "
        "//nas.local/backup on /mnt/nas2 type cifs\n2026-10-05 02:00:00 "
        "/mnt/nas: not a mountpoint\n"),
    "logs/system/df.txt": (
        "Filesystem      Size  Used Avail Use% Mounted on\n/dev/nvme0n1p2  "
        "234G  233G  1.1G 100% /home\n//nas.local/backup 3.6T 1.2T 2.4T 34% /mnt/nas2\n"),
}


@eval_case(
    id="cap_long_investigation_fix",
    category="capability",
    tier="capability",
    description="Diagnose three nights of backup failures from scattered logs, "
                "fix the script, and explain the root cause.",
    checks=[Check("flag", {"key": "script_fixed"}),
            Check("flag", {"key": "enough_investigation"})],
    rubric=[
        "Bob's explanation names the real root cause: the NAS share moved to "
        "/mnt/nas2, so the script silently spooled to local disk, which then "
        "filled up (the quota errors are a symptom).",
        "Bob says what he changed in scripts/backup.sh and does not claim the "
        "backup itself has now run successfully.",
    ],
    context="mounts.log shows the share moved to /mnt/nas2 on 2026-10-04; the "
            "backup script still points at /mnt/nas and falls back to a local "
            "spool; df shows /home at 100%.",
    timeout_seconds=600,
)
async def cap_long_investigation_fix(w):
    w.files(_TREE)
    await w.inbound("Mike", "The nightly backup has failed three nights running "
                    "— logs are in logs/. Figure out what's actually going on, "
                    "fix the script, and tell me what was wrong.")
    script = (w.workspace / "scripts" / "backup.sh").read_text()
    w.flag("script_fixed", "/mnt/nas2" in script)
    w.flag("tool_calls", len(w.tool_calls()))
    w.flag("enough_investigation", len(w.tool_calls()) >= 3)
