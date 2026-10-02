"""Programme SSH_ASKPASS de ScreenPort.

ssh l'exécute avec le texte de la question en argument et lit la réponse
sur la sortie standard.

* Si l'application a fourni un secret (mot de passe ou phrase de passe) et que
  la question y correspond, le secret est renvoyé directement — une seule
  fois par processus ssh, pour ne jamais boucler sur un mot de passe refusé.
* Sinon (code de vérification, confirmation d'empreinte, secret refusé...),
  une petite fenêtre demande la réponse à l'utilisateur.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_SECRET_PATTERNS = {
    "password": re.compile(r"password|mot de passe|passwort|contrase|senha", re.I),
    "passphrase": re.compile(r"passphrase|phrase de passe", re.I),
}
_CONFIRM = re.compile(r"\(yes/no", re.I)


def _runtime_dir() -> Path:
    from .paths import runtime_dir

    return runtime_dir()


def _process_start_time(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
            stat = fh.read()
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return "?"


def _first_use(kind: str) -> bool:
    """Vrai si le secret n'a pas encore été envoyé à ce processus ssh."""
    ppid = os.getppid()
    marker = _runtime_dir() / f"askpass-{ppid}-{kind}"
    stamp = _process_start_time(ppid)
    try:
        if marker.read_text() == stamp:
            return False
    except OSError:
        pass
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(stamp)
    except OSError:
        pass
    return True


def _answer(text: str) -> int:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    prompt = argv[1] if len(argv) > 1 else ""
    secret = os.environ.get("SCREENPORT_SECRET", "")
    kind = os.environ.get("SCREENPORT_SECRET_KIND", "password")
    retry = os.environ.get("SCREENPORT_ASKPASS_RETRY", "gui")
    confirm = bool(_CONFIRM.search(prompt))

    note = ""
    pattern = _SECRET_PATTERNS.get(kind)
    if secret and not confirm and pattern and pattern.search(prompt):
        if _first_use(kind):
            return _answer(secret)
        if retry == "fail":
            return 1
        note = "Le secret enregistré a été refusé par le serveur."

    if os.environ.get("SCREENPORT_ASKPASS_NO_GUI"):
        return 1
    return _run_dialog(prompt, confirm, note)


def _run_dialog(prompt: str, confirm: bool, note: str) -> int:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, Gtk
    except (ImportError, ValueError):
        return 1

    result: dict[str, str | None] = {"value": None}

    def build(app):
        win = Adw.ApplicationWindow(application=app, title="ScreenPort", resizable=False)
        win.set_default_size(440, -1)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_bottom(24)

        icon = Gtk.Image.new_from_icon_name(
            "channel-secure-symbolic" if confirm else "dialog-password-symbolic"
        )
        icon.set_pixel_size(48)
        icon.add_css_class("accent")
        box.append(icon)

        title = Gtk.Label(
            label="Vérifier l'identité du serveur" if confirm else "Authentification SSH",
            css_classes=["title-2"],
            wrap=True,
        )
        box.append(title)
        if note:
            box.append(Gtk.Label(label=note, css_classes=["error"], wrap=True))
        text = Gtk.Label(label=prompt.strip(), wrap=True, xalign=0.5)
        text.set_max_width_chars(48)
        text.set_justify(Gtk.Justification.CENTER)
        box.append(text)

        entry = None
        if not confirm:
            entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
            entry.set_margin_top(6)
            box.append(entry)

        buttons = Gtk.Box(spacing=12, homogeneous=True, margin_top=6)
        cancel = Gtk.Button(label="Refuser" if confirm else "Annuler")
        ok = Gtk.Button(
            label="Faire confiance" if confirm else "Valider",
            css_classes=["suggested-action"],
        )
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)

        def finish(value):
            result["value"] = value
            win.close()

        cancel.connect("clicked", lambda *_: finish("no" if confirm else None))
        ok.connect(
            "clicked", lambda *_: finish("yes" if confirm else (entry.get_text() if entry else ""))
        )
        if entry is not None:
            entry.connect("activate", lambda *_: ok.emit("clicked"))
        win.set_default_widget(ok)

        toolbar.set_content(box)
        win.set_content(toolbar)
        win.present()
        (entry if entry is not None else ok).grab_focus()

    app = Adw.Application(
        application_id="io.github.wyze3306.ScreenPort.AskPass",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    app.connect("activate", build)
    app.run([])
    if result["value"] is None:
        return 1
    return _answer(result["value"])


if __name__ == "__main__":
    sys.exit(main())
