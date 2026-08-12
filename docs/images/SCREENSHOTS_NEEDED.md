# Screenshots needed

Two images, referenced from the main README, that need a real desktop and
a real DaVinci Resolve install to capture — no way to fake either
convincingly in a container. Once captured, drop them in this folder with
these exact filenames and delete this checklist file.

(The other two screenshots the README uses, `installer-installing.png` and
`installer-connected.png`, don't belong here — they're of the dashboard
itself, which *can* be faked convincingly in a container, so those are
generated automatically by CI. See `docker/capture-all-screenshots.sh` if
you want to regenerate them locally; don't hand-capture or hand-edit them,
CI will just overwrite whatever's there on the next push.)

## `desktop-file-trust.png`

Right-click on the extracted `davinci-aac-support.desktop` file in your file
manager, showing the "Allow Launching" (or equivalent trust/permissions)
option. Crop tightly to just the context menu — no need to show the rest of
the desktop.

## `resolve-external-scripting.png`

DaVinci Resolve → Preferences → (search "scripting", or System tab →
General) → the "External scripting using" setting, ideally already switched
to "Local" so it's clear what the end state should look like. Crop to the
Preferences panel — no need for the full Resolve window behind it.

---

Use whatever screenshot tool you're comfortable with (e.g. GNOME's
Shift+Ctrl+Print Screen for a selected region). Keep each one tight to just
the relevant UI — nothing else on screen needs to be in frame.
