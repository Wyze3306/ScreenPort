"""Exécution asynchrone de commandes ssh hors terminal (liste des screens...)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

from . import ssh
from .models import Server
from .paths import askpass_path, remove_secret_file, runtime_dir, write_secret_file
from .settings import Preferences

log = logging.getLogger(__name__)


@dataclass
class RemoteResult:
    exit_code: int
    stdout: str
    stderr: str
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.cancelled

    def error(self) -> ssh.SshError:
        return ssh.humanize_ssh_error(self.stderr, self.exit_code)


class RemoteHandle:
    """Permet d'annuler une commande en cours (fermeture d'un dialogue...)."""

    def __init__(self):
        self.proc: Gio.Subprocess | None = None
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        if self.proc is not None:
            self.proc.force_exit()


def run_remote(
    server: Server,
    prefs: Preferences,
    secret: str | None,
    remote_command: str,
    callback: Callable[[RemoteResult], None],
    *,
    retry: str = "fail",
) -> RemoteHandle:
    """Lance ``ssh hôte commande`` sans terminal et rappelle ``callback``.

    Si le multiplexage est actif, ce premier appel établit aussi la
    connexion maîtresse que les terminaux réutiliseront ensuite.
    """
    handle = RemoteHandle()
    resolved = ssh.resolve_config(server)
    argv = ssh.build_ssh_argv(
        server,
        prefs.ssh_options(),
        remote_command=remote_command,
        tty=False,
        runtime_dir=str(runtime_dir()),
        password_prompts=1 if retry == "fail" and secret else None,
        resolved=resolved,
    )
    if os.environ.get("SCREENPORT_DEBUG"):
        log.warning("ssh: %s", " ".join(argv))
    launcher = Gio.SubprocessLauncher.new(
        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
    )
    secret_file = write_secret_file(secret) if secret else None
    env = ssh.askpass_env(askpass_path(), secret_file, server, resolved, retry=retry, force=True)
    for key, value in env.items():
        launcher.setenv(key, value, True)

    try:
        proc = launcher.spawnv(argv)
    except GLib.Error as exc:
        remove_secret_file(secret_file)
        message = exc.message

        def report_failure():
            callback(RemoteResult(127, "", message))
            return GLib.SOURCE_REMOVE

        GLib.idle_add(report_failure)
        return handle

    handle.proc = proc

    def done(proc, result):
        remove_secret_file(secret_file)
        try:
            _ok, out, err = proc.communicate_finish(result)
        except GLib.Error as exc:
            callback(RemoteResult(-1, "", exc.message, cancelled=handle.cancelled))
            return
        code = proc.get_exit_status() if proc.get_if_exited() else -1
        callback(RemoteResult(code, _decode(out), _decode(err), cancelled=handle.cancelled))

    proc.communicate_async(None, None, done)
    return handle


def _decode(data) -> str:
    if data is None:
        return ""
    raw = data.get_data() if hasattr(data, "get_data") else data
    return bytes(raw or b"").decode("utf-8", "replace")


def list_screens(
    server: Server,
    prefs: Preferences,
    secret: str | None,
    callback: Callable[[RemoteResult, ssh.ScreenListing], None],
) -> RemoteHandle:
    def done(result: RemoteResult):
        listing = ssh.parse_list_output(result.stdout)
        if not listing.ok and result.exit_code == 0:
            result.exit_code = 1
        callback(result, listing)

    return run_remote(server, prefs, secret, ssh.remote_list_command(), done)


def close_master(server: Server) -> None:
    """Ferme (sans attendre) la connexion maîtresse partagée d'un serveur."""
    argv = ssh.build_control_exit_argv(server, str(runtime_dir()))
    try:
        Gio.Subprocess.new(
            argv,
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
        )
    except GLib.Error:
        pass
