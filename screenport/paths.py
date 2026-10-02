"""Emplacements des fichiers (configuration, sockets, ressources)."""

import os
import tempfile
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = Path(base) / "screenport"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


_RUNTIME_DIR: Path | None = None
# Longueur maximale d'un chemin de socket Unix (108 sous Linux), avec une marge
# pour le nom « cm-<empreinte de 40 caractères> ».
_SOCKET_DIR_MAX = 108 - len("/cm-") - 40 - 4


def _private_dir(path: Path) -> bool:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid():
            return False
        path.chmod(0o700)
        return True
    except OSError:
        return False


def runtime_dir() -> Path:
    """Dossier privé et court pour les sockets de multiplexage SSH."""
    global _RUNTIME_DIR
    if _RUNTIME_DIR is not None and _RUNTIME_DIR.is_dir():
        return _RUNTIME_DIR
    candidates = []
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        candidates.append(Path(base) / "screenport")
    candidates.append(Path("/tmp") / f"screenport-{os.getuid()}")
    for path in candidates:
        if len(str(path)) <= _SOCKET_DIR_MAX and _private_dir(path):
            _RUNTIME_DIR = path
            return path
    _RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="sp-", dir="/tmp"))
    return _RUNTIME_DIR


def askpass_path() -> str:
    """Programme SSH_ASKPASS fourni avec l'application."""
    override = os.environ.get("SCREENPORT_ASKPASS")
    if override:
        return override
    for candidate in (
        SOURCE_ROOT / "bin" / "screenport-askpass",
        Path("/usr/lib/screenport/bin/screenport-askpass"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return str(SOURCE_ROOT / "bin" / "screenport-askpass")


def icons_dir() -> Path | None:
    """Icônes de l'application quand elle tourne depuis les sources."""
    path = SOURCE_ROOT / "data" / "icons"
    return path if path.is_dir() else None


def stylesheet_path() -> Path:
    return PACKAGE_DIR / "style.css"
