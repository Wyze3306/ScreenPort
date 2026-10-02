"""Point d'entrée de l'application GTK."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

import gi

_MISSING = []
for _namespace, _version, _package in (
    ("Gtk", "4.0", "gir1.2-gtk-4.0"),
    ("Adw", "1", "gir1.2-adw-1"),
    ("Vte", "3.91", "gir1.2-vte-3.91"),
):
    try:
        gi.require_version(_namespace, _version)
    except ValueError:
        _MISSING.append(_package)
if _MISSING:
    sys.stderr.write(
        "ScreenPort : bibliothèques manquantes. Installez-les avec :\n"
        f"  sudo apt install {' '.join(_MISSING)}\n"
    )
    sys.exit(1)

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, WEBSITE  # noqa: E402
from .models import ServerStore  # noqa: E402
from .paths import config_dir, icons_dir, runtime_dir, stylesheet_path  # noqa: E402
from .settings import SettingsStore  # noqa: E402
from .vault import Vault  # noqa: E402

log = logging.getLogger("screenport")

USAGE = (
    "Utilisation : screenport [--open SERVEUR [--screen NOM]...]\n\n"
    "  -o, --open SERVEUR   ouvre le serveur (nom, id ou utilisateur@hôte)\n"
    "  -s, --screen NOM     ouvre directement ce screen (répétable)\n"
    "  -v, --version        affiche la version\n"
)

ACCELS = {
    "win.new-screen": ["<Control><Shift>t"],
    "win.close-tab": ["<Control><Shift>w"],
    "win.add-server": ["<Control><Shift>n"],
    "win.quick-connect": ["<Control><Shift>k"],
    "win.open-current": ["<Control><Shift>o"],
    "win.rename-screen": ["<Control><Shift>r"],
    "win.mosaic": ["<Control><Shift>m"],
    "win.toggle-sidebar": ["<Control><Shift>b"],
    "win.fullscreen": ["F11"],
    "app.preferences": ["<Control>comma"],
    "app.shortcuts": ["<Control><Shift>question"],
    "app.quit": ["<Control><Shift>q"],
}

_SHORTCUTS_UI = """
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts">
    <property name="modal">1</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="section-name">main</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Sessions</property>
            {sessions}
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Onglets</property>
            {tabs}
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Terminal</property>
            {terminal}
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Application</property>
            {app}
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""


def _shortcut(accel: str, title: str) -> str:
    return (
        '<child><object class="GtkShortcutsShortcut">'
        f'<property name="accelerator">{GLib.markup_escape_text(accel)}</property>'
        f'<property name="title">{GLib.markup_escape_text(title)}</property>'
        "</object></child>"
    )


class ScreenPortApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings: SettingsStore | None = None
        self.store: ServerStore | None = None
        self.vault: Vault | None = None
        self._restored = False
        self._preferences = None

    # ------------------------------------------------------------ cycle
    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        cfg = config_dir()
        self.settings = SettingsStore(cfg / "settings.json")
        self.store = ServerStore(cfg / "servers.json")
        self.vault = Vault(cfg / "secrets.json")

        display = Gdk.Display.get_default()
        if display is not None:
            provider = Gtk.CssProvider()
            provider.load_from_path(str(stylesheet_path()))
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            extra_icons = icons_dir()
            if extra_icons is not None:
                Gtk.IconTheme.get_for_display(display).add_search_path(str(extra_icons))
        Gtk.Window.set_default_icon_name(APP_ID)

        for name, handler in (
            ("preferences", self.show_preferences),
            ("shortcuts", self.show_shortcuts),
            ("about", self.show_about),
            ("quit", self.quit_app),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_unix_signal)

        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self.apply_prefs(style_only=True))
        self._apply_color_scheme()
        self._cleanup_runtime()

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        open_target = None
        screens: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("-o", "--open") and index + 1 < len(args):
                open_target = args[index + 1]
                index += 1
            elif arg.startswith("--open="):
                open_target = arg.split("=", 1)[1]
            elif arg in ("-s", "--screen") and index + 1 < len(args):
                screens.append(args[index + 1])
                index += 1
            elif arg.startswith("--screen="):
                screens.append(arg.split("=", 1)[1])
            else:
                command_line.printerr_literal(f"Option inconnue : {arg}\n")
                return 1
            index += 1

        self.activate()
        window = self.props.active_window
        if open_target and window is not None:
            server = self.store.find(open_target)
            if server is None:
                window.toast(f"Serveur « {open_target} » introuvable")
            elif screens:
                from .models import parse_screen_list_input

                names = parse_screen_list_input(",".join(screens))
                window.with_secret(server, lambda secret: window.connect_and_open(server, names, secret))
            else:
                window.open_server(server.id)
        return 0

    def do_activate(self):
        from .window import MainWindow

        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
        window.present()
        if not self._restored:
            self._restored = True
            state = self.settings.window
            if self.settings.prefs.restore_sessions and state.sessions:
                entries = list(state.sessions)
                GLib.idle_add(
                    lambda: window.restore_sessions(entries, state.selected, state.mosaic) and False
                )

    def _on_unix_signal(self):
        """Arrêt demandé par le système : on sauvegarde les onglets d'abord."""
        for window in self.get_windows():
            if hasattr(window, "shutdown"):
                window.shutdown()
        self.quit()
        return GLib.SOURCE_REMOVE

    # ---------------------------------------------------------- réglages
    def is_dark(self) -> bool:
        return Adw.StyleManager.get_default().get_dark()

    def _apply_color_scheme(self):
        scheme = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(self.settings.prefs.color_scheme, Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def apply_prefs(self, style_only: bool = False):
        if not style_only:
            self._apply_color_scheme()
        for window in self.get_windows():
            if hasattr(window, "apply_prefs"):
                window.apply_prefs()

    def _cleanup_runtime(self):
        """Supprime les marqueurs askpass de plus d'un jour."""
        try:
            directory = runtime_dir()
            limit = time.time() - 86400
            for entry in directory.glob("askpass-*"):
                try:
                    if entry.stat().st_mtime < limit:
                        entry.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # ----------------------------------------------------------- fenêtres
    def show_preferences(self):
        from .preferences import PreferencesDialog

        dialog = PreferencesDialog(self)
        dialog.present(self.props.active_window)

    def show_shortcuts(self):
        sessions = "".join(
            _shortcut(a, t)
            for a, t in (
                ("<Control><Shift>t", "Nouveau screen sur le serveur courant"),
                ("<Control><Shift>o", "Ouvrir des screens sur le serveur courant"),
                ("<Control><Shift>r", "Renommer le screen"),
                ("<Control><Shift>w", "Fermer le terminal (le screen continue)"),
                ("<Control><Shift>n", "Ajouter un serveur"),
                ("<Control><Shift>k", "Connexion rapide"),
            )
        )
        tabs = "".join(
            _shortcut(a, t)
            for a, t in (
                ("<Control>Page_Down", "Onglet suivant"),
                ("<Control>Page_Up", "Onglet précédent"),
                ("<Alt>1...9", "Aller à l'onglet 1 à 9"),
                ("<Control><Shift>Page_Down", "Déplacer l'onglet à droite"),
                ("<Control><Shift>m", "Vue mosaïque"),
            )
        )
        terminal = "".join(
            _shortcut(a, t)
            for a, t in (
                ("<Control><Shift>c", "Copier"),
                ("<Control><Shift>v", "Coller"),
                ("<Control>plus", "Agrandir le texte"),
                ("<Control>minus", "Réduire le texte"),
                ("<Control>0", "Taille normale"),
                ("<Control>a d", "Détacher le screen (raccourci de screen)"),
            )
        )
        app = "".join(
            _shortcut(a, t)
            for a, t in (
                ("<Control><Shift>b", "Afficher/masquer la liste des serveurs"),
                ("F11", "Plein écran"),
                ("<Control>comma", "Préférences"),
                ("<Control><Shift>q", "Quitter"),
            )
        )
        builder = Gtk.Builder.new_from_string(
            _SHORTCUTS_UI.format(sessions=sessions, tabs=tabs, terminal=terminal, app=app), -1
        )
        window = builder.get_object("shortcuts")
        window.set_transient_for(self.props.active_window)
        window.present()

    def show_about(self):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            developer_name="Wyze3306",
            license_type=Gtk.License.MIT_X11,
            website=WEBSITE,
            issue_url=f"{WEBSITE}/issues",
            comments="Client SSH pour ouvrir plusieurs terminaux, chacun sur sa propre "
            "session GNU screen nommée. Si le screen existe, ScreenPort s'y rattache ; "
            "sinon il le crée.",
            copyright="© 2026 Wyze3306",
        )
        about.present(self.props.active_window)

    def quit_app(self):
        window = self.props.active_window
        if window is not None:
            window.close()
        else:
            self.quit()


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    # Options ne nécessitant pas d'affichage graphique.
    if any(a in ("-v", "--version") for a in argv[1:]):
        print(f"{APP_NAME} {VERSION}")
        return 0
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(USAGE, end="")
        return 0
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("SCREENPORT_DEBUG") else logging.WARNING,
        format="%(name)s: %(message)s",
    )
    GLib.set_prgname(APP_ID)
    app = ScreenPortApp()
    return app.run(argv)
