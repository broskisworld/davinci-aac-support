"""Guards against exactly the bug that bit this project once already: install.sh
embeds a copy of resolve_aac_watch.py so it can install without any sibling
file, but that means it's possible to edit one and forget the other. This
fails CI if they ever drift apart -- run ./sync-daemon.sh to fix.
"""
import os
import re

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_install_sh_embedded_daemon_matches_source_file():
    with open(os.path.join(REPO_ROOT, "resolve_aac_watch.py")) as f:
        daemon_source = f.read().rstrip("\n")

    with open(os.path.join(REPO_ROOT, "install.sh")) as f:
        install_source = f.read()

    match = re.search(
        r'cat > "\$DAEMON_PATH" <<\'PYDAEMON\'\n(.*?)\nPYDAEMON\n',
        install_source,
        re.DOTALL,
    )
    assert match, "Couldn't find the PYDAEMON heredoc block in install.sh"
    embedded = match.group(1)

    assert embedded == daemon_source, (
        "install.sh's embedded daemon copy is out of sync with resolve_aac_watch.py. "
        "Run ./sync-daemon.sh and commit the result."
    )
