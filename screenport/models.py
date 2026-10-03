"""Profils de serveurs et validation des noms de screen.

Ce module n'importe pas GTK : il est testable avec n'importe quel Python 3.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

AUTH_PASSWORD = "password"
AUTH_KEY = "key"
AUTH_AGENT = "agent"
AUTH_METHODS = (AUTH_PASSWORD, AUTH_KEY, AUTH_AGENT)

# Couleurs proposées pour distinguer les serveurs (nom -> hex).
COLORS = {
    "blue": "#3584e4",
    "teal": "#2190a4",
    "green": "#3a944a",
    "yellow": "#c88800",
    "orange": "#ed5b00",
    "red": "#e62d42",
    "pink": "#d56199",
    "purple": "#9141ac",
    "slate": "#6f8396",
}

_HOST_RE = re.compile(r"[\w.:%-]+")

SCREEN_NAME_MAX = 64
_SCREEN_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,%d}" % SCREEN_NAME_MAX)


def sanitize_screen_name(text: str) -> str:
    """Transforme une saisie libre en nom de session screen valide.

    Les espaces deviennent des tirets, les accents sont retirés et tout
    caractère hors ``[A-Za-z0-9_-]`` est supprimé : le nom peut ainsi être
    transmis au serveur sans aucun problème d'échappement.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[\s.]+", "-", text.strip())
    text = re.sub(r"[^A-Za-z0-9_-]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:SCREEN_NAME_MAX]


def is_valid_screen_name(name: str) -> bool:
    return isinstance(name, str) and bool(_SCREEN_NAME_RE.fullmatch(name))


def parse_screen_list_input(text: str) -> list[str]:
    """Découpe « main, logs build » en noms valides, sans doublons."""
    names: list[str] = []
    for chunk in re.split(r"[,;\n]+", text or ""):
        name = sanitize_screen_name(chunk)
        if name and name not in names:
            names.append(name)
    return names


def parse_address(text: str) -> tuple[str, str, int | None]:
    """Analyse « user@hôte:port » (IPv6 entre crochets acceptée).

    Retourne ``(user, host, port)`` ; les parties absentes valent ``""`` ou
    ``None``.
    """
    text = (text or "").strip()
    for prefix in ("ssh://",):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.rstrip("/")
    user = ""
    if "@" in text:
        user, text = text.rsplit("@", 1)
    port: int | None = None
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            host = text[1:end]
            rest = text[end + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                port = int(rest[1:])
            return user, host, port
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        if port_text.isdigit():
            port = int(port_text)
            text = host
    return user, text, port


def initials(name: str) -> str:
    if re.fullmatch(r"[0-9.]+", name or "") or (name or "").count(":") > 1:
        # Adresse IP : le dernier octet distingue mieux les machines.
        last = [p for p in re.split(r"[.:]", name) if p]
        return (last[-1][-3:] if last else "IP").upper()
    words = [w for w in re.split(r"[\s._@-]+", name or "") if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


@dataclass
class Server:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    auth: str = AUTH_PASSWORD
    key_path: str = ""
    remember_secret: bool = True
    # IPQoS=none : certains réseaux (box, partage de connexion 4G/5G) jettent
    # les paquets que ssh marque en QoS, et la connexion finit en délai dépassé.
    disable_qos: bool = True
    color: str = "blue"
    favorite_screens: list[str] = field(default_factory=list)
    last_used: float = 0.0

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.host

    @property
    def target(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.user}@{host}" if self.user else host

    @property
    def ssh_host(self) -> str:
        """Destination passée à ssh (sans crochets IPv6, port séparé)."""
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def address(self) -> str:
        if self.port and self.port != 22:
            return f"{self.target}:{self.port}"
        return self.target

    @property
    def secret_kind(self) -> str | None:
        if self.auth == AUTH_PASSWORD:
            return "password"
        if self.auth == AUTH_KEY:
            return "passphrase"
        return None

    def validate(self) -> list[str]:
        errors = []
        if not self.host.strip():
            errors.append("L'adresse du serveur est obligatoire.")
        elif (
            not _HOST_RE.fullmatch(self.host)
            or self.host.startswith("-")
            or self.host.count(":") == 1
        ):
            errors.append("L'adresse du serveur n'est pas valide.")
        # Mêmes règles que ssh (valid_ruser) : DOMAINE\\nom ou nom@domaine restent possibles.
        if (
            self.user.startswith("-")
            or any(c.isspace() or c in "'`\";&<>|(){}" for c in self.user)
            or self.user.endswith("\\")
        ):
            errors.append("Le nom d'utilisateur n'est pas valide.")
        if not 1 <= int(self.port) <= 65535:
            errors.append("Le port doit être compris entre 1 et 65535.")
        if self.auth not in AUTH_METHODS:
            errors.append("Méthode d'authentification inconnue.")
        if self.auth == AUTH_KEY and not self.key_path.strip():
            errors.append("Choisissez un fichier de clé privée.")
        return errors

    def copy(self, **changes) -> "Server":
        data = asdict(self)
        data.update(changes)
        return Server.from_dict(data)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Server":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        server = cls(**clean)
        try:
            server.port = int(server.port)
        except (TypeError, ValueError):
            server.port = 22
        if server.auth not in AUTH_METHODS:
            server.auth = AUTH_PASSWORD
        server.disable_qos = bool(server.disable_qos)
        if server.color not in COLORS:
            server.color = "blue"
        if not isinstance(server.favorite_screens, list):
            server.favorite_screens = []
        server.favorite_screens = [
            n for n in (sanitize_screen_name(str(x)) for x in server.favorite_screens) if n
        ]
        return server


def write_private_json(path: Path, data) -> None:
    """Écrit un JSON de façon atomique, lisible uniquement par l'utilisateur."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # Fichier corrompu : on le met de côté plutôt que de tout perdre.
        try:
            os.replace(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
        return default


class ServerStore:
    """Liste des serveurs enregistrés (sans aucun secret)."""

    def __init__(self, path: Path):
        self.path = path
        self.servers: list[Server] = []
        self.load()

    def load(self) -> None:
        data = read_json(self.path, {})
        items = data.get("servers", []) if isinstance(data, dict) else []
        self.servers = []
        for item in items:
            if isinstance(item, dict):
                server = Server.from_dict(item)
                if server.host:
                    self.servers.append(server)

    def save(self) -> None:
        write_private_json(
            self.path, {"version": 1, "servers": [s.to_dict() for s in self.servers]}
        )

    def get(self, server_id: str) -> Server | None:
        return next((s for s in self.servers if s.id == server_id), None)

    def find(self, query: str) -> Server | None:
        """Retrouve un serveur par id, nom, ou adresse (ligne de commande)."""
        query = (query or "").strip()
        if not query:
            return None
        for server in self.servers:
            if query == server.id:
                return server
        lowered = query.lower()
        for server in self.servers:
            if server.display_name.lower() == lowered:
                return server
        for server in self.servers:
            if lowered in (server.address.lower(), server.target.lower(), server.host.lower()):
                return server
        return None

    def sorted(self) -> list[Server]:
        return sorted(self.servers, key=lambda s: s.display_name.lower())

    def upsert(self, server: Server) -> None:
        for index, existing in enumerate(self.servers):
            if existing.id == server.id:
                self.servers[index] = server
                break
        else:
            self.servers.append(server)
        self.save()

    def remove(self, server_id: str) -> None:
        self.servers = [s for s in self.servers if s.id != server_id]
        self.save()
