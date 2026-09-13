"""Where rendered pages get hosted. A backend turns one static HTML document into a URL.

* ``pipa``    — deployed to a pipa server (https://github.com/fguisso/pipa) through its CLI.
* ``command`` — any other deploy tool, driven by command templates (wrangler, rsync, s3 cp, ...).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, Protocol

from .settings import Settings, as_bool, as_int

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


class DeployError(RuntimeError):
    pass


class Backend(Protocol):
    name: str

    def deploy(self, page_id: str, html: str, *, title: str, remote_id: Optional[str] = None) -> tuple[str, str]:
        """Publish ``html``; return ``(url, remote_id)``. ``remote_id`` given = update in place."""

    def remove(self, page_id: str, remote_id: str, *, tombstone_html: str, title: str, purge: bool = False) -> dict:
        """Take the page down (or replace it with ``tombstone_html``); return details for the agent."""


def _write_bundle(root: Path, page_id: str, html: str) -> Path:
    target = root / page_id
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "index.html").write_text(html, encoding="utf-8")
    return target


def _parse_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        start = text.find("{")  # tolerate log noise before the JSON object
        if start == -1:
            return None
        try:
            data = json.loads(text[start:])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


class PipaBackend:
    """Drives the ``pipa`` CLI (``--json``). Pages are created ``--access noauth --csp off``: Telegram
    can't type a password, and pipa's strict CSP (``default-src 'self'``) would block Telegram's
    WebApp script — the page ships its own CSP ``<meta>`` instead. Creating with these flags needs no
    step-up; ``pipa rm`` always does, so closing a page replaces it with a tombstone and a real
    delete (``purge``) hands a confirmation URL to a human."""

    name = "pipa"

    def __init__(
        self,
        build_dir: Path,
        *,
        bin: str = "pipa",
        zone: str = "",
        workspace: str = "",
        headless: bool = False,
        force_zone: bool = False,
        purge: bool = False,
        timeout: int = 180,
    ):
        self.build_dir = Path(build_dir)
        self.bin = bin
        self.zone = zone
        self.workspace = workspace
        self.headless = headless
        self.force_zone = force_zone
        self.purge = purge
        self.timeout = timeout

    def _run(self, *args: str, timeout: Optional[int] = None) -> dict:
        cmd = [self.bin, "--json", *(["--headless"] if self.headless else []), *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise DeployError(
                f"pipa CLI not found ({self.bin!r}); install it with "
                "`curl -fsSL https://guisso.dev/pipa/install.sh | sh` and run `pipa login`"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DeployError(f"pipa {args[0]} timed out after {exc.timeout}s") from exc
        data = _parse_json(proc.stdout)
        if proc.returncode != 0:
            detail = (data or {}).get("message") or (data or {}).get("error") or proc.stderr.strip() or proc.stdout.strip()
            raise DeployError(f"pipa {args[0]} failed (exit {proc.returncode}): {detail}")
        if data is None:
            raise DeployError(f"pipa {args[0]} printed no JSON: {proc.stdout[:200]!r}")
        return data

    def deploy(self, page_id: str, html: str, *, title: str, remote_id: Optional[str] = None) -> tuple[str, str]:
        bundle = _write_bundle(self.build_dir, page_id, html)
        args = ["deploy", str(bundle), "--name", f"interact: {title}"[:80]]
        if remote_id:
            args += ["--uuid", remote_id]  # update in place: access/zone/csp are kept
        else:
            args += ["--new", "--access", "noauth", "--csp", "off"]
            if self.zone:
                args += ["--zone", self.zone]
                if self.force_zone:
                    args.append("--force")
            if self.workspace:
                args += ["--workspace", self.workspace]
        data = self._run(*args)
        url, uuid = str(data.get("url") or ""), str(data.get("uuid") or "")
        if not url or not uuid:
            raise DeployError(f"pipa deploy returned no url/uuid: {data}")
        if data.get("csp", "off") != "off":
            raise DeployError("pipa kept the strict CSP on this page; Telegram's WebApp script would be blocked")
        if data.get("access", "noauth") != "noauth":
            raise DeployError("pipa page is password-gated; Telegram cannot open it")
        return url.rstrip("/") + "/", uuid

    def remove(self, page_id: str, remote_id: str, *, tombstone_html: str, title: str, purge: bool = False) -> dict:
        info: dict = {}
        if remote_id:
            self.deploy(page_id, tombstone_html, title=title, remote_id=remote_id)
            info["tombstoned"] = True
            if purge or self.purge:
                data = self._run("rm", remote_id, "--no-wait")
                verify_url = (data.get("step_up") or {}).get("verify_url") or data.get("verify_url")
                if verify_url:
                    info["purge_verify_url"] = verify_url
                    threading.Thread(
                        target=self._resume_rm, args=(remote_id,), name=f"pipa-rm-{remote_id}", daemon=True
                    ).start()
                else:
                    info["purged"] = True
        shutil.rmtree(self.build_dir / page_id, ignore_errors=True)
        return info

    def _resume_rm(self, remote_id: str) -> None:
        try:
            self._run("rm", remote_id, "--resume", timeout=900)
            logger.info("pipa page %s deleted after step-up", remote_id)
        except DeployError as exc:
            logger.warning("pipa rm %s was not confirmed: %s", remote_id, exc)


class CommandBackend:
    """Runs user-supplied command templates (no shell). Placeholders: ``{dir}``, ``{file}``,
    ``{page_id}``, ``{remote_id}``. The URL comes from ``url_template`` or the last URL printed."""

    name = "command"

    def __init__(self, build_dir: Path, *, deploy: str, remove: str = "", url_template: str = "", timeout: int = 180):
        if not deploy:
            raise ValueError("command backend needs settings.command.deploy")
        self.build_dir = Path(build_dir)
        self.deploy_cmd = deploy
        self.remove_cmd = remove
        self.url_template = url_template
        self.timeout = timeout

    def _exec(self, template: str, **values: str) -> str:
        argv = [part.format(**values) for part in shlex.split(template)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeployError(f"{argv[0]} failed: {exc}") from exc
        if proc.returncode != 0:
            raise DeployError(f"{argv[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}")
        return proc.stdout

    def deploy(self, page_id: str, html: str, *, title: str, remote_id: Optional[str] = None) -> tuple[str, str]:
        bundle = _write_bundle(self.build_dir, page_id, html)
        remote = remote_id or page_id
        out = self._exec(self.deploy_cmd, dir=str(bundle), file=str(bundle / "index.html"), page_id=page_id, remote_id=remote)
        if self.url_template:
            url = self.url_template.format(page_id=page_id, remote_id=remote)
        else:
            urls = _URL_RE.findall(out)
            if not urls:
                raise DeployError("deploy command printed no URL; set settings.command.url_template")
            url = urls[-1]
        return url, remote

    def remove(self, page_id: str, remote_id: str, *, tombstone_html: str, title: str, purge: bool = False) -> dict:
        if self.remove_cmd:
            bundle = self.build_dir / page_id
            self._exec(self.remove_cmd, dir=str(bundle), file=str(bundle / "index.html"), page_id=page_id, remote_id=remote_id)
            shutil.rmtree(bundle, ignore_errors=True)
            return {"removed": True}
        self.deploy(page_id, tombstone_html, title=title, remote_id=remote_id)
        return {"tombstoned": True}


def make_backend(settings: Settings) -> Backend:
    root = settings.data_dir / "build"
    if settings.backend == "command":
        cfg = settings.command
        return CommandBackend(
            root,
            deploy=str(cfg.get("deploy") or ""),
            remove=str(cfg.get("remove") or ""),
            url_template=str(cfg.get("url_template") or ""),
            timeout=as_int(cfg.get("timeout"), 180),
        )
    cfg = settings.pipa
    return PipaBackend(
        root,
        bin=str(cfg.get("bin") or "pipa"),
        zone=str(cfg.get("zone") or ""),
        workspace=str(cfg.get("workspace") or ""),
        headless=as_bool(cfg.get("headless"), False),
        force_zone=as_bool(cfg.get("force_zone"), False),
        purge=as_bool(cfg.get("purge"), False),
        timeout=as_int(cfg.get("timeout"), 180),
    )
