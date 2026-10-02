"""Construction des commandes ssh / screen et analyse de leurs sorties.

Ce module n'importe pas GTK : il est entièrement testable unitairement.

Toutes les commandes distantes sont envoyées sous la forme
``sh -c '<script>' screenport <arguments>`` : le script ne contient ni
apostrophe, ni point d'exclamation, ni retour à la ligne, ce qui le rend
compatible avec les shells de connexion courants (bash, zsh, dash, fish,
tcsh...). Les arguments (noms de screen) sont validés en amont et ne
contiennent que ``[A-Za-z0-9_-]``.
"""

from __future__ import annotations

import datetime
import os
import re
import shlex
from dataclasses import dataclass, field

from .models import AUTH_KEY, AUTH_PASSWORD, Server, is_valid_screen_name

ATTACH_DETACH = "detach"  # screen -d -r : reprend la session, détache les autres
ATTACH_SHARE = "share"  # screen -x : multi-affichage, partage la session
SHELL_ONLY = "@shell"  # pas de screen : simple shell de connexion

_BEGIN = "__SCREENPORT_BEGIN__"
_END = "__SCREENPORT_END__"
_HAS_SCREEN = "__SCREENPORT_SCREEN__="


def _sh(script_lines: list[str], *args: str) -> str:
    script = ""
    for line in script_lines:
        if script:
            # Pas de « ; » juste après then/else/do (erreur de syntaxe en sh).
            script += " " if script.endswith(("then", "else", "do")) else "; "
        script += line
    for forbidden in ("'", "!", "\n"):
        assert forbidden not in script, f"caractère interdit dans le script : {forbidden!r}"
    quoted_args = " ".join(shlex.quote(a) for a in args)
    return f"sh -c '{script}' screenport {quoted_args}".rstrip()


# Recherche « pid.nom » d'une session existante portant exactement ce nom.
_FIND_ID = (
    'id=$(screen -ls 2>/dev/null | sed -n "s/^[[:space:]]*\\([0-9][0-9]*\\.$n\\)[[:space:]].*/\\1/p"'
    " | head -n 1)"
)


def remote_attach_command(name: str, mode: str = ATTACH_DETACH, history: int = 10000) -> str:
    """Commande distante : se rattache au screen ``name`` ou le crée."""
    if name == SHELL_ONLY:
        return remote_shell_command()
    if not is_valid_screen_name(name):
        raise ValueError(f"nom de screen invalide : {name!r}")
    if mode not in (ATTACH_DETACH, ATTACH_SHARE):
        mode = ATTACH_DETACH
    history = max(100, min(int(history), 1_000_000))
    lines = [
        'n="$1"',
        'mode="$2"',
        'hist="$3"',
        "if command -v screen >/dev/null 2>&1; then :",
        "else printf \"\\r\\n\\033[1;33m[ScreenPort]\\033[0m GNU screen est introuvable sur ce serveur.\\r\\n\"",
        'printf "Installez-le (par exemple : sudo apt install screen). Ouverture d un shell standard.\\r\\n\\r\\n"',
        'exec "${SHELL:-/bin/sh}" -l',
        "fi",
        "screen -wipe >/dev/null 2>&1",
        _FIND_ID,
        'if [ -n "$id" ]; then',
        'if [ "$mode" = "share" ]; then exec screen -U -x "$id"; fi',
        'exec screen -U -d -r "$id"',
        "fi",
        'exec screen -U -h "$hist" -S "$n"',
    ]
    return _sh(lines, name, mode, str(history))


def remote_shell_command() -> str:
    return _sh(['exec "${SHELL:-/bin/sh}" -l'])


def remote_list_command() -> str:
    """Commande distante listant les sessions screen (sortie balisée)."""
    lines = [
        f"echo {_BEGIN}",
        "if command -v screen >/dev/null 2>&1; then",
        f"echo {_HAS_SCREEN}yes",
        "LC_ALL=C screen -ls 2>&1",
        "else",
        f"echo {_HAS_SCREEN}no",
        "fi",
        f"echo {_END}",
    ]
    return _sh(lines)


def remote_kill_command(name: str) -> str:
    """Commande distante terminant le screen ``name`` (toutes ses fenêtres)."""
    if not is_valid_screen_name(name):
        raise ValueError(f"nom de screen invalide : {name!r}")
    lines = [
        'n="$1"',
        _FIND_ID,
        'if [ -n "$id" ]; then screen -S "$id" -X quit; else exit 3; fi',
    ]
    return _sh(lines, name)


def remote_rename_command(old: str, new: str) -> str:
    """Commande distante renommant la session screen ``old`` en ``new``."""
    if not (is_valid_screen_name(old) and is_valid_screen_name(new)):
        raise ValueError("nom de screen invalide")
    lines = [
        'n="$1"',
        _FIND_ID,
        'if [ -z "$id" ]; then exit 3; fi',
        'n="$2"',
        "dup=$(screen -ls 2>/dev/null | grep -c \"[[:space:]][0-9][0-9]*\\.$n[[:space:]]\")",
        'if [ "$dup" -gt 0 ]; then exit 4; fi',
        'screen -S "$id" -X sessionname "$2"',
    ]
    return _sh(lines, old, new)


