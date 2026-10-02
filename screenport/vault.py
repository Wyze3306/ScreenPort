"""Stockage des mots de passe et phrases de passe.

Le trousseau du système (Secret Service : GNOME Keyring, KWallet...) est
utilisé en priorité. S'il est indisponible, les secrets sont conservés dans
un fichier lisible uniquement par l'utilisateur (permissions 600).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from .models import read_json, write_private_json

log = logging.getLogger(__name__)

try:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ImportError, ValueError):  # pragma: no cover - dépend du système
    Secret = None

_SCHEMA_NAME = "io.github.wyze3306.ScreenPort"


class Vault:
    def __init__(self, fallback_path: Path):
        self.fallback_path = fallback_path
        self._schema = None
        self._keyring_ok: bool | None = None
        if Secret is not None:
            self._schema = Secret.Schema.new(
                _SCHEMA_NAME,
                Secret.SchemaFlags.NONE,
                {
                    "server": Secret.SchemaAttributeType.STRING,
                    "kind": Secret.SchemaAttributeType.STRING,
                },
            )

    # -- état -------------------------------------------------------------
    @property
    def backend_label(self) -> str:
        return "trousseau du système" if self._use_keyring() else "fichier local protégé"

    def _use_keyring(self) -> bool:
        if self._keyring_ok is None:
            self._keyring_ok = False
            if self._schema is not None:
                try:
                    # OPEN_SESSION démarre réellement le service : échoue vite
                    # si aucun trousseau n'est installé.
                    Secret.Service.get_sync(Secret.ServiceFlags.OPEN_SESSION, None)
                    self._keyring_ok = True
                except Exception as exc:  # GLib.Error, D-Bus absent...
                    log.info("Trousseau indisponible, repli sur fichier : %s", exc)
        return self._keyring_ok

    # -- fichier de repli -------------------------------------------------
    def _file_load(self) -> dict:
        data = read_json(self.fallback_path, {})
        return data if isinstance(data, dict) else {}

    def _file_save(self, data: dict) -> None:
        write_private_json(self.fallback_path, data)

    # -- API --------------------------------------------------------------
    def get(self, server_id: str, kind: str) -> str | None:
        if self._use_keyring():
            try:
                value = Secret.password_lookup_sync(
                    self._schema, {"server": server_id, "kind": kind}, None
                )
                if value:
                    return value
            except Exception as exc:
                log.warning("Lecture du trousseau impossible : %s", exc)
                self._keyring_ok = False
        encoded = self._file_load().get(f"{server_id}:{kind}")
        if encoded:
            try:
                return base64.b64decode(encoded).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
        return None

    def set(self, server_id: str, kind: str, secret: str, label: str) -> None:
        if self._use_keyring():
            try:
                Secret.password_store_sync(
                    self._schema,
                    {"server": server_id, "kind": kind},
                    Secret.COLLECTION_DEFAULT,
                    label,
                    secret,
                    None,
                )
                self._file_delete(server_id, kind)
                return
            except Exception as exc:
                log.warning("Écriture dans le trousseau impossible : %s", exc)
                self._keyring_ok = False
        data = self._file_load()
        data[f"{server_id}:{kind}"] = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        self._file_save(data)

    def delete(self, server_id: str, kind: str | None = None) -> None:
        kinds = [kind] if kind else ["password", "passphrase"]
        for k in kinds:
            if self._use_keyring():
                try:
                    Secret.password_clear_sync(self._schema, {"server": server_id, "kind": k}, None)
                except Exception as exc:
                    log.warning("Suppression dans le trousseau impossible : %s", exc)
            self._file_delete(server_id, k)

    def _file_delete(self, server_id: str, kind: str) -> None:
        if not self.fallback_path.exists():
            return
        data = self._file_load()
        if data.pop(f"{server_id}:{kind}", None) is not None:
            self._file_save(data)
