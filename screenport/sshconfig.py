"""Lecture de ~/.ssh/config pour importer des serveurs (sans GTK)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .models import AUTH_AGENT, AUTH_KEY, Server

_LINE = re.compile(r"^\s*(\S+?)\s*(?:=\s*|\s+)(.*?)\s*$")


@dataclass
class SshConfigHost:
    alias: str
    hostname: str = ""
    user: str = ""
    port: int = 22
    identity_file: str = ""
    proxy_jump: str = ""

    def to_server(self) -> Server:
        # On garde l'alias comme hôte : ssh appliquera ainsi toutes les options
        # de ~/.ssh/config (ProxyJump, etc.).
        server = Server(
            name=self.alias,
            host=self.alias,
            user=self.user,
            port=self.port,
            auth=AUTH_KEY if self.identity_file else AUTH_AGENT,
            key_path=self.identity_file,
        )
        return server

    @property
    def description(self) -> str:
        target = self.hostname or self.alias
        if self.user:
            target = f"{self.user}@{target}"
        if self.port != 22:
            target += f":{self.port}"
        if self.proxy_jump:
            target += f" via {self.proxy_jump}"
        return target


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_ssh_config(text: str) -> list[SshConfigHost]:
    hosts: list[SshConfigHost] = []
    current: list[SshConfigHost] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        value = _unquote(match.group(2))
        if key == "host":
            current = []
            for pattern in value.split():
                if any(c in pattern for c in "*?!"):
                    continue
                entry = SshConfigHost(alias=pattern)
                hosts.append(entry)
                current.append(entry)
        elif key == "match":
            current = []
        else:
            for entry in current:
                if key == "hostname" and not entry.hostname:
                    entry.hostname = value
                elif key == "user" and not entry.user:
                    entry.user = value
                elif key == "port" and value.isdigit() and entry.port == 22:
                    entry.port = int(value)
                elif key == "identityfile" and not entry.identity_file:
                    entry.identity_file = value
                elif key == "proxyjump" and not entry.proxy_jump:
                    entry.proxy_jump = value
    # Un alias peut apparaître plusieurs fois : on garde le premier.
    seen: set[str] = set()
    unique = []
    for entry in hosts:
        if entry.alias not in seen:
            seen.add(entry.alias)
            unique.append(entry)
    return unique


def load_user_ssh_config() -> list[SshConfigHost]:
    path = os.path.expanduser("~/.ssh/config")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_ssh_config(fh.read())
    except OSError:
        return []
