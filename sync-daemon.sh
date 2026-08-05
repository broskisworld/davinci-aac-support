#!/usr/bin/env bash
#
# resolve_aac_watch.py is the source of truth for the daemon. install.sh
# embeds a copy of it (so the installer stays a single portable file with no
# sibling-file dependency at install time) -- this script keeps that copy in
# sync mechanically, so nobody has to hand-edit two copies of the same code.
#
# Run this after any change to resolve_aac_watch.py, before committing.
# tests/test_daemon_in_sync.py fails CI if this ever gets forgotten.
#
# Usage: ./sync-daemon.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 - "$SCRIPT_DIR/resolve_aac_watch.py" "$SCRIPT_DIR/install.sh" <<'PYEOF'
import re
import sys

daemon_path, install_path = sys.argv[1], sys.argv[2]

with open(daemon_path) as f:
    daemon_source = f.read().rstrip("\n")

with open(install_path) as f:
    install_source = f.read()

pattern = re.compile(
    r'(cat > "\$DAEMON_PATH" <<\'PYDAEMON\'\n).*?(\nPYDAEMON\n)',
    re.DOTALL,
)
new_install_source, count = pattern.subn(
    lambda m: m.group(1) + daemon_source + m.group(2), install_source
)
if count != 1:
    sys.exit(f"Expected exactly one PYDAEMON heredoc block in {install_path}, found {count}")

with open(install_path, "w") as f:
    f.write(new_install_source)

print(f"Synced {daemon_path} -> {install_path}")
PYEOF
