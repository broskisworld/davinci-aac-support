"""davinci-aac-support.zip is a generated artifact (see build-release-zip.sh)
-- a plain zip of files that also live in the repo. Nothing is embedded or
encoded this time, but it can still go stale if someone edits install.sh (or
any other packaged file) and forgets to rebuild the zip. Fails CI if the
zip's contents don't byte-for-byte match the current source files.
Run ./build-release-zip.sh and commit the result to fix.
"""
import os
import zipfile

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
ZIP_PATH = os.path.join(REPO_ROOT, "davinci-aac-support.zip")

PACKAGED_FILES = [
    "install.sh",
    "davinci_aac_support_watch.py",
    "davinci-aac-support.desktop",
    "README.md",
    "LICENSE",
]


@pytest.mark.skipif(not os.path.exists(ZIP_PATH), reason="zip not built yet")
def test_zip_contains_exactly_the_expected_files():
    with zipfile.ZipFile(ZIP_PATH) as z:
        names = set(z.namelist())
    assert names == set(PACKAGED_FILES)


@pytest.mark.skipif(not os.path.exists(ZIP_PATH), reason="zip not built yet")
@pytest.mark.parametrize("filename", PACKAGED_FILES)
def test_zip_member_matches_source_file(filename):
    with zipfile.ZipFile(ZIP_PATH) as z:
        zipped = z.read(filename)
    with open(os.path.join(REPO_ROOT, filename), "rb") as f:
        source = f.read()
    assert zipped == source, f"{filename} in the zip is out of date -- rebuild with ./build-release-zip.sh"


def test_desktop_launcher_is_readable_not_embedded():
    with open(os.path.join(REPO_ROOT, "davinci-aac-support.desktop")) as f:
        content = f.read()
    assert "base64" not in content, "the whole point of the zip switch was to stop embedding a blob here"
    assert "install.sh" in content, "the launcher should visibly call install.sh, not hide what it runs"
