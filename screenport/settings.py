"""Préférences de l'application et état de la dernière session."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .models import read_json, write_private_json
from .ssh import ATTACH_DETACH, ATTACH_SHARE, SshOptions


@dataclass
class Preferences:
    # Terminal
    font: str = "Monospace 11"
    palette: str = "tokyo-night"
    scrollback: int = 10000
    cursor_shape: str = "block"  # block | ibeam | underline
    cursor_blink: bool = True
    copy_on_select: bool = False
    audible_bell: bool = False
    # Sessions screen
    attach_mode: str = ATTACH_DETACH
    screen_history: int = 10000
    auto_reconnect: bool = True
    restore_sessions: bool = True
    confirm_close: bool = True
    # Connexion SSH
    multiplex: bool = True
    keepalive: int = 30
    accept_new_hostkeys: bool = True
    # Apparence
    color_scheme: str = "default"  # default | light | dark

    def ssh_options(self) -> SshOptions:
        return SshOptions(
            multiplex=self.multiplex,
            keepalive=self.keepalive,
            accept_new_hostkeys=self.accept_new_hostkeys,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Preferences":
        prefs = cls()
        for f in fields(cls):
            if f.name in (data or {}):
                value = data[f.name]
                default = getattr(prefs, f.name)
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, int):
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                elif isinstance(default, str) and not isinstance(value, str):
                    continue
                setattr(prefs, f.name, value)
        if prefs.attach_mode not in (ATTACH_DETACH, ATTACH_SHARE):
            prefs.attach_mode = ATTACH_DETACH
        if prefs.cursor_shape not in ("block", "ibeam", "underline"):
            prefs.cursor_shape = "block"
        if prefs.color_scheme not in ("default", "light", "dark"):
            prefs.color_scheme = "default"
        prefs.scrollback = max(0, min(prefs.scrollback, 1_000_000))
        prefs.screen_history = max(100, min(prefs.screen_history, 1_000_000))
        prefs.keepalive = max(0, min(prefs.keepalive, 3600))
        return prefs


@dataclass
class WindowState:
    width: int = 1180
    height: int = 760
    maximized: bool = False
    sidebar_visible: bool = True
    mosaic: bool = False
    # Liste de {"server": id, "screen": nom} des onglets ouverts.
    sessions: list[dict] = field(default_factory=list)
    # Onglet sélectionné : {"server": id, "screen": nom}.
    selected_key: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "WindowState":
        state = cls()
        for f in fields(cls):
            if f.name in (data or {}):
                setattr(state, f.name, data[f.name])
        if not isinstance(state.sessions, list):
            state.sessions = []
        state.sessions = [
            {"server": s["server"], "screen": s["screen"]}
            for s in state.sessions
            if isinstance(s, dict) and isinstance(s.get("server"), str) and isinstance(s.get("screen"), str)
        ]
        key = state.selected_key
        if not (isinstance(key, dict) and all(isinstance(key.get(k), str) for k in ("server", "screen"))):
            state.selected_key = {}
        return state


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        data = read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        self.prefs = Preferences.from_dict(data.get("preferences", {}))
        self.window = WindowState.from_dict(data.get("window", {}))

    def save(self) -> None:
        write_private_json(
            self.path,
            {"version": 1, "preferences": asdict(self.prefs), "window": asdict(self.window)},
        )
