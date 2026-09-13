import json
import os
import stat
import sys
import time

import pytest

from interact.backends import CommandBackend, DeployError, PipaBackend

FAKE_PIPA = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_PIPA_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
mode = os.environ.get("FAKE_PIPA_MODE", "ok")
if mode == "fail":
    sys.stderr.write("error: not logged in\n")
    sys.exit(1)
cmd = next(a for a in args if not a.startswith("--"))
if cmd == "deploy":
    uuid = args[args.index("--uuid") + 1] if "--uuid" in args else "01TESTUUID"
    print(json.dumps({{"uuid": uuid, "url": "https://pages.example.com/p/" + uuid, "size_bytes": 10,
                      "file_count": 1, "mode": "spa", "access": "noauth", "zone": "public",
                      "csp": "strict" if mode == "strict" else "off"}}, indent=2))
elif cmd == "rm":
    if "--no-wait" in args:
        print(json.dumps({{"step_up": {{"verify_url": "https://pages.example.com/confirm/abc"}}}}))
    else:
        print(json.dumps({{"deleted": True}}))
'''


@pytest.fixture
def fake_pipa(tmp_path, monkeypatch):
    script = tmp_path / "pipa"
    script.write_text(FAKE_PIPA.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_PIPA_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return script, calls


def test_pipa_create_uses_open_page_flags(tmp_path, fake_pipa):
    script, calls = fake_pipa
    backend = PipaBackend(tmp_path / "build", bin=str(script), zone="public", workspace="ws1", headless=True)
    url, uuid = backend.deploy("page123", "<html>hi</html>", title="Preços")

    assert (url, uuid) == ("https://pages.example.com/p/01TESTUUID/", "01TESTUUID")
    args = calls()[0]
    assert args[:3] == ["--json", "--headless", "deploy"]
    assert args[3] == str(tmp_path / "build" / "page123")
    for flag in (["--new"], ["--access", "noauth"], ["--csp", "off"], ["--zone", "public"], ["--workspace", "ws1"]):
        assert " ".join(flag) in " ".join(args)
    assert (tmp_path / "build" / "page123" / "index.html").read_text() == "<html>hi</html>"


def test_pipa_update_targets_the_same_uuid(tmp_path, fake_pipa):
    script, calls = fake_pipa
    backend = PipaBackend(tmp_path / "build", bin=str(script))
    backend.deploy("page123", "v2", title="T", remote_id="01KEEP")
    args = calls()[0]
    assert ["--uuid", "01KEEP"] == args[args.index("--uuid"):args.index("--uuid") + 2]
    assert "--new" not in args and "--access" not in args


def test_pipa_remove_tombstones_and_hands_off_purge(tmp_path, fake_pipa):
    script, calls = fake_pipa
    backend = PipaBackend(tmp_path / "build", bin=str(script))
    info = backend.remove("page123", "01KEEP", tombstone_html="<p>closed</p>", title="T", purge=True)
    assert info == {"tombstoned": True, "purge_verify_url": "https://pages.example.com/confirm/abc"}

    deadline = time.time() + 5  # `rm --resume` runs on a background thread
    while time.time() < deadline and not any("--resume" in c for c in calls()):
        time.sleep(0.05)
    commands = [c[1:] for c in calls()]
    assert commands[0][0] == "deploy" and "--uuid" in commands[0]
    assert ["rm", "01KEEP", "--no-wait"] in commands and ["rm", "01KEEP", "--resume"] in commands
    assert not (tmp_path / "build" / "page123").exists()


def test_pipa_errors_surface(tmp_path, fake_pipa, monkeypatch):
    script, _ = fake_pipa
    backend = PipaBackend(tmp_path / "build", bin=str(script))
    monkeypatch.setenv("FAKE_PIPA_MODE", "fail")
    with pytest.raises(DeployError, match="not logged in"):
        backend.deploy("p", "x", title="T")
    monkeypatch.setenv("FAKE_PIPA_MODE", "strict")
    with pytest.raises(DeployError, match="strict CSP"):
        backend.deploy("p", "x", title="T")
    with pytest.raises(DeployError, match="not found"):
        PipaBackend(tmp_path / "b", bin=str(tmp_path / "nope")).deploy("p", "x", title="T")


def test_command_backend_parses_url_and_formats_placeholders(tmp_path):
    printer = f"{sys.executable} -c \"import sys; print('uploaded', sys.argv[1]); print('https://cdn.example/{{page_id}}/')\" {{file}}"
    backend = CommandBackend(tmp_path / "build", deploy=printer)
    url, remote = backend.deploy("abc", "<p>x</p>", title="T")
    assert (url, remote) == ("https://cdn.example/abc/", "abc")

    templated = CommandBackend(tmp_path / "build", deploy=f"{sys.executable} -c pass",
                               url_template="https://{page_id}.pages.example/")
    assert templated.deploy("xyz", "x", title="T")[0] == "https://xyz.pages.example/"
    assert templated.remove("xyz", "xyz", tombstone_html="gone", title="T") == {"tombstoned": True}
    assert (tmp_path / "build" / "xyz" / "index.html").read_text() == "gone"

    failing = CommandBackend(tmp_path / "build", deploy=f"{sys.executable} -c \"import sys; sys.exit(3)\"")
    with pytest.raises(DeployError, match="exited 3"):
        failing.deploy("q", "x", title="T")
