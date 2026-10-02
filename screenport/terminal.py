"""Un terminal connecté à une session screen distante."""

from __future__ import annotations

import logging
import os
import random
import signal

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango, Vte

from . import palettes, ssh
from .models import Server
from .paths import askpass_path, remove_secret_file, runtime_dir, write_secret_file

log = logging.getLogger(__name__)

CONNECTING = "connecting"
CONNECTED = "connected"
CLOSED = "closed"

_RECONNECT_DELAYS = (2, 4, 8, 15, 30)
_ZOOM_STEPS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5, 1.7, 2.0, 2.4, 3.0)

# PCRE2_UTF | PCRE2_NO_UTF_CHECK | PCRE2_UCP | PCRE2_MULTILINE
_PCRE2_FLAGS = 0x00080000 | 0x40000000 | 0x00020000 | 0x00000400
_URL_REGEX = r"(?:https?|ftp)://[\w\-.~:/?#\[\]@!$&*+,;=%]+[\w\-~/#=&%]"


def _hangup(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGHUP)
    except (ProcessLookupError, PermissionError):
        pass


def _rgba(value: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(value)
    return color


class TerminalPane(Gtk.Box):
    """Terminal VTE + bandeau d'état, déplaçable entre onglet et mosaïque."""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "close-request": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "focus-in": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "maximize-request": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "bell": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app, server: Server, screen_name: str, secret: str | None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.server = server
        self.screen_name = screen_name
        self.secret = secret
        self.state = CONNECTING
        self.child_pid: int | None = None
        self.reconnect_attempts = 0
        self._reconnect_source = 0
        self._countdown = 0
        self._closing = False
        self.established = False
        self._spawn_row = 0
        self._secret_file: str | None = None
        self._secret_timeout = 0
        self._zoom_index = _ZOOM_STEPS.index(1.0)
        self.add_css_class("terminal-pane")

        self._build_header()
        self._build_terminal()
        self._build_banner()
        self._build_actions()
        self.apply_prefs()
        self.update_labels()

    # ------------------------------------------------------------------ UI
    def _build_header(self):
        self.header = Gtk.Box(spacing=8, css_classes=["pane-header"], visible=False)
        self.dot = Gtk.Box(css_classes=["color-dot", self.server.color], valign=Gtk.Align.CENTER)
        self.header.append(self.dot)
        labels = Gtk.Box(spacing=6, hexpand=True)
        self.header_title = Gtk.Label(xalign=0, css_classes=["heading"], ellipsize=Pango.EllipsizeMode.END)
        self.header_sub = Gtk.Label(
            xalign=0, hexpand=True, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END
        )
        labels.append(self.header_title)
        labels.append(self.header_sub)
        self.header.append(labels)
        self.header_spinner = Gtk.Spinner(visible=False)
        self.header.append(self.header_spinner)
        self.header_status = Gtk.Image.new_from_icon_name("network-offline-symbolic")
        self.header_status.set_visible(False)
        self.header_status.add_css_class("warning")
        self.header.append(self.header_status)
        maximize = Gtk.Button(
            icon_name="view-fullscreen-symbolic",
            tooltip_text="Afficher seul",
            css_classes=["flat", "circular"],
        )
        maximize.connect("clicked", lambda *_: self.emit("maximize-request"))
        self.header.append(maximize)
        close = Gtk.Button(
            icon_name="window-close-symbolic",
            tooltip_text="Fermer le terminal (le screen continue sur le serveur)",
            css_classes=["flat", "circular"],
        )
        close.connect("clicked", lambda *_: self.emit("close-request"))
        self.header.append(close)
        double = Gtk.GestureClick(button=1)
        double.connect("pressed", self._on_header_pressed)
        self.header.add_controller(double)
        self.append(self.header)

    def _build_terminal(self):
        self.terminal = Vte.Terminal(hexpand=True, vexpand=True)
        self.terminal.set_mouse_autohide(True)
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_scroll_on_output(False)
        self.terminal.set_allow_hyperlink(True)
        self.terminal.set_enable_fallback_scrolling(True)
        try:
            regex = Vte.Regex.new_for_match(_URL_REGEX, -1, _PCRE2_FLAGS)
            tag = self.terminal.match_add_regex(regex, 0)
            self.terminal.match_set_cursor_name(tag, "pointer")
        except GLib.Error as exc:  # pragma: no cover
            log.warning("Regex URL refusée par VTE : %s", exc)

        self.terminal.connect("child-exited", self._on_child_exited)
        self.terminal.connect("contents-changed", self._on_contents_changed)
        self.terminal.connect("window-title-changed", self._on_title_changed)
        self.terminal.connect("bell", lambda *_: self.emit("bell"))
        self.terminal.connect("selection-changed", self._on_selection_changed)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", self._on_focus_enter)
        focus.connect("leave", lambda *_: self.remove_css_class("focused"))
        self.terminal.add_controller(focus)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.terminal.add_controller(keys)

        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_click)
        self.terminal.add_controller(click)

        self.scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.terminal, vexpand=True
        )
        self.overlay = Gtk.Overlay(child=self.scrolled, vexpand=True)
        self.append(self.overlay)

    def _build_banner(self):
        self.banner = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            valign=Gtk.Align.END,
            halign=Gtk.Align.CENTER,
            reveal_child=False,
        )
        self.banner.set_margin_bottom(18)
        self.banner.set_margin_start(18)
        self.banner.set_margin_end(18)
        card = Gtk.Box(spacing=14, css_classes=["session-banner"])
        self.banner_icon = Gtk.Image(pixel_size=28, valign=Gtk.Align.CENTER)
        card.append(self.banner_icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.banner_title = Gtk.Label(xalign=0, css_classes=["heading"], wrap=True)
        self.banner_detail = Gtk.Label(
            xalign=0, css_classes=["dim-label"], wrap=True, max_width_chars=52
        )
        texts.append(self.banner_title)
        texts.append(self.banner_detail)
        card.append(texts)
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.banner_reconnect = Gtk.Button(
            label="Reconnecter", css_classes=["suggested-action", "pill"]
        )
        self.banner_reconnect.connect("clicked", lambda *_: self.reconnect())
        self.banner_cancel = Gtk.Button(label="Annuler", css_classes=["pill"], visible=False)
        self.banner_cancel.connect("clicked", lambda *_: self._cancel_reconnect(update=True))
        close = Gtk.Button(label="Fermer", css_classes=["pill"])
        close.connect("clicked", lambda *_: self.emit("close-request"))
        buttons.append(self.banner_reconnect)
        buttons.append(self.banner_cancel)
        buttons.append(close)
        card.append(buttons)
        self.banner.set_child(card)
        self.overlay.add_overlay(self.banner)

    def _build_actions(self):
        group = Gio.SimpleActionGroup()
        self.copy_action = Gio.SimpleAction.new("copy", None)
        self.copy_action.set_enabled(False)
        self.copy_action.connect("activate", lambda *_: self.copy())
        group.add_action(self.copy_action)
        for name, handler in (
            ("paste", self.paste),
            ("select-all", self.select_all),
            ("zoom-in", self.zoom_in),
            ("zoom-out", self.zoom_out),
            ("zoom-reset", self.zoom_reset),
            ("reconnect", self.reconnect),
            ("close", lambda: self.emit("close-request")),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            group.add_action(action)
        self.insert_action_group("term", group)

        menu = Gio.Menu()
        edit = Gio.Menu()
        edit.append("Copier", "term.copy")
        edit.append("Coller", "term.paste")
        edit.append("Tout sélectionner", "term.select-all")
        menu.append_section(None, edit)
        zoom = Gio.Menu()
        zoom.append("Agrandir le texte", "term.zoom-in")
        zoom.append("Réduire le texte", "term.zoom-out")
        zoom.append("Taille normale", "term.zoom-reset")
        menu.append_section(None, zoom)
        session = Gio.Menu()
        session.append("Reconnecter", "term.reconnect")
        session.append("Fermer (le screen continue)", "term.close")
        menu.append_section(None, session)
        self.terminal.set_context_menu_model(menu)

    # ----------------------------------------------------------- libellés
    @property
    def title(self) -> str:
        return self.screen_name if self.screen_name != ssh.SHELL_ONLY else "shell"

    def update_labels(self):
        self.header_title.set_label(self.title)
        self.header_sub.set_label(f"{self.server.display_name} · {self.server.address}")
        for color in list(self.dot.get_css_classes()):
            if color != "color-dot":
                self.dot.remove_css_class(color)
        self.dot.add_css_class(self.server.color)

    def set_mosaic(self, enabled: bool):
        self.header.set_visible(enabled)
        if enabled:
            self.add_css_class("mosaic-cell")
        else:
            self.remove_css_class("mosaic-cell")

    def _on_header_pressed(self, gesture, n_press, _x, _y):
        if n_press == 2:
            self.emit("maximize-request")
        else:
            self.terminal.grab_focus()

    # ----------------------------------------------------------- réglages
    def apply_prefs(self):
        prefs = self.app.settings.prefs
        self.terminal.set_font(Pango.FontDescription.from_string(prefs.font))
        palette = palettes.resolve(prefs.palette, self.app.is_dark())
        background = _rgba(palette.background)
        self.terminal.set_colors(
            _rgba(palette.foreground), background, [_rgba(c) for c in palette.colors]
        )
        if palette.cursor:
            self.terminal.set_color_cursor(_rgba(palette.cursor))
            self.terminal.set_color_cursor_foreground(background)
        else:
            self.terminal.set_color_cursor(None)
            self.terminal.set_color_cursor_foreground(None)
        self.terminal.set_scrollback_lines(prefs.scrollback)
        self.terminal.set_cursor_shape(
            {
                "ibeam": Vte.CursorShape.IBEAM,
                "underline": Vte.CursorShape.UNDERLINE,
            }.get(prefs.cursor_shape, Vte.CursorShape.BLOCK)
        )
        self.terminal.set_cursor_blink_mode(
            Vte.CursorBlinkMode.ON if prefs.cursor_blink else Vte.CursorBlinkMode.OFF
        )
        self.terminal.set_audible_bell(prefs.audible_bell)

    # ---------------------------------------------------------- connexion
    def connect_session(self):
        self._cancel_reconnect()
        self._remove_secret_file()
        self.banner.set_reveal_child(False)
        prefs = self.app.settings.prefs
        remote_command = ssh.remote_attach_command(
            self.screen_name, prefs.attach_mode, prefs.screen_history
        )
        resolved = ssh.resolve_config(self.server)
        argv = ssh.build_ssh_argv(
            self.server,
            prefs.ssh_options(),
            remote_command=remote_command,
            tty=True,
            runtime_dir=str(runtime_dir()),
            resolved=resolved,
        )
        if os.environ.get("SCREENPORT_DEBUG"):
            log.warning("ssh: %s", " ".join(argv))
        if self.secret:
            self._secret_file = write_secret_file(self.secret)
            # Filet de sécurité si la session n'aboutit jamais.
            self._secret_timeout = GLib.timeout_add_seconds(120, self._on_secret_timeout)
        env = ssh.askpass_env(
            askpass_path(), self._secret_file, self.server, resolved, retry="gui", force=bool(self.secret)
        )
        envv = [f"{k}={v}" for k, v in env.items()]
        self.established = False
        # Titre remis à zéro : VTE ne signale un titre que s'il change, et le
        # marqueur de connexion établie doit être détecté à chaque connexion.
        self.terminal.feed(b"\x1b]0;\x07")
        self._spawn_row = self.terminal.get_cursor_position()[1]
        self._set_state(CONNECTING)
        common = (Vte.PtyFlags.DEFAULT, GLib.get_home_dir(), argv, envv, GLib.SpawnFlags.SEARCH_PATH)
        try:
            # Signature de PyGObject ≤ 3.50 : child_setup, child_setup_data, timeout...
            self.terminal.spawn_async(*common, None, None, -1, None, self._on_spawned, None)
        except TypeError:
            self.terminal.spawn_async(*common, None, -1, None, self._on_spawned, None)

    def _on_spawned(self, _terminal, pid, error, *_args):
        if error is not None or pid in (None, -1):
            self._remove_secret_file()
            if self._closing:
                return
            message = error.message if error is not None else "erreur inconnue"
            self._set_state(CLOSED)
            self._show_banner(
                "dialog-error-symbolic",
                "Impossible de lancer ssh",
                f"{message}\nVérifiez que le paquet « openssh-client » est installé.",
            )
            return
        if self._closing:
            # Onglet fermé pendant le lancement : on n'abandonne pas ssh.
            _hangup(pid)
            return
        self.child_pid = pid

    def _on_contents_changed(self, *_):
        if self.state == CONNECTING and self.child_pid:
            self._set_state(CONNECTED)

    def _on_title_changed(self, *_):
        try:
            title = self.terminal.get_window_title() or ""
        except Exception:  # pragma: no cover - API dépréciée dans VTE ≥ 0.78
            return
        if title == ssh.READY_TITLE and not self.established:
            # Le script distant s'exécute : la connexion est authentifiée.
            self.established = True
            self.reconnect_attempts = 0
            self._remove_secret_file()
            self._set_state(CONNECTED)

    def _on_secret_timeout(self):
        self._secret_timeout = 0
        self._remove_secret_file()
        return GLib.SOURCE_REMOVE

    def _remove_secret_file(self):
        if self._secret_timeout:
            GLib.source_remove(self._secret_timeout)
            self._secret_timeout = 0
        remove_secret_file(self._secret_file)
        self._secret_file = None

    def _on_child_exited(self, _terminal, status):
        self.child_pid = None
        self._remove_secret_file()
        if self._closing:
            return
        try:
            code = os.waitstatus_to_exitcode(status)
        except ValueError:
            code = status
        self._set_state(CLOSED)
        tail = self._text_since_spawn()
        auto = False
        if code == 0:
            self.reconnect_attempts = 0
            if "[detached" in tail:
                title = "Screen détaché"
                detail = f"« {self.title} » continue de tourner sur {self.server.display_name}."
            elif "screen is terminating" in tail:
                title = "Screen terminé"
                detail = f"La session « {self.title} » a été fermée sur le serveur."
            else:
                title = "Session terminée"
                detail = "La connexion a été fermée normalement."
            icon = "emblem-ok-symbolic"
        elif code == 255 and self.established:
            # Coupure d'une session qui fonctionnait : on retente.
            title = "Connexion perdue"
            detail = "La connexion SSH a été interrompue."
            icon = "network-offline-symbolic"
            auto = self.app.settings.prefs.auto_reconnect
        elif code == 255:
            error = ssh.humanize_ssh_error(tail, code)
            retrying = self.reconnect_attempts > 0
            title = "Reconnexion impossible" if retrying else error.title
            detail = f"{error.title} : {error.detail}" if retrying else error.detail
            icon = "network-offline-symbolic"
            # Une première connexion ratée n'est pas retentée automatiquement ;
            # une série de reconnexions continue jusqu'à la limite d'essais.
            auto = self.app.settings.prefs.auto_reconnect and retrying and not error.auth_failed
        else:
            title = "Session terminée"
            detail = f"ssh s'est arrêté avec le code {code}."
            icon = "dialog-warning-symbolic"
        self._show_banner(icon, title, detail)
        if auto:
            self._schedule_reconnect()

    def _text_since_spawn(self) -> str:
        """Texte affiché depuis le lancement de ssh (pas le contenu du screen)."""
        text = ""
        try:
            end_row = self.terminal.get_cursor_position()[1]
            text, _length = self.terminal.get_text_range_format(
                Vte.Format.TEXT, self._spawn_row, 0, end_row, self.terminal.get_column_count()
            )
        except Exception:  # pragma: no cover - selon la version de VTE
            text = ""
        lines = [line for line in (text or "").splitlines() if line.strip()]
        return "\n".join(lines[-12:])

    def _schedule_reconnect(self):
        if self.reconnect_attempts >= len(_RECONNECT_DELAYS):
            self.banner_detail.set_label(
                "La reconnexion automatique a échoué. Réessayez quand le serveur sera joignable."
            )
            return
        # Léger décalage aléatoire : quand plusieurs onglets d'un même serveur
        # tombent ensemble, le premier rétablit la connexion partagée et les
        # suivants la réutilisent au lieu de s'authentifier chacun.
        self._countdown = _RECONNECT_DELAYS[self.reconnect_attempts] + random.randint(0, 2)
        self.reconnect_attempts += 1
        self.banner_cancel.set_visible(True)
        self._update_countdown()
        self._reconnect_source = GLib.timeout_add_seconds(1, self._tick)

    def _tick(self):
        self._countdown -= 1
        if self._countdown <= 0:
            self._reconnect_source = 0
            self.reconnect()
            return GLib.SOURCE_REMOVE
        self._update_countdown()
        return GLib.SOURCE_CONTINUE

    def _update_countdown(self):
        self.banner_detail.set_label(
            f"Nouvelle tentative dans {self._countdown} s "
            f"(essai {self.reconnect_attempts}/{len(_RECONNECT_DELAYS)})…"
        )

    def _cancel_reconnect(self, update: bool = False):
        if self._reconnect_source:
            GLib.source_remove(self._reconnect_source)
            self._reconnect_source = 0
        self.banner_cancel.set_visible(False)
        if update:
            self.reconnect_attempts = 0
            self.banner_detail.set_label("Reconnexion automatique annulée.")

    def reconnect(self):
        if self.child_pid:
            return
        self.terminal.feed(b"\r\n\x1b[2m\xe2\x94\x80\xe2\x94\x80 Reconnexion \xe2\x94\x80\xe2\x94\x80\x1b[0m\r\n")
        self.connect_session()

    def _show_banner(self, icon: str, title: str, detail: str):
        self.banner_icon.set_from_icon_name(icon)
        self.banner_title.set_label(title)
        self.banner_detail.set_label(detail)
        self.banner_detail.set_visible(bool(detail))
        self.banner.set_reveal_child(True)

    def _set_state(self, state: str):
        self.state = state
        self.header_spinner.set_visible(state == CONNECTING)
        self.header_spinner.set_spinning(state == CONNECTING)
        self.header_status.set_visible(state == CLOSED)
        self.emit("state-changed")

    def close(self):
        """Ferme le terminal : ssh est arrêté, le screen se détache tout seul."""
        self._closing = True
        self._cancel_reconnect()
        self._remove_secret_file()
        if self.child_pid:
            _hangup(self.child_pid)
            self.child_pid = None

    # --------------------------------------------------------- édition
    def copy(self):
        if self.terminal.get_has_selection():
            self.terminal.copy_clipboard_format(Vte.Format.TEXT)

    def paste(self):
        self.terminal.paste_clipboard()

    def select_all(self):
        self.terminal.select_all()

    def _on_selection_changed(self, *_):
        has = self.terminal.get_has_selection()
        self.copy_action.set_enabled(has)
        if has and self.app.settings.prefs.copy_on_select:
            self.terminal.copy_clipboard_format(Vte.Format.TEXT)

    def zoom_in(self):
        self._set_zoom(self._zoom_index + 1)

    def zoom_out(self):
        self._set_zoom(self._zoom_index - 1)

    def zoom_reset(self):
        self._set_zoom(_ZOOM_STEPS.index(1.0))

    def _set_zoom(self, index: int):
        self._zoom_index = max(0, min(index, len(_ZOOM_STEPS) - 1))
        self.terminal.set_font_scale(_ZOOM_STEPS[self._zoom_index])

    # -------------------------------------------------------- événements
    def _on_focus_enter(self, *_):
        self.add_css_class("focused")
        self.emit("focus-in")

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        mods = state & Gtk.accelerator_get_default_mod_mask()
        ctrl = Gdk.ModifierType.CONTROL_MASK
        shift = Gdk.ModifierType.SHIFT_MASK
        key = Gdk.keyval_to_lower(keyval)
        if mods == ctrl | shift:
            if key == Gdk.KEY_c:
                self.copy()
                return True
            if key == Gdk.KEY_v:
                self.paste()
                return True
        if mods in (ctrl, ctrl | shift):
            if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self.zoom_in()
                return True
            if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract, Gdk.KEY_underscore) and mods == ctrl:
                self.zoom_out()
                return True
            if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0) and mods == ctrl:
                self.zoom_reset()
                return True
        if mods == shift and keyval == Gdk.KEY_Insert:
            self.paste()
            return True
        return False

    def _on_click(self, gesture, _n_press, x, y):
        state = gesture.get_current_event_state()
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return
        uri = self.terminal.check_hyperlink_at(x, y) if hasattr(self.terminal, "check_hyperlink_at") else None
        if not uri:
            match, _tag = self.terminal.check_match_at(x, y)
            uri = match
        if uri:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            Gtk.UriLauncher.new(uri).launch(self.get_root(), None, None, None)
