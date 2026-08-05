"""DaVinci-Resolve-AAC-Fix-Installer.desktop is a generated artifact (see
build-single-file-installer.sh) -- install.sh base64-encoded into its Exec=
line. Same drift risk as the other sync test: fails CI if the committed
.desktop file doesn't match what the build script would currently produce.
Run ./build-single-file-installer.sh and commit the result to fix.
"""
import base64
import os
import re

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
DESKTOP_FILE = os.path.join(REPO_ROOT, "DaVinci-Resolve-AAC-Fix-Installer.desktop")


def test_desktop_bundle_matches_install_sh():
    if not os.path.exists(DESKTOP_FILE):
        return  # not built yet -- nothing to check

    with open(os.path.join(REPO_ROOT, "install.sh")) as f:
        install_source = f.read()

    with open(DESKTOP_FILE) as f:
        desktop_source = f.read()

    match = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d \| bash", desktop_source)
    assert match, "Couldn't find the embedded base64 blob in the .desktop Exec= line"

    embedded_install_source = base64.b64decode(match.group(1)).decode()
    assert embedded_install_source == install_source, (
        "DaVinci-Resolve-AAC-Fix-Installer.desktop is out of sync with install.sh. "
        "Run ./build-single-file-installer.sh and commit the result."
    )
