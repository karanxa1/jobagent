"""Kill agent-launched Chromes that no live daemon owns (orphans of a crash / hard kill). Works on every OS.

Browsers of running daemons (this install's or any other's) are left alone; use `jobagent stop` to stop a daemon
together with its browsers. --dry-run only lists them.
"""
import sys

from jobagent.browsers import kill_orphans

dry = "--dry-run" in sys.argv[1:]
pids = kill_orphans(dry_run=dry)
print(f"{'would kill' if dry else 'killed'} {len(pids)} orphaned agent browser(s){': ' + str(pids) if pids else ''}")
