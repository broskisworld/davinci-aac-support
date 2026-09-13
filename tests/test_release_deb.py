"""davinci-aac-support_<version>_all.deb is a generated artifact (see
build-deb.sh) -- it embeds the same source files as the zip, just packaged
for apt instead. It can still go stale if someone edits install.sh (or any
other packaged file, or the deb/ metadata) and forgets to rebuild. Fails CI
if the .deb's contents don't byte-for-byte match the current source files.
Run ./build-deb.sh and commit the result to fix.
"""
import os
import subprocess
import tempfile

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

with open(os.path.join(REPO_ROOT, "VERSION")) as _f:
    _VERSION = _f.read().strip()
DEB_PATH = os.path.join(REPO_ROOT, f"davinci-aac-support_{_VERSION}-1_all.deb")

# Maps the path inside the .deb to the source file it must match.
PACKAGED_FILES = {
    "./usr/share/davinci-aac-support/install.sh": "install.sh",
    "./usr/share/davinci-aac-support/davinci_aac_support_watch.py": "davinci_aac_support_watch.py",
    "./usr/share/davinci-aac-support/davinci_aac_support_ui.py": "davinci_aac_support_ui.py",
    "./usr/share/davinci-aac-support/davinci_aac_support_config.py": "davinci_aac_support_config.py",
    "./usr/share/applications/davinci-aac-support-setup.desktop": "deb/davinci-aac-support-setup.desktop",
    "./usr/share/doc/davinci-aac-support/README.md": "README.md",
    "./usr/share/doc/davinci-aac-support/copyright": "LICENSE",
    "./DEBIAN/postinst": "deb/postinst",
}


def _extract_deb(tmp_path):
    data_dir = os.path.join(tmp_path, "data")
    control_dir = os.path.join(tmp_path, "control")
    os.makedirs(data_dir)
    os.makedirs(control_dir)
    subprocess.run(["dpkg-deb", "-x", DEB_PATH, data_dir], check=True)
    subprocess.run(["dpkg-deb", "-e", DEB_PATH, control_dir], check=True)
    return data_dir, control_dir


@pytest.mark.skipif(not os.path.exists(DEB_PATH), reason=".deb not built yet")
@pytest.mark.parametrize("packaged_path,source_path", PACKAGED_FILES.items())
def test_deb_member_matches_source_file(packaged_path, source_path):
    with tempfile.TemporaryDirectory() as tmp:
        data_dir, control_dir = _extract_deb(tmp)
        if packaged_path.startswith("./DEBIAN/"):
            rel = packaged_path[len("./DEBIAN/"):]
            extracted_file = os.path.join(control_dir, rel)
        else:
            rel = packaged_path[len("./"):]
            extracted_file = os.path.join(data_dir, rel)
        with open(extracted_file, "rb") as f:
            packaged = f.read()
    with open(os.path.join(REPO_ROOT, source_path), "rb") as f:
        source = f.read()
    assert packaged == source, f"{packaged_path} in the .deb is out of date -- rebuild with ./build-deb.sh"


@pytest.mark.skipif(not os.path.exists(DEB_PATH), reason=".deb not built yet")
def test_deb_control_version_matches_version_file():
    with tempfile.TemporaryDirectory() as tmp:
        _, control_dir = _extract_deb(tmp)
        with open(os.path.join(control_dir, "control")) as f:
            control = f.read()
    assert f"Version: {_VERSION}-1" in control


@pytest.mark.skipif(not os.path.exists(DEB_PATH), reason=".deb not built yet")
def test_postinst_is_executable_inside_the_deb():
    with tempfile.TemporaryDirectory() as tmp:
        _, control_dir = _extract_deb(tmp)
        postinst = os.path.join(control_dir, "postinst")
        assert os.access(postinst, os.X_OK), "DEBIAN/postinst must be executable or dpkg will refuse to run it"