@dataclass
class ScreenSession:
    pid: int
    name: str
    state: str = ""  # "Attached", "Detached", "Multi, attached", ...
    date: str = ""

    @property
    def id(self) -> str:
        return f"{self.pid}.{self.name}"

    @property
    def attached(self) -> bool:
        return "attached" in self.state.lower() and "detached" not in self.state.lower()

    @property
    def dead(self) -> bool:
        return "dead" in self.state.lower()

    @property
    def pretty_date(self) -> str:
        return format_screen_date(self.date)

    @property
    def state_label(self) -> str:
        lowered = self.state.lower()
        if "dead" in lowered:
            return "Mort"
        if "multi" in lowered and "attached" in lowered and "detached" not in lowered:
            return "Attaché (partagé)"
        if "detached" in lowered:
            return "Détaché"
        if "attached" in lowered:
            return "Attaché"
        return self.state or "Inconnu"


_DATE_FORMATS = (
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%y %I:%M:%S %p",
    "%m/%d/%Y %I:%M:%S %p",
    "%d/%m/%y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


def format_screen_date(text: str) -> str:
    """Date de création affichée par screen -ls, au format français."""
    text = (text or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            value = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return value.strftime("%d/%m/%Y à %H:%M")
    return text


@dataclass
class ScreenListing:
    ok: bool
    has_screen: bool = True
    sessions: list[ScreenSession] = field(default_factory=list)


_SESSION_LINE = re.compile(r"^\s+(\d+)\.(\S.*)$")


def parse_screen_ls(output: str) -> list[ScreenSession]:
    """Analyse la sortie de ``screen -ls``.

    Exemple de lignes reconnues ::

        \t12345.main\t(02/10/2026 09:12:33)\t(Detached)
        \t678.logs\t(Attached)
    """
    sessions: list[ScreenSession] = []
    for line in (output or "").splitlines():
        match = _SESSION_LINE.match(line)
        if not match:
            continue
        pid = int(match.group(1))
        rest = match.group(2)
        parts = [p.strip() for p in rest.split("\t") if p.strip()]
        if not parts:
            continue
        name = parts[0]
        extras = [p[1:-1] if p.startswith("(") and p.endswith(")") else p for p in parts[1:]]
        if len(parts) == 1:
            # Séparateur non tabulé : « 123.nom (Detached) ».
            m = re.match(r"^(\S+)\s+(.*)$", rest)
            if m:
                name = m.group(1)
                extras = re.findall(r"\(([^()]*)\)", m.group(2))
        state = extras[-1] if extras else ""
        date = extras[0] if len(extras) > 1 else ""
        sessions.append(ScreenSession(pid=pid, name=name, state=state, date=date))
    return sessions


def parse_list_output(stdout: str) -> ScreenListing:
    """Analyse la sortie balisée de :func:`remote_list_command`."""
    text = stdout or ""
    if _BEGIN not in text:
        return ScreenListing(ok=False)
    body = text.split(_BEGIN, 1)[1]
    body = body.split(_END, 1)[0]
    has_screen = f"{_HAS_SCREEN}no" not in body
    return ScreenListing(ok=True, has_screen=has_screen, sessions=parse_screen_ls(body))


@dataclass
class SshOptions:
    """Préférences influant sur la ligne de commande ssh."""

    multiplex: bool = True
    persist_seconds: int = 600
    keepalive: int = 30
    accept_new_hostkeys: bool = True
    connect_timeout: int = 15


def control_path(runtime_dir: str) -> str:
    return os.path.join(runtime_dir, "cm-%C")


def build_ssh_argv(
    server: Server,
    options: SshOptions,
    *,
    remote_command: str | None = None,
    tty: bool = True,
    runtime_dir: str | None = None,
    multiplex: bool | None = None,
    password_prompts: int | None = None,
) -> list[str]:
    """Ligne de commande ssh complète pour ``server``.

    ``password_prompts`` limite le nombre de tentatives de mot de passe
    (1 pour échouer vite quand le secret enregistré est refusé).
    """
    argv = ["ssh", "-t" if tty else "-T"]
    if not tty:
        argv.append("-n")
    argv += ["-p", str(int(server.port or 22))]

    opts: list[tuple[str, str]] = [
        ("LogLevel", "ERROR"),
        ("ConnectTimeout", str(int(options.connect_timeout))),
        (
            "StrictHostKeyChecking",
            "accept-new" if options.accept_new_hostkeys else "ask",
        ),
    ]
    if options.keepalive and options.keepalive > 0:
        opts += [
            ("ServerAliveInterval", str(int(options.keepalive))),
            ("ServerAliveCountMax", "3"),
        ]
    if server.auth == AUTH_KEY and server.key_path.strip():
        argv += ["-i", os.path.expanduser(server.key_path.strip())]
        opts.append(("IdentitiesOnly", "yes"))
    elif server.auth == AUTH_PASSWORD:
        opts.append(("PreferredAuthentications", "password,keyboard-interactive,publickey"))
    if password_prompts:
        opts.append(("NumberOfPasswordPrompts", str(int(password_prompts))))

    use_mux = options.multiplex if multiplex is None else multiplex
    if use_mux and runtime_dir:
        opts += [
            ("ControlMaster", "auto"),
            ("ControlPath", control_path(runtime_dir)),
            ("ControlPersist", f"{int(options.persist_seconds)}s"),
        ]

    for key, value in opts:
        argv += ["-o", f"{key}={value}"]
    # « -- » : rien de ce qui suit ne peut être interprété comme une option.
    argv += ["--", server.ssh_host]
    if remote_command:
        argv.append(remote_command)
    return argv


def build_control_exit_argv(server: Server, runtime_dir: str) -> list[str]:
    """Ferme la connexion maîtresse partagée d'un serveur."""
    return [
        "ssh",
        "-p",
        str(int(server.port or 22)),
        "-o",
        f"ControlPath={control_path(runtime_dir)}",
        "-O",
        "exit",
        "--",
        server.ssh_host,
    ]


def askpass_env(
    askpass: str,
    secret: str | None,
    kind: str | None,
    *,
    retry: str = "gui",
    force: bool = True,
) -> dict[str, str]:
    """Variables d'environnement pour que ssh utilise notre askpass.

    ``retry`` : comportement si le secret enregistré est refusé
    (``gui`` : demander à l'utilisateur, ``fail`` : abandonner).
    """
    env: dict[str, str] = {}
    if not force and not secret:
        return env
    env["SSH_ASKPASS"] = askpass
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env["SCREENPORT_ASKPASS_RETRY"] = retry
    if secret:
        env["SCREENPORT_SECRET"] = secret
        env["SCREENPORT_SECRET_KIND"] = kind or "password"
    return env


def command_preview(server: Server) -> str:
    """Commande ssh « humaine » à copier dans un terminal."""
    parts = ["ssh"]
    if server.port and server.port != 22:
        parts += ["-p", str(server.port)]
    if server.auth == AUTH_KEY and server.key_path:
        parts += ["-i", server.key_path]
    parts.append(server.ssh_host)
    return " ".join(shlex.quote(p) for p in parts)


@dataclass
class SshError:
    title: str
    detail: str
    auth_failed: bool = False


def humanize_ssh_error(stderr: str, exit_code: int) -> SshError:
    """Traduit les erreurs ssh courantes en messages compréhensibles."""
    text = (stderr or "").strip()
    lowered = text.lower()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""

    if "remote host identification has changed" in lowered:
        return SshError(
            "La clé du serveur a changé",
            "L'empreinte du serveur ne correspond plus à celle enregistrée "
            "(réinstallation ou attaque de l'homme du milieu). Si le changement est "
            "légitime, supprimez l'ancienne clé avec « ssh-keygen -R <hôte> ».",
        )
    if "host key verification failed" in lowered:
        return SshError(
            "Vérification de la clé du serveur refusée",
            "L'identité du serveur n'a pas été confirmée.",
        )
    if "permission denied" in lowered or "too many authentication failures" in lowered:
        return SshError(
            "Authentification refusée",
            "Le serveur a refusé l'identifiant, le mot de passe ou la clé.",
            auth_failed=True,
        )
    if "could not resolve hostname" in lowered or "name or service not known" in lowered:
        return SshError("Serveur introuvable", "Le nom d'hôte n'a pas pu être résolu (DNS).")
    if "connection refused" in lowered:
        return SshError(
            "Connexion refusée",
            "Aucun serveur SSH n'écoute sur ce port, ou un pare-feu bloque la connexion.",
        )
    if "timed out" in lowered:
        return SshError("Délai dépassé", "Le serveur ne répond pas (réseau, pare-feu ou adresse).")
    if "no route to host" in lowered or "network is unreachable" in lowered:
        return SshError("Serveur injoignable", "Aucune route réseau vers ce serveur.")
    if "connection closed" in lowered or "connection reset" in lowered:
        return SshError("Connexion interrompue", last or "Le serveur a fermé la connexion.")
    if "no such file or directory" in lowered and ("identity" in lowered or "load key" in lowered):
        return SshError("Clé privée introuvable", last)
    if "invalid format" in lowered or "incorrect passphrase" in lowered or "bad passphrase" in lowered:
        return SshError("Clé privée illisible", last or "Phrase de passe incorrecte ou format invalide.")
    if exit_code == 127 or "no such file or directory: ssh" in lowered:
        return SshError("Client SSH absent", "Installez le paquet « openssh-client ».")
    return SshError(
        "Échec de la connexion",
        last or f"ssh s'est arrêté avec le code {exit_code}.",
    )
