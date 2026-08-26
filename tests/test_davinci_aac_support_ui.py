"""Tests for davinci_aac_support_ui.py -- the local dashboard server that
replaced zenity. Covers the pure logic directly, plus a real end-to-end
smoke test against the actual server on an ephemeral port (no mocking the
HTTP layer itself, since that's exactly the part most worth catching
regressions in).
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import davinci_aac_support_ui as ui  # noqa: E402


class TestReadJsonFile:
    def test_missing_file_returns_none(self, tmp_path):
        assert ui.read_json_file(str(tmp_path / "nope.json")) is None

    def test_valid_file(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text('{"a": 1}')
        assert ui.read_json_file(str(p)) == {"a": 1}

    def test_corrupt_file_returns_none_not_raises(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{not valid")
        assert ui.read_json_file(str(p)) is None


class TestIsInstalled:
    def test_false_when_service_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ui, "SERVICE_PATH", str(tmp_path / "nope.service"))
        assert ui.is_installed() is False

    def test_true_when_service_file_present(self, tmp_path, monkeypatch):
        p = tmp_path / "davinci-aac-support.service"
        p.write_text("[Unit]")
        monkeypatch.setattr(ui, "SERVICE_PATH", str(p))
        assert ui.is_installed() is True


class TestIsActive:
    def test_true_on_zero_exit(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert ui.is_active() is True

    def test_false_on_nonzero_exit(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=3)):
            assert ui.is_active() is False

    def test_false_if_systemctl_missing_entirely(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert ui.is_active() is False


class TestIsProcessAlive:
    def test_true_for_own_pid(self):
        # Signal 0 to our own PID is always safe -- it's the standard way
        # to check liveness without actually sending a real signal.
        assert ui.is_process_alive(os.getpid()) is True

    def test_false_for_a_pid_that_does_not_exist(self):
        # PID 1 is init and always exists; a very large PID is a safe bet
        # to not exist on any real system without race conditions.
        assert ui.is_process_alive(2**30) is False


class TestKillExistingServer:
    def test_no_pid_file_is_a_silent_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ui, "PID_FILE", str(tmp_path / "nope.txt"))
        ui.kill_existing_server()  # must not raise

    def test_corrupt_pid_file_is_a_silent_noop(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "ui-pid.txt"
        pid_file.write_text("not-a-pid")
        monkeypatch.setattr(ui, "PID_FILE", str(pid_file))
        ui.kill_existing_server()  # must not raise

    def test_dead_pid_is_left_alone_not_signaled(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "ui-pid.txt"
        pid_file.write_text("54321")
        monkeypatch.setattr(ui, "PID_FILE", str(pid_file))
        # Mocking at is_process_alive (not the lower-level os.kill) --
        # is_process_alive's own liveness check also calls os.kill(pid, 0)
        # internally, so mocking os.kill wholesale would fake out that
        # probe too and make every PID look alive within the test.
        with patch.object(ui, "is_process_alive", return_value=False), \
             patch("os.kill") as mock_kill:
            ui.kill_existing_server()
            mock_kill.assert_not_called()

    def test_own_pid_in_file_is_not_self_killed(self, tmp_path, monkeypatch):
        # Guards against a pathological case where a stale file happens to
        # contain our own (recycled) PID -- must never send ourselves SIGTERM.
        pid_file = tmp_path / "ui-pid.txt"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(ui, "PID_FILE", str(pid_file))
        with patch("os.kill") as mock_kill:
            ui.kill_existing_server()
            mock_kill.assert_not_called()

    def test_live_other_pid_gets_sigtermed(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "ui-pid.txt"
        pid_file.write_text("54321")
        monkeypatch.setattr(ui, "PID_FILE", str(pid_file))
        with patch.object(ui, "is_process_alive", return_value=True), \
             patch("os.kill") as mock_kill:
            ui.kill_existing_server()
            mock_kill.assert_called_once_with(54321, 15)


class TestPageTemplate:
    def test_mode_placeholder_present_and_no_leftovers_after_substitution(self):
        assert "%%MODE%%" in ui.PAGE
        rendered = ui.PAGE.replace("%%MODE%%", json.dumps("install"))
        assert "%%MODE%%" not in rendered
        assert 'const MODE = "install";' in rendered

    def test_no_base64_or_embedded_secrets_look_sane(self):
        # Not a security test per se -- just a guard against accidentally
        # pasting something that doesn't belong in a client-served page.
        assert "api_key" not in ui.PAGE.lower()
        assert "password" not in ui.PAGE.lower()


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """Starts a real instance of the server's Handler on 127.0.0.1:0,
    pointed at tmp_path for all its state files, and tears it down after."""
    monkeypatch.setattr(ui, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ui, "STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(ui, "INSTALL_LOG", str(tmp_path / "install-log.jsonl"))
    monkeypatch.setattr(ui, "EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(ui, "SERVICE_PATH", str(tmp_path / "nonexistent.service"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    server.mode = "install"
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", tmp_path
    server.shutdown()


class TestServerIntegration:
    def test_root_serves_html(self, running_server):
        base, _ = running_server
        with urllib.request.urlopen(f"{base}/") as r:
            assert r.status == 200
            body = r.read().decode()
        assert "<title>DaVinci AAC Support</title>" in body
        assert "%%MODE%%" not in body

    def test_status_reports_not_installed(self, running_server):
        base, _ = running_server
        with urllib.request.urlopen(f"{base}/api/status") as r:
            data = json.loads(r.read())
        assert data["installed"] is False
        assert data["active"] is False

    def test_answer_endpoint_writes_expected_file(self, running_server):
        base, tmp_path = running_server
        req = urllib.request.Request(
            f"{base}/api/answer",
            data=json.dumps({"id": "ffmpeg-install", "answer": "yes"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read()) == {"ok": True}
        assert (tmp_path / "answer-ffmpeg-install.txt").read_text() == "yes"

    def test_unknown_action_reports_error_not_crash(self, running_server):
        base, _ = running_server
        req = urllib.request.Request(
            f"{base}/api/action",
            data=json.dumps({"action": "launch-the-missiles"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        assert data["ok"] is False

    def test_events_stream_delivers_appended_lines(self, running_server):
        base, tmp_path = running_server
        events_file = tmp_path / "install-log.jsonl"
        events_file.write_text("")

        received = []

        def consume():
            with urllib.request.urlopen(f"{base}/api/events?stream=install", timeout=3) as r:
                for raw in r:
                    line = raw.decode().strip()
                    if line.startswith("data: "):
                        received.append(json.loads(line[len("data: "):]))
                    if len(received) >= 2:
                        break

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        time.sleep(0.3)
        with open(events_file, "a") as f:
            f.write(json.dumps({"type": "step", "text": "Checking things..."}) + "\n")
            f.write(json.dumps({"type": "ok", "text": "Done"}) + "\n")
        t.join(timeout=3)

        assert len(received) == 2
        assert received[0]["type"] == "step"
        assert received[1]["type"] == "ok"

    def test_requests_update_last_activity(self, running_server):
        base, _ = running_server
        ui._last_activity = 0  # simulate having been idle for a long time
        with urllib.request.urlopen(f"{base}/api/status"):
            pass
        assert time.time() - ui._last_activity < 2, (
            "a real request should reset the idle clock -- otherwise the "
            "idle watchdog would eventually kill a server someone is "
            "actively looking at"
        )

    def test_uninstall_schedules_exit_without_killing_the_test_process(
        self, running_server, monkeypatch
    ):
        base, tmp_path = running_server
        pid_file = tmp_path / "ui-pid.txt"
        port_file = tmp_path / "ui-port.txt"
        pid_file.write_text("12345")
        port_file.write_text("9999")
        monkeypatch.setattr(ui, "PID_FILE", str(pid_file))
        monkeypatch.setattr(ui, "PORT_FILE", str(port_file))
        # Shrink the real delay so the test doesn't need to sleep for it,
        # and stub os._exit so the background thread doesn't tear down
        # the test process itself when it "exits".
        monkeypatch.setattr(ui, "UNINSTALL_EXIT_DELAY_SECONDS", 0.05)
        with patch("subprocess.run") as mock_run, patch("os._exit") as mock_exit:
            req = urllib.request.Request(
                f"{base}/api/action",
                data=json.dumps({"action": "uninstall"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as r:
                data = json.loads(r.read())
            assert data == {"ok": True}
            assert mock_run.called, "should have called systemctl disable --now"

            for _ in range(50):
                if mock_exit.called:
                    break
                time.sleep(0.02)
            assert mock_exit.called, "delayed_exit should fire after the shrunk delay"
        assert not pid_file.exists()
        assert not port_file.exists()
