"""Fenêtre principale : serveurs à gauche, terminaux en onglets ou en mosaïque."""

from __future__ import annotations

import math
import time
import uuid
from collections import OrderedDict

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from . import APP_ID, remote, ssh
from .dialogs import (
    OpenScreensDialog,
    ServerEditorDialog,
    ask_password,
    ask_screen_name,
    confirm,
    confirm_kill,
)
from .models import COLORS, Server, initials, is_valid_screen_name
from .terminal import CLOSED, CONNECTING, TerminalPane


class PaneSlot(Adw.Bin):
    """Contenu d'un onglet : accueille le terminal hors mode mosaïque."""

    def __init__(self, pane: TerminalPane):
        super().__init__(child=pane)
        self.pane = pane


class ServerRow(Gtk.ListBoxRow):
    def __init__(self, server: Server, menu: Gio.MenuModel):
        super().__init__()
        self.server = server
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=2, margin_end=0)
        self.badge = Gtk.Label(
            label=initials(server.display_name),
            css_classes=["server-badge", server.color],
            valign=Gtk.Align.CENTER,
        )
        box.append(self.badge)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        title = Gtk.Label(
            label=server.display_name,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["server-title"],
        )
        subtitle = Gtk.Label(
            label=server.address,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["dim-label", "caption"],
        )
        texts.append(title)
        texts.append(subtitle)
        box.append(texts)
        self.count = Gtk.Label(css_classes=["count-pill"], valign=Gtk.Align.CENTER, visible=False)
        box.append(self.count)
        more = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=menu,
            valign=Gtk.Align.CENTER,
            tooltip_text="Actions",
            css_classes=["flat", "circular", "row-menu"],
        )
        box.append(more)
        self.set_child(box)
        self.set_tooltip_text(f"Ouvrir des screens sur {server.display_name}")

    def set_count(self, count: int):
        self.count.set_label(str(count))
        self.count.set_visible(count > 0)
        self.count.set_tooltip_text(
            f"{count} terminal ouvert" if count == 1 else f"{count} terminaux ouverts"
        )


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ScreenPort")
        self.set_size_request(360, 400)
        self.app = app
        # Partagés entre toutes les fenêtres (un onglet peut changer de fenêtre).
        self.secret_cache: dict[str, str] = app.secret_cache
        self.ephemeral: dict[str, Server] = app.ephemeral
        self._mosaic = False
        self._focused_pane: TerminalPane | None = None
        self._menu_page: Adw.TabPage | None = None
        self._server_rows: dict[str, ServerRow] = {}
        self._icons: dict[str, Gio.Icon] = {}
        self._close_toast: Adw.Toast | None = None
        self._force_close = False
        self._closing = False
        self._restoring = False
        self._restore_entries: list[dict] = []
        self._save_source = 0

        state = app.settings.window
        self.set_default_size(max(640, state.width), max(420, state.height))
        if state.maximized:
            self.maximize()

        self._setup_actions()
        self._build_ui()
        self.split.set_show_sidebar(state.sidebar_visible)
        self.refresh_servers()
        self._on_pages_changed()
        self.connect("close-request", self._on_close_request)

    # ================================================================== UI
    def _build_ui(self):
        self.toasts = Adw.ToastOverlay()
        self.split = Adw.OverlaySplitView(
            min_sidebar_width=250, max_sidebar_width=340, sidebar_width_fraction=0.24
        )
        self.split.set_sidebar(self._build_sidebar())
        self.split.set_content(self._build_content())
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

    def _main_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Connexion rapide…", "win.quick-connect")
        section.append("Ajouter un serveur…", "win.add-server")
        section.append("Importer depuis ~/.ssh/config…", "win.import-ssh-config")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Vue mosaïque", "win.mosaic")
        section.append("Plein écran", "win.fullscreen")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Préférences", "app.preferences")
        section.append("Raccourcis clavier", "app.shortcuts")
        section.append("À propos de ScreenPort", "app.about")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Quitter", "app.quit")
        menu.append_section(None, section)
        return menu

    def _build_sidebar(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="ScreenPort"))
        add = Gtk.Button(
            icon_name="list-add-symbolic",
            tooltip_text="Ajouter un serveur (Ctrl+Maj+N)",
            action_name="win.add-server",
        )
        header.pack_start(add)
        menu = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=self._main_menu(),
            tooltip_text="Menu principal",
            primary=True,
        )
        header.pack_end(menu)
        view.add_top_bar(header)

        self.search = Gtk.SearchEntry(placeholder_text="Rechercher un serveur")
        self.search.set_margin_start(10)
        self.search.set_margin_end(10)
        self.search.set_margin_bottom(6)
        self.search.connect("search-changed", lambda *_: self.server_list.invalidate_filter())

        self.server_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.server_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.server_list.set_filter_func(self._filter_server_row)
        self.server_list.connect("row-activated", self._on_server_activated)
        no_match = Adw.StatusPage(
            icon_name="system-search-symbolic", title="Aucun résultat", css_classes=["compact"]
        )
        self.server_list.set_placeholder(no_match)
        scrolled = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.server_list
        )
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        list_box.append(self.search)
        list_box.append(scrolled)

        empty = Adw.StatusPage(
            icon_name="network-server-symbolic",
            title="Aucun serveur",
            description="Ajoutez un serveur pour vous y connecter en un clic.",
            css_classes=["compact"],
            vexpand=True,
        )
        empty_button = Gtk.Button(
            label="Ajouter un serveur",
            css_classes=["pill", "suggested-action"],
            halign=Gtk.Align.CENTER,
            action_name="win.add-server",
        )
        empty.set_child(empty_button)

        self.sidebar_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.sidebar_stack.add_named(list_box, "list")
        self.sidebar_stack.add_named(empty, "empty")
        view.set_content(self.sidebar_stack)

        bottom = Gtk.Box(css_classes=["sidebar-bottom"])
        quick = Gtk.Button(
            child=Adw.ButtonContent(label="Connexion rapide", icon_name="utilities-terminal-symbolic"),
            action_name="win.quick-connect",
            hexpand=True,
            tooltip_text="Se connecter sans enregistrer de serveur (Ctrl+Maj+K)",
            css_classes=["flat"],
        )
        bottom.append(quick)
        view.add_bottom_bar(bottom)
        return view

    def _build_content(self) -> Gtk.Widget:
        view = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.RAISED_BORDER)
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="ScreenPort")
        header.set_title_widget(self.window_title)

        sidebar_button = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic", tooltip_text="Liste des serveurs (F9)"
        )
        self.split.bind_property(
            "show-sidebar",
            sidebar_button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        header.pack_start(sidebar_button)
        new_button = Gtk.Button(
            icon_name="tab-new-symbolic",
            tooltip_text="Nouveau screen sur ce serveur (Ctrl+Maj+T)",
            action_name="win.new-screen",
        )
        header.pack_start(new_button)

        mosaic_button = Gtk.ToggleButton(
            icon_name="view-grid-symbolic",
            tooltip_text="Vue mosaïque : tous les terminaux côte à côte (Ctrl+Maj+M)",
            action_name="win.mosaic",
        )
        header.pack_end(mosaic_button)
        view.add_top_bar(header)

        self.tab_view = Adw.TabView()
        self.tab_view.set_shortcuts(
            Adw.TabViewShortcuts.CONTROL_TAB
            | Adw.TabViewShortcuts.CONTROL_SHIFT_TAB
            | Adw.TabViewShortcuts.CONTROL_PAGE_UP
            | Adw.TabViewShortcuts.CONTROL_PAGE_DOWN
            | Adw.TabViewShortcuts.CONTROL_SHIFT_PAGE_UP
            | Adw.TabViewShortcuts.CONTROL_SHIFT_PAGE_DOWN
            | Adw.TabViewShortcuts.ALT_DIGITS
            | Adw.TabViewShortcuts.ALT_ZERO
        )
        self.tab_view.set_menu_model(self._tab_menu())
        self.tab_view.connect("close-page", self._on_close_page)
        self.tab_view.connect("notify::selected-page", self._on_selected_page)
        self.tab_view.connect("page-attached", self._on_page_attached)
        self.tab_view.connect("page-detached", self._on_page_detached)
        self.tab_view.connect("page-reordered", lambda *_: self._on_pages_changed())
        self.tab_view.connect("setup-menu", self._on_setup_menu)
        self.tab_view.connect("indicator-activated", self._on_indicator_activated)
        self.tab_view.connect("create-window", self._on_create_window)

        self.tab_bar = Adw.TabBar(view=self.tab_view, autohide=False)
        self.tab_bar.set_end_action_widget(
            Gtk.Button(
                icon_name="list-add-symbolic",
                tooltip_text="Nouveau screen sur ce serveur (Ctrl+Maj+T)",
                action_name="win.new-screen",
                css_classes=["flat"],
            )
        )
        view.add_top_bar(self.tab_bar)

        self.mosaic = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, homogeneous=True, css_classes=["mosaic"]
        )
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_welcome(), "welcome")
        self.stack.add_named(self.tab_view, "tabs")
        self.stack.add_named(self.mosaic, "mosaic")
        view.set_content(self.stack)
        return view

    def _build_welcome(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name=APP_ID,
            title="Bienvenue dans ScreenPort",
            description="Connectez-vous à vos serveurs en SSH et ouvrez autant de sessions "
            "<b>screen</b> que vous voulez, chacune dans son propre terminal.",
            vexpand=True,
        )
        page.add_css_class("welcome")
        clamp = Adw.Clamp(maximum_size=460)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(
            Gtk.Button(
                label="Ajouter un serveur",
                css_classes=["pill", "suggested-action"],
                action_name="win.add-server",
            )
        )
        buttons.append(
            Gtk.Button(label="Connexion rapide", css_classes=["pill"], action_name="win.quick-connect")
        )
        box.append(buttons)
        self.recent_title = Gtk.Label(
            label="Vos serveurs", xalign=0, css_classes=["heading"], margin_top=12
        )
        box.append(self.recent_title)
        self.recent_list = Gtk.ListBox(css_classes=["boxed-list"])
        self.recent_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.recent_list.connect("row-activated", self._on_recent_activated)
        box.append(self.recent_list)
        clamp.set_child(box)
        page.set_child(clamp)
        return page

    def _tab_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Renommer le screen…", "win.rename-screen")
        section.append("Reconnecter", "win.reconnect")
        section.append("Nouveau screen sur ce serveur…", "win.new-screen")
        section.append("Déplacer dans une nouvelle fenêtre", "win.move-to-window")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Fermer (le screen continue)", "win.close-tab")
        section.append("Terminer le screen…", "win.kill-screen")
        menu.append_section(None, section)
        return menu

    def _server_menu(self, server_id: str) -> Gio.Menu:
        target = GLib.Variant.new_string(server_id)

        def item(label, action):
            entry = Gio.MenuItem.new(label, None)
            entry.set_action_and_target_value(action, target)
            return entry

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append_item(item("Ouvrir des screens…", "win.open-server"))
        section.append_item(item("Nouveau screen…", "win.new-screen-on"))
        section.append_item(item("Ouvrir les screens favoris", "win.open-favorites"))
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append_item(item("Modifier…", "win.edit-server"))
        section.append_item(item("Dupliquer", "win.duplicate-server"))
        section.append_item(item("Copier la commande SSH", "win.copy-command"))
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append_item(item("Supprimer", "win.delete-server"))
        menu.append_section(None, section)
        return menu

    # ============================================================ actions
    def _setup_actions(self):
        simple = {
            "add-server": self.add_server,
            "quick-connect": self.quick_connect,
            "import-ssh-config": self.import_ssh_config,
            "new-screen": self.new_screen_current,
            "open-current": self.open_current_server,
            "close-tab": lambda: self._close_pane(self._target_pane()),
            "reconnect": lambda: self._target_pane() and self._target_pane().reconnect(),
            "rename-screen": lambda: self.rename_screen(self._target_pane()),
            "kill-screen": lambda: self.kill_screen(self._target_pane()),
            "move-to-window": lambda: self.move_to_new_window(self._target_pane()),
            "toggle-sidebar": lambda: self.split.set_show_sidebar(not self.split.get_show_sidebar()),
            "fullscreen": lambda: self.unfullscreen() if self.is_fullscreen() else self.fullscreen(),
            "copy": lambda: self._current_call("copy"),
            "paste": lambda: self._current_call("paste"),
            "zoom-in": lambda: self._current_call("zoom_in"),
            "zoom-out": lambda: self._current_call("zoom_out"),
            "zoom-reset": lambda: self._current_call("zoom_reset"),
        }
        for name, handler in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)

        with_id = {
            "open-server": self.open_server,
            "open-favorites": self.open_favorites,
            "new-screen-on": self.new_screen_on,
            "edit-server": self.edit_server,
            "duplicate-server": self.duplicate_server,
            "delete-server": self.delete_server,
            "copy-command": self.copy_command,
        }
        for name, handler in with_id.items():
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, param, h=handler: h(param.get_string()))
            self.add_action(action)

        reopen = Gio.SimpleAction.new("reopen", GLib.VariantType.new("(ss)"))
        reopen.connect("activate", self._on_reopen)
        self.add_action(reopen)

        self.mosaic_action = Gio.SimpleAction.new_stateful("mosaic", None, GLib.Variant.new_boolean(False))
        self.mosaic_action.connect("change-state", lambda a, v: self.set_mosaic(v.get_boolean()))
        self.add_action(self.mosaic_action)

        forget = Gio.SimpleAction.new("forget-skipped", GLib.VariantType.new("s"))
        forget.connect("activate", lambda _a, p: self.app.forget_skipped(p.get_string()))
        self.add_action(forget)

        self._pane_actions = [
            "close-tab", "reconnect", "rename-screen", "kill-screen", "move-to-window", "copy", "paste",
        ]

    def _current_call(self, method: str):
        pane = self.current_pane()
        if pane is not None:
            getattr(pane, method)()

    # ============================================================ serveurs
    def server_by_id(self, server_id: str) -> Server | None:
        return self.app.store.get(server_id) or self.ephemeral.get(server_id)

    def refresh_servers(self):
        for row in list(self._server_rows.values()):
            self.server_list.remove(row)
        self._server_rows.clear()
        servers = self.app.store.sorted()
        for server in servers:
            row = ServerRow(server, self._server_menu(server.id))
            self._server_rows[server.id] = row
            self.server_list.append(row)
        self.sidebar_stack.set_visible_child_name("list" if servers else "empty")

        child = self.recent_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.recent_list.remove(child)
            child = nxt
        recent = sorted(servers, key=lambda s: (-s.last_used, s.display_name.lower()))[:6]
        for server in recent:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(server.display_name),
                subtitle=GLib.markup_escape_text(server.address),
                activatable=True,
            )
            row.server_id = server.id
            row.add_prefix(
                Gtk.Label(
                    label=initials(server.display_name),
                    css_classes=["server-badge", server.color],
                    valign=Gtk.Align.CENTER,
                )
            )
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            self.recent_list.append(row)
        self.recent_title.set_visible(bool(recent))
        self.recent_list.set_visible(bool(recent))
        self._update_counts()

    def _filter_server_row(self, row: ServerRow) -> bool:
        query = self.search.get_text().strip().lower()
        if not query:
            return True
        server = row.server
        return any(query in field.lower() for field in (server.display_name, server.host, server.user))

    def _on_server_activated(self, _list, row: ServerRow):
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)
        self.open_server(row.server.id)

    def _on_recent_activated(self, _list, row):
        self.open_server(row.server_id)

    def with_secret(self, server: Server, callback, cancelled=None):
        """Récupère le secret d'un serveur (cache, trousseau ou saisie)."""
        errors = server.validate()
        if errors:
            # Profil invalide (fichier modifié à la main...) : on ne lance rien.
            self.toast(f"{server.display_name} : {errors[0]}")
            if cancelled:
                cancelled()
            return
        kind = server.secret_kind
        if kind is None:
            callback(None)
            return
        secret = self.secret_cache.get(server.id)
        if not secret and server.remember_secret:
            secret = self.app.vault.get(server.id, kind)
        if secret:
            callback(secret)
            return
        if kind == "passphrase":
            # ssh demandera la phrase de passe si besoin (fenêtre askpass).
            callback(None)
            return

        def got(value, remember):
            if value is None:
                if cancelled:
                    cancelled()
                return
            self.remember_secret(server, value, remember)
            callback(value)

        ask_password(self, server, got)

    def _store_secret(self, server: Server, secret: str) -> bool:
        ok = self.app.vault.set(server.id, server.secret_kind, secret, f"ScreenPort — {server.target}")
        if not ok:
            self.toast("Le trousseau a refusé l'enregistrement : secret gardé en mémoire pour cette session")
        return ok

    def remember_secret(self, server: Server, secret: str, remember: bool):
        self.secret_cache[server.id] = secret
        stored = self.app.store.get(server.id)
        if stored is None or not server.secret_kind:
            return
        if remember:
            self._store_secret(server, secret)
        else:
            # Ne pas laisser traîner un ancien secret (éventuellement refusé).
            self.app.vault.delete(server.id, server.secret_kind)
        if stored.remember_secret != remember:
            stored.remember_secret = remember
            self.app.store.upsert(stored)

    def add_server(self):
        ServerEditorDialog(self, on_done=self._on_server_saved).present(self)

    def quick_connect(self):
        def done(server: Server, secret: str | None, save: bool):
            if save:
                self._on_server_saved(server, secret, True)
            else:
                self.ephemeral[server.id] = server
                if secret:
                    self.secret_cache[server.id] = secret
            self.open_server(server.id)

        ServerEditorDialog(self, quick=True, on_done=done).present(self)

    def edit_server(self, server_id: str):
        server = self.server_by_id(server_id)
        if server is None:
            return
        ServerEditorDialog(self, server, on_done=self._on_server_saved).present(self)

    def _on_server_saved(self, server: Server, secret: str | None, save: bool = True):
        kind = server.secret_kind
        self.ephemeral.pop(server.id, None)
        self.app.store.upsert(server)
        ssh.clear_resolved_cache()
        for other in ("password", "passphrase"):
            if other != kind:
                self.app.vault.delete(server.id, other)
        if kind:
            if secret:
                self.secret_cache[server.id] = secret
                if server.remember_secret:
                    self._store_secret(server, secret)
                else:
                    self.app.vault.delete(server.id, kind)
            else:
                self.secret_cache.pop(server.id, None)
                self.app.vault.delete(server.id, kind)
        else:
            self.secret_cache.pop(server.id, None)
        for window in self.app.main_windows():
            for pane in window.panes():
                if pane.server.id == server.id:
                    pane.server = server
                    pane.secret = self.secret_cache.get(server.id)
                    pane.update_labels()
        self.app.refresh_servers()
        self.toast(f"Serveur « {server.display_name} » enregistré")

    def duplicate_server(self, server_id: str):
        server = self.server_by_id(server_id)
        if server is None:
            return
        copy = server.copy(id=uuid.uuid4().hex, name=f"{server.display_name} (copie)", last_used=0.0)
        self.app.store.upsert(copy)
        if server.secret_kind:
            secret = self.app.vault.get(server.id, server.secret_kind)
            if secret:
                self._store_secret(copy, secret)
        self.app.refresh_servers()
        self.edit_server(copy.id)

    def delete_server(self, server_id: str):
        server = self.server_by_id(server_id)
        if server is None:
            return

        def do_delete():
            self.app.store.remove(server.id)
            self.app.vault.delete(server.id)
            self.secret_cache.pop(server.id, None)
            self.app.refresh_servers()
            self.toast(f"Serveur « {server.display_name} » supprimé")

        confirm(
            self,
            f"Supprimer « {server.display_name} » ?",
            "Le serveur et son mot de passe enregistré seront retirés de ScreenPort. "
            "Les screens sur le serveur ne sont pas touchés.",
            "Supprimer",
            do_delete,
        )

    def copy_command(self, server_id: str):
        server = self.server_by_id(server_id)
        if server is None:
            return
        self.get_clipboard().set(ssh.command_preview(server))
        self.toast("Commande SSH copiée dans le presse-papiers")

    def import_ssh_config(self):
        from .import_dialog import ImportDialog

        ImportDialog(self).present(self)

    # ======================================================= ouverture
    def open_server(self, server_id: str, preset: list[str] | None = None):
        server = self.server_by_id(server_id)
        if server is None:
            return
        self.with_secret(server, lambda secret: OpenScreensDialog(self, server, secret, preset).present(self))

    def open_current_server(self):
        pane = self.current_pane()
        if pane is not None:
            self.open_server(pane.server.id)

    def open_favorites(self, server_id: str):
        server = self.server_by_id(server_id)
        if server is None:
            return
        names = server.favorite_screens or ["main"]
        self.with_secret(server, lambda secret: self.connect_and_open(server, names, secret))

    def new_screen_on(self, server_id: str):
        server = self.server_by_id(server_id)
        if server is None:
            return
        suggestion = self._suggest_name(server)

        def got(names: str):
            self.with_secret(
                server, lambda secret: self.connect_and_open(server, names.split(","), secret)
            )

        ask_screen_name(
            self,
            "Nouveau screen",
            f"Sur {server.display_name}. S'il existe déjà un screen de ce nom, "
            "ScreenPort s'y rattache. Séparez plusieurs noms par des virgules.",
            suggestion,
            got,
            action="Ouvrir",
        )

    def new_screen_current(self):
        pane = self.current_pane()
        if pane is not None:
            self.new_screen_on(pane.server.id)
            return
        servers = self.app.store.servers
        if len(servers) == 1:
            self.open_server(servers[0].id)
        elif not servers:
            self.add_server()
        else:
            self.split.set_show_sidebar(True)
            self.toast("Choisissez un serveur dans la liste")

    def _suggest_name(self, server: Server) -> str:
        used = self.open_screen_names(server)
        for candidate in [*server.favorite_screens, "main"]:
            if candidate not in used:
                return candidate
        index = 2
        while f"screen{index}" in used:
            index += 1
        return f"screen{index}"

    def connect_and_open(self, server: Server, names: list[str], secret: str | None, mosaic=None, done=None):
        """Établit la connexion (partagée) puis ouvre les terminaux."""
        names = [n for n in names if n == ssh.SHELL_ONLY or is_valid_screen_name(n)]
        if not names:
            if done:
                done()
            return
        already = all(self._find_pane(server.id, n) for n in names)
        if already or not self.app.settings.prefs.multiplex or len(names) == 1:
            self.open_sessions(server, names, secret, mosaic=mosaic)
            if done:
                done()
            return
        toast = Adw.Toast(title=GLib.markup_escape_text(f"Connexion à {server.display_name}…"), timeout=0)
        self.toasts.add_toast(toast)

        def ready(result: remote.RemoteResult, _listing):
            toast.dismiss()
            self.open_sessions(server, names, secret, mosaic=mosaic)
            if done:
                done()

        remote.list_screens(server, self.app.settings.prefs, secret, ready)

    def open_sessions(self, server: Server, names: list[str], secret: str | None, mosaic=None):
        last = None
        for name in names:
            pane = self._find_pane(server.id, name)
            if pane is None:
                pane = self._add_pane(server, name, secret)
            last = pane
        if last is not None:
            self.tab_view.set_selected_page(last.page)
        stored = self.app.store.get(server.id)
        if stored is not None:
            stored.last_used = time.time()
            self.app.store.upsert(stored)
        if mosaic is not None and mosaic != self._mosaic and len(self.panes()) > 1:
            self.set_mosaic(mosaic)
        elif last is not None:
            self._focus_pane(last)

    def _add_pane(self, server: Server, name: str, secret: str | None) -> TerminalPane:
        pane = TerminalPane(self.app, server, name, secret)
        slot = PaneSlot(pane)
        pane.slot = slot
        pane.page = self.tab_view.append(slot)
        pane.page.set_icon(self._color_icon(server.color))
        pane.page.set_indicator_tooltip("Déconnecté — cliquer pour se reconnecter")
        self._refresh_tabs()
        if self._mosaic:
            self._rebuild_mosaic()
        pane.connect_session()
        return pane

    def on_screen_killed(self, server: Server, name: str):
        pane = self._find_pane(server.id, name)
        if pane is not None:
            pane.close()
            self.tab_view.close_page(pane.page)

    # ========================================================= onglets
    def panes(self) -> list[TerminalPane]:
        result = []
        for index in range(self.tab_view.get_n_pages()):
            page = self.tab_view.get_nth_page(index)
            slot = page.get_child()
            if isinstance(slot, PaneSlot):
                # « page-attached » est émis avant le retour de append() :
                # on relie ici le terminal à son onglet.
                slot.pane.page = page
                result.append(slot.pane)
        return result

    def current_pane(self) -> TerminalPane | None:
        if self._mosaic and self._focused_pane in self.panes():
            return self._focused_pane
        page = self.tab_view.get_selected_page()
        if page is not None and isinstance(page.get_child(), PaneSlot):
            return page.get_child().pane
        return None

    def _target_pane(self) -> TerminalPane | None:
        if self._menu_page is not None and isinstance(self._menu_page.get_child(), PaneSlot):
            return self._menu_page.get_child().pane
        return self.current_pane()

    def _find_pane(self, server_id: str, name: str) -> TerminalPane | None:
        return next(
            (p for p in self.panes() if p.server.id == server_id and p.screen_name == name), None
        )

    def open_screen_names(self, server: Server) -> set[str]:
        return {p.screen_name for p in self.panes() if p.server.id == server.id}

    @staticmethod
    def is_valid_name(name: str) -> bool:
        return is_valid_screen_name(name)

    def is_mosaic(self) -> bool:
        return self._mosaic

    def _color_icon(self, color: str) -> Gio.Icon:
        if color not in self._icons:
            hex_color = COLORS.get(color, COLORS["blue"])
            svg = (
                "<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'>"
                f"<circle cx='8' cy='8' r='5' fill='{hex_color}'/></svg>"
            )
            self._icons[color] = Gio.BytesIcon.new(GLib.Bytes.new(svg.encode()))
        return self._icons[color]

    def _refresh_tabs(self):
        panes = self.panes()
        several_servers = len({p.server.id for p in panes}) > 1
        for pane in panes:
            title = pane.title
            if several_servers:
                title = f"{pane.title} · {pane.server.display_name}"
            pane.page.set_title(title)
            pane.page.set_tooltip(
                GLib.markup_escape_text(f"{pane.title} — {pane.server.display_name} ({pane.server.address})")
            )
            pane.page.set_icon(self._color_icon(pane.server.color))
        self._update_title()

    def _on_page_attached(self, _view, page, _position):
        slot = page.get_child()
        if isinstance(slot, PaneSlot):
            pane = slot.pane
            pane.page = page
            # Les signaux sont reliés à la fenêtre qui contient l'onglet : un
            # onglet déplacé vers une autre fenêtre change de propriétaire.
            pane.window_handlers = [
                pane.connect("state-changed", self._on_pane_state),
                pane.connect("close-request", self._close_pane),
                pane.connect("focus-in", self._on_pane_focus),
                pane.connect("maximize-request", self._on_pane_maximize),
                pane.connect("bell", self._on_pane_bell),
            ]
            pane.update_labels()
            self._on_pane_state(pane)
        self._on_pages_changed()

    def _on_page_detached(self, _view, page, _position):
        slot = page.get_child()
        if isinstance(slot, PaneSlot):
            pane = slot.pane
            for handler in getattr(pane, "window_handlers", []):
                pane.disconnect(handler)
            pane.window_handlers = []
            if self._focused_pane is pane:
                self._focused_pane = None
            # Onglet emporté depuis la mosaïque : on remet le terminal dans son onglet.
            parent = pane.get_parent()
            if parent is not None and parent is not slot:
                parent.remove(pane)
                pane.set_mosaic(False)
                slot.set_child(pane)
        self._on_pages_changed()

    def _on_create_window(self, _view):
        """Onglet glissé hors de la fenêtre : il part dans une nouvelle fenêtre."""
        window = MainWindow(self.app)
        window.present()
        return window.tab_view

    def move_to_new_window(self, pane: TerminalPane | None):
        if pane is None:
            return
        window = MainWindow(self.app)
        window.present()
        self.tab_view.transfer_page(pane.page, window.tab_view, 0)

    def _on_pages_changed(self):
        panes = self.panes()
        has_panes = bool(panes)
        self.tab_bar.set_visible(has_panes)
        if not has_panes and self._mosaic:
            self.set_mosaic(False)
        if self._mosaic:
            self._rebuild_mosaic()
        self.stack.set_visible_child_name("mosaic" if self._mosaic else ("tabs" if has_panes else "welcome"))
        for name in self._pane_actions:
            self.lookup_action(name).set_enabled(has_panes)
        self.mosaic_action.set_enabled(len(panes) > 1 or self._mosaic)
        self._update_counts()
        self._refresh_tabs()
        self.schedule_save()

    def schedule_save(self):
        """Mémorise les onglets ouverts peu après chaque changement."""
        if self._closing or self._save_source or self._restoring:
            return

        def save():
            self._save_source = 0
            if not self._closing:
                self.save_state()
            return GLib.SOURCE_REMOVE

        self._save_source = GLib.timeout_add(800, save)

    def _update_counts(self):
        # Compteur global : un onglet déplacé dans une autre fenêtre compte toujours.
        counts: dict[str, int] = {}
        windows = self.app.main_windows() or [self]
        for window in windows:
            for pane in window.panes():
                counts[pane.server.id] = counts.get(pane.server.id, 0) + 1
        for window in windows:
            for server_id, row in window._server_rows.items():
                row.set_count(counts.get(server_id, 0))

    def _update_title(self):
        pane = self.current_pane()
        if self._mosaic:
            count = len(self.panes())
            self.window_title.set_title("Mosaïque")
            self.window_title.set_subtitle(f"{count} terminaux")
            self.set_title("Mosaïque — ScreenPort")
        elif pane is not None:
            self.window_title.set_title(pane.title)
            self.window_title.set_subtitle(f"{pane.server.display_name} · {pane.server.address}")
            self.set_title(f"{pane.title} — ScreenPort")
        else:
            self.window_title.set_title("ScreenPort")
            self.window_title.set_subtitle("")
            self.set_title("ScreenPort")

    def _on_selected_page(self, *_):
        page = self.tab_view.get_selected_page()
        if page is not None:
            page.set_needs_attention(False)
            if isinstance(page.get_child(), PaneSlot):
                pane = page.get_child().pane
                self._focused_pane = pane
                self._focus_pane(pane)
        self._update_title()

    def _focus_pane(self, pane: TerminalPane):
        def grab():
            pane.terminal.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(grab)

    def _on_pane_focus(self, pane: TerminalPane):
        self._focused_pane = pane
        if self._mosaic and self.tab_view.get_selected_page() is not pane.page:
            self.tab_view.set_selected_page(pane.page)
        pane.page.set_needs_attention(False)
        self._update_title()

    def _on_pane_state(self, pane: TerminalPane):
        page = pane.page
        page.set_loading(pane.state == CONNECTING)
        if pane.state == CLOSED:
            page.set_indicator_icon(Gio.ThemedIcon.new("network-offline-symbolic"))
            page.set_indicator_activatable(True)
        else:
            page.set_indicator_icon(None)

    def _on_indicator_activated(self, _view, page: Adw.TabPage):
        if isinstance(page.get_child(), PaneSlot):
            page.get_child().pane.reconnect()

    def _on_pane_bell(self, pane: TerminalPane):
        if pane is not self.current_pane():
            pane.page.set_needs_attention(True)

    def _on_pane_maximize(self, pane: TerminalPane):
        self.set_mosaic(False)
        self.tab_view.set_selected_page(pane.page)
        self._focus_pane(pane)

    def _on_setup_menu(self, _view, page):
        if page is not None:
            self._menu_page = page
            shell = isinstance(page.get_child(), PaneSlot) and page.get_child().pane.screen_name == ssh.SHELL_ONLY
            self.lookup_action("rename-screen").set_enabled(not shell)
            self.lookup_action("kill-screen").set_enabled(not shell)
        else:
            def reset():
                self._menu_page = None
                has = bool(self.panes())
                self.lookup_action("rename-screen").set_enabled(has)
                self.lookup_action("kill-screen").set_enabled(has)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(reset)

    def _close_pane(self, pane: TerminalPane | None):
        if pane is not None:
            self.tab_view.close_page(pane.page)

    def _on_close_page(self, view, page):
        slot = page.get_child()
        if isinstance(slot, PaneSlot):
            pane = slot.pane
            was_connected = pane.child_pid is not None
            pane.close()
            if was_connected and pane.screen_name != ssh.SHELL_ONLY and not self._force_close:
                self._show_close_toast(pane)
        view.close_page_finish(page, True)
        return True

    def _show_close_toast(self, pane: TerminalPane):
        if self._close_toast is not None:
            self._close_toast.dismiss()
        toast = Adw.Toast(
            title=GLib.markup_escape_text(
                f"« {pane.title} » fermé · le screen continue sur {pane.server.display_name}"
            ),
            button_label="Rouvrir",
            action_name="win.reopen",
            action_target=GLib.Variant("(ss)", (pane.server.id, pane.screen_name)),
            timeout=5,
        )
        toast.connect("dismissed", lambda t: setattr(self, "_close_toast", None) if self._close_toast is t else None)
        self._close_toast = toast
        self.toasts.add_toast(toast)

    def _on_reopen(self, _action, param):
        server_id, name = param.unpack()
        server = self.server_by_id(server_id)
        if server is not None:
            self.with_secret(server, lambda secret: self.open_sessions(server, [name], secret))

    def rename_screen(self, pane: TerminalPane | None):
        if pane is None or pane.screen_name == ssh.SHELL_ONLY:
            return
        server = pane.server
        old = pane.screen_name

        def got(names: str):
            new = names.split(",")[0]
            if new == old:
                return
            if self._find_pane(server.id, new):
                self.toast(f"Un terminal « {new} » est déjà ouvert sur ce serveur")
                return

            def done(result: remote.RemoteResult):
                if result.ok:
                    pane.screen_name = new
                    pane.update_labels()
                    self._refresh_tabs()
                    self.schedule_save()
                    self.toast(f"Screen renommé en « {new} »")
                elif result.exit_code == 3:
                    self.toast(f"Le screen « {old} » n'existe plus sur le serveur")
                elif result.exit_code == 4:
                    self.toast(f"Un screen « {new} » existe déjà sur le serveur")
                else:
                    self.toast(f"Échec du renommage : {result.error().title}")

            self.with_secret(
                server,
                lambda secret: remote.run_remote(
                    server, self.app.settings.prefs, secret, ssh.remote_rename_command(old, new), done
                ),
            )

        ask_screen_name(self, "Renommer le screen", f"Nouveau nom pour « {old} » :", old, got, action="Renommer")

    def kill_screen(self, pane: TerminalPane | None):
        if pane is None or pane.screen_name == ssh.SHELL_ONLY:
            return
        server = pane.server
        name = pane.screen_name

        def do_kill():
            pane.close()
            self.tab_view.close_page(pane.page)

            def done(result: remote.RemoteResult):
                if result.ok:
                    self.toast(f"Screen « {name} » terminé")
                elif result.exit_code == 3:
                    self.toast(f"Le screen « {name} » n'existait plus")
                else:
                    self.toast(f"Impossible de terminer « {name} » : {result.error().title}")

            self.with_secret(
                server,
                lambda secret: remote.run_remote(
                    server, self.app.settings.prefs, secret, ssh.remote_kill_command(name), done
                ),
            )

        confirm_kill(self, name, server, do_kill)

    # ========================================================= mosaïque
    def set_mosaic(self, enabled: bool):
        enabled = bool(enabled) and bool(self.panes())
        if enabled == self._mosaic:
            self.mosaic_action.set_state(GLib.Variant.new_boolean(enabled))
            return
        current = self.current_pane()
        self._mosaic = enabled
        self.mosaic_action.set_state(GLib.Variant.new_boolean(enabled))
        if enabled:
            self._rebuild_mosaic()
            self.stack.set_visible_child_name("mosaic")
        else:
            self._leave_mosaic()
            self.stack.set_visible_child_name("tabs" if self.panes() else "welcome")
        self._on_pages_changed()
        if current is not None:
            if not enabled:
                self.tab_view.set_selected_page(current.page)
            self._focus_pane(current)

    def _clear_mosaic(self):
        row = self.mosaic.get_first_child()
        while row is not None:
            next_row = row.get_next_sibling()
            child = row.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                row.remove(child)
                child = next_child
            self.mosaic.remove(row)
            row = next_row

    def _rebuild_mosaic(self):
        self._clear_mosaic()
        panes = self.panes()
        for pane in panes:
            if pane.get_parent() is pane.slot:
                pane.slot.set_child(None)
            pane.set_mosaic(True)
        count = len(panes)
        if count == 0:
            return
        columns = math.ceil(math.sqrt(count))
        rows = math.ceil(count / columns)
        for r in range(rows):
            row = Gtk.Box(spacing=8, homogeneous=True)
            for pane in panes[r * columns:(r + 1) * columns]:
                row.append(pane)
            self.mosaic.append(row)

    def _leave_mosaic(self):
        self._clear_mosaic()
        for pane in self.panes():
            pane.set_mosaic(False)
            if pane.get_parent() is None:
                pane.slot.set_child(pane)

    # ======================================================== réglages
    def apply_prefs(self):
        for pane in self.panes():
            pane.apply_prefs()

    def toast(self, message: str, timeout: int = 3):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message), timeout=timeout))

    # ===================================================== restauration
    def restore_sessions(self, entries: list[dict], selected_key: dict | None = None, mosaic: bool = False):
        self._restoring = True
        self._restore_entries = [dict(e) for e in entries]
        groups: OrderedDict[str, list[str]] = OrderedDict()
        for entry in entries:
            groups.setdefault(entry["server"], [])
            if entry["screen"] not in groups[entry["server"]]:
                groups[entry["server"]].append(entry["screen"])

        def finish():
            self._restoring = False
            self._restore_entries = []
            # Les onglets ont été ouverts serveur par serveur : on rétablit
            # l'ordre d'origine puis l'onglet qui était sélectionné.
            ordered = [self._find_pane(e["server"], e["screen"]) for e in entries]
            ordered = [p for p in ordered if p is not None]
            for position, pane in enumerate(ordered):
                self.tab_view.reorder_page(pane.page, position)
            target = None
            if selected_key:
                target = self._find_pane(selected_key.get("server", ""), selected_key.get("screen", ""))
            if target is None and ordered:
                target = ordered[0]
            if target is not None:
                self.tab_view.set_selected_page(target.page)
            if mosaic and len(self.panes()) > 1:
                self.set_mosaic(True)
            self.schedule_save()

        def skip(server: Server, names: list[str]):
            # Mot de passe refusé/annulé : ces onglets restent mémorisés pour
            # le prochain lancement, sauf si l'utilisateur demande de les oublier.
            self.app.skip_sessions(server.id, names)
            self.toasts.add_toast(
                Adw.Toast(
                    title=GLib.markup_escape_text(
                        f"Onglets de « {server.display_name} » non rouverts"
                    ),
                    button_label="Oublier",
                    action_name="win.forget-skipped",
                    action_target=GLib.Variant.new_string(server.id),
                    timeout=8,
                )
            )
            next_server()

        def next_server():
            while groups:
                server_id, names = groups.popitem(last=False)
                server = self.app.store.get(server_id)
                if server is None:
                    continue
                self.with_secret(
                    server,
                    lambda secret, s=server, n=names: self.connect_and_open(s, n, secret, done=next_server),
                    cancelled=lambda s=server, n=names: skip(s, n),
                )
                return
            finish()

        next_server()

    def save_state(self):
        """Mémorise la fenêtre et les onglets de toutes les fenêtres ouvertes."""
        state = self.app.settings.window
        if not self.is_maximized() and not self.is_fullscreen():
            width, height = self.get_default_size()
            state.width, state.height = width, height
        state.maximized = self.is_maximized()
        state.sidebar_visible = self.split.get_show_sidebar()
        state.mosaic = self._mosaic
        current = self.current_pane()
        state.selected_key = (
            {"server": current.server.id, "screen": current.screen_name} if current else {}
        )
        sessions: list[dict] = []
        restoring = False
        for window in [self, *(w for w in self.app.main_windows() if w is not self)]:
            if window._closing and window is not self:
                continue
            if window._restoring:
                # Restauration en cours (mot de passe demandé...) : on garde la
                # liste complète prévue plutôt que les seuls onglets déjà ouverts.
                restoring = True
                sessions += [e for e in window._restore_entries if e not in sessions]
            for pane in window.panes():
                entry = {"server": pane.server.id, "screen": pane.screen_name}
                if self.app.store.get(pane.server.id) is not None and entry not in sessions:
                    sessions.append(entry)
        for entry in self.app.skipped_sessions:
            if entry not in sessions:
                sessions.append(entry)
        if restoring and not current:
            state.selected_key = {}
        state.sessions = sessions
        self.app.settings.save()

    def _on_close_request(self, *_):
        panes = self.panes()
        others = [w for w in self.app.main_windows() if w is not self and not w._closing]
        if panes and self.app.settings.prefs.confirm_close and not self._force_close:
            count = len(panes)
            restore = self.app.settings.prefs.restore_sessions and not others
            body = (
                f"{count} terminal ouvert. " if count == 1 else f"{count} terminaux ouverts. "
            ) + "Les screens continueront de tourner sur les serveurs"
            body += " et seront rouverts au prochain lancement." if restore else "."

            def really_close():
                self._force_close = True
                self.close()

            heading = "Fermer cette fenêtre ?" if others else "Fermer ScreenPort ?"
            confirm(self, heading, body, "Fermer", really_close, destructive=False)
            return True
        if others:
            # Une autre fenêtre reste ouverte : elle mémorisera l'état.
            self.shutdown(save=False)
            others[0].schedule_save()
        else:
            self.shutdown()
        return False

    def shutdown(self, save: bool = True):
        """Sauvegarde l'état puis ferme proprement les connexions."""
        if self._closing:
            return
        if save:
            self.save_state()
        self._closing = True
        self._force_close = True
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_source = 0
        for pane in self.panes():
            pane.close()
