"""Dialogues : édition d'un serveur, ouverture de screens, mot de passe..."""

from __future__ import annotations

import os
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from . import remote, ssh
from .models import (
    AUTH_AGENT,
    AUTH_KEY,
    AUTH_METHODS,
    AUTH_PASSWORD,
    COLORS,
    Server,
    initials,
    parse_address,
    parse_screen_list_input,
    sanitize_screen_name,
)

_AUTH_LABELS = {
    AUTH_PASSWORD: "Mot de passe",
    AUTH_KEY: "Clé privée (fichier)",
    AUTH_AGENT: "Agent SSH / clés par défaut",
}
_COLOR_LABELS = {
    "blue": "Bleu",
    "teal": "Turquoise",
    "green": "Vert",
    "yellow": "Jaune",
    "orange": "Orange",
    "red": "Rouge",
    "pink": "Rose",
    "purple": "Violet",
    "slate": "Ardoise",
}


# --------------------------------------------------------------------------
# Édition / ajout d'un serveur
# --------------------------------------------------------------------------
class ServerEditorDialog(Adw.Dialog):
    """Formulaire d'ajout, de modification ou de connexion rapide."""

    def __init__(
        self,
        window,
        server: Server | None = None,
        *,
        quick: bool = False,
        on_done: Callable[[Server, str | None, bool], None] | None = None,
    ):
        super().__init__()
        self.window = window
        self.app = window.app
        self.quick = quick
        self.creating = server is None
        self.server = server.copy() if server else Server()
        self.on_done = on_done
        self._test_handle = None
        self._secret_loaded = False

        if quick:
            title = "Connexion rapide"
        elif self.creating:
            title = "Ajouter un serveur"
        else:
            title = "Modifier le serveur"
        self.set_title(title)
        self.set_content_width(560)
        self.set_content_height(720)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Annuler")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_button = Gtk.Button(
            label="Connecter" if quick else "Enregistrer", css_classes=["suggested-action"]
        )
        self.save_button.connect("clicked", self._on_save)
        header.pack_end(self.save_button)
        toolbar.add_top_bar(header)

        self.toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)
        toolbar.set_content(self.toasts)
        self.set_child(toolbar)

        # -- Serveur ----------------------------------------------------
        group = Adw.PreferencesGroup(
            title="Serveur",
            description="Astuce : vous pouvez coller « utilisateur@hôte:port » dans le champ Adresse.",
        )
        self.host_row = Adw.EntryRow(title="Adresse (nom d'hôte ou IP)")
        self.host_row.set_text(self.server.host)
        self.host_row.connect("changed", lambda *_: self._validate())
        self.host_row.connect("entry-activated", self._normalize_host)
        host_focus = Gtk.EventControllerFocus()
        host_focus.connect("leave", self._normalize_host)
        self.host_row.add_controller(host_focus)
        group.add(self.host_row)

        self.user_row = Adw.EntryRow(title="Utilisateur")
        self.user_row.set_text(self.server.user or ("" if not self.creating else os.environ.get("USER", "")))
        self.user_row.connect("changed", lambda *_: self._validate())
        group.add(self.user_row)

        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title("Port")
        self.port_row.set_value(self.server.port or 22)
        group.add(self.port_row)

        self.name_row = Adw.EntryRow(title="Nom affiché (facultatif)")
        self.name_row.set_text(self.server.name)
        group.add(self.name_row)
        page.add(group)

        # -- Authentification -------------------------------------------
        auth_group = Adw.PreferencesGroup(title="Authentification")
        self.auth_row = Adw.ComboRow(title="Méthode")
        self.auth_row.set_model(Gtk.StringList.new([_AUTH_LABELS[m] for m in AUTH_METHODS]))
        self.auth_row.set_selected(AUTH_METHODS.index(self.server.auth))
        self.auth_row.connect("notify::selected", lambda *_: self._update_auth_rows())
        auth_group.add(self.auth_row)

        self.password_row = Adw.PasswordEntryRow(title="Mot de passe")
        self.password_row.connect("changed", lambda *_: self._validate())
        auth_group.add(self.password_row)

        self.key_row = Adw.ActionRow(title="Fichier de clé privée", subtitle_lines=1)
        self.key_row.set_subtitle(self.server.key_path or "Aucun fichier choisi")
        choose = Gtk.Button(
            icon_name="document-open-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Choisir la clé…",
            css_classes=["flat"],
        )
        choose.connect("clicked", self._choose_key)
        self.key_row.add_suffix(choose)
        self.key_row.set_activatable_widget(choose)
        auth_group.add(self.key_row)
        self._key_path = self.server.key_path

        self.passphrase_row = Adw.PasswordEntryRow(title="Phrase de passe de la clé (facultative)")
        auth_group.add(self.passphrase_row)

        self.remember_row = Adw.SwitchRow(
            title="Mémoriser le secret",
            subtitle=f"Stocké de façon sécurisée ({self.app.vault.backend_label})",
        )
        self.remember_row.set_active(self.server.remember_secret)
        auth_group.add(self.remember_row)

        self.agent_info = Adw.ActionRow(
            title="Clés de ~/.ssh et agent SSH",
            subtitle="Aucun secret à saisir : ssh utilise vos clés habituelles.",
            icon_name="channel-secure-symbolic",
        )
        auth_group.add(self.agent_info)
        page.add(auth_group)

        # -- Screens & apparence ----------------------------------------
        extra = Adw.PreferencesGroup(title="Screens et apparence")
        self.favorites_row = Adw.EntryRow(title="Screens favoris (séparés par des virgules)")
        self.favorites_row.set_text(", ".join(self.server.favorite_screens))
        extra.add(self.favorites_row)

        color_row = Adw.ActionRow(title="Couleur")
        color_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.color_buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for color in COLORS:
            button = Gtk.ToggleButton(
                css_classes=["color-choice", color],
                tooltip_text=_COLOR_LABELS[color],
                valign=Gtk.Align.CENTER,
            )
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(color == self.server.color)
            self.color_buttons[color] = button
            color_box.append(button)
        color_row.add_suffix(color_box)
        extra.add(color_row)

        if quick:
            self.save_switch = Adw.SwitchRow(
                title="Enregistrer ce serveur",
                subtitle="L'ajouter à la liste pour s'y reconnecter en un clic",
            )
            self.save_switch.set_active(True)
            extra.add(self.save_switch)
        else:
            self.save_switch = None
        page.add(extra)

        # -- Test -------------------------------------------------------
        test_group = Adw.PreferencesGroup()
        self.test_row = Adw.ActionRow(
            title="Tester la connexion",
            subtitle="Vérifie l'accès et compte les screens existants",
            activatable=True,
        )
        self.test_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.test_row.add_suffix(self.test_spinner)
        self.test_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self.test_row.connect("activated", self._on_test)
        test_group.add(self.test_row)
        page.add(test_group)

        self.connect("closed", self._on_closed)
        self._load_secret()
        self._update_auth_rows()
        self._validate()
        GLib.idle_add(self._focus_first)

    def _focus_first(self):
        self.host_row.grab_focus()
        return GLib.SOURCE_REMOVE

    # -- comportement -----------------------------------------------------
    def _load_secret(self):
        if self.creating:
            return
        kind = self.server.secret_kind
        if kind:
            secret = self.app.vault.get(self.server.id, kind)
            if secret:
                row = self.password_row if kind == "password" else self.passphrase_row
                row.set_text(secret)
                self._secret_loaded = True

    def _normalize_host(self, *_):
        """Répartit « utilisateur@hôte:port » dans les champs correspondants."""
        text = self.host_row.get_text().strip()
        user, host, port = parse_address(text)
        if host and host != text:
            if user:
                self.user_row.set_text(user)
            if port:
                self.port_row.set_value(port)
            self.host_row.set_text(host)
            self.host_row.set_position(-1)
        self._validate()

    def _auth(self) -> str:
        return AUTH_METHODS[self.auth_row.get_selected()]

    def _update_auth_rows(self):
        auth = self._auth()
        self.password_row.set_visible(auth == AUTH_PASSWORD)
        self.key_row.set_visible(auth == AUTH_KEY)
        self.passphrase_row.set_visible(auth == AUTH_KEY)
        self.remember_row.set_visible(auth != AUTH_AGENT)
        self.remember_row.set_title(
            "Mémoriser le mot de passe" if auth == AUTH_PASSWORD else "Mémoriser la phrase de passe"
        )
        self.agent_info.set_visible(auth == AUTH_AGENT)
        self._validate()

    def _choose_key(self, *_):
        dialog = Gtk.FileDialog(title="Choisir une clé privée", modal=True)
        ssh_dir = os.path.expanduser("~/.ssh")
        if os.path.isdir(ssh_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))

        def done(dlg, result):
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            if file and file.get_path():
                path = file.get_path()
                home = os.path.expanduser("~")
                if path.startswith(home + os.sep):
                    path = "~" + path[len(home):]
                self._key_path = path
                self.key_row.set_subtitle(path)
                self._validate()

        dialog.open(self.window, None, done)

    def _collect(self) -> Server:
        server = self.server.copy()
        user, host, port = parse_address(self.host_row.get_text())
        server.host = host
        server.user = user or self.user_row.get_text().strip()
        server.port = port or int(self.port_row.get_value())
        server.name = self.name_row.get_text().strip()
        server.auth = self._auth()
        server.key_path = self._key_path.strip() if server.auth == AUTH_KEY else ""
        server.remember_secret = self.remember_row.get_active()
        server.favorite_screens = parse_screen_list_input(self.favorites_row.get_text())
        server.color = next((c for c, b in self.color_buttons.items() if b.get_active()), "blue")
        return server

    def _secret(self, server: Server) -> str | None:
        if server.auth == AUTH_PASSWORD:
            return self.password_row.get_text() or None
        if server.auth == AUTH_KEY:
            return self.passphrase_row.get_text() or None
        return None

    def _validate(self) -> bool:
        server = self._collect()
        errors = server.validate()
        self.save_button.set_sensitive(not errors)
        self.test_row.set_sensitive(not errors)
        return not errors

    def _on_save(self, *_):
        self._normalize_host()
        server = self._collect()
        errors = server.validate()
        if errors:
            self.toasts.add_toast(Adw.Toast(title=errors[0]))
            return
        secret = self._secret(server)
        save = self.save_switch.get_active() if self.save_switch is not None else True
        if self.on_done:
            self.on_done(server, secret, save)
        self.close()

    def _on_test(self, *_):
        if self._test_handle is not None:
            return
        server = self._collect()
        if server.validate():
            return
        self.test_spinner.set_spinning(True)
        self.test_row.set_subtitle(f"Connexion à {server.address}…")

        def done(result: remote.RemoteResult, listing: ssh.ScreenListing):
            self._test_handle = None
            self.test_spinner.set_spinning(False)
            if result.cancelled:
                return
            if result.ok and listing.ok:
                if not listing.has_screen:
                    msg = "Connexion réussie, mais GNU screen n'est pas installé sur le serveur."
                else:
                    count = len(listing.sessions)
                    msg = (
                        "Connexion réussie — aucun screen en cours"
                        if count == 0
                        else f"Connexion réussie — {count} screen{'s' if count > 1 else ''} en cours"
                    )
                self.test_row.set_subtitle("✔ " + msg)
            else:
                err = result.error()
                self.test_row.set_subtitle(f"✖ {err.title} : {err.detail}")

        self._test_handle = remote.list_screens(
            server, self.app.settings.prefs, self._secret(server), done
        )

    def _on_closed(self, *_):
        if self._test_handle is not None:
            self._test_handle.cancel()


# --------------------------------------------------------------------------
# Ouverture de screens sur un serveur
# --------------------------------------------------------------------------
class _ScreenChoice:
    def __init__(self, name: str, row: Adw.ActionRow, check: Gtk.CheckButton):
        self.name = name
        self.row = row
        self.check = check


class OpenScreensDialog(Adw.Dialog):
    """Liste les screens existants et permet d'en ouvrir/créer plusieurs."""

    def __init__(self, window, server: Server, secret: str | None, preset: list[str] | None = None):
        super().__init__(title="Ouvrir des screens")
        self.window = window
        self.app = window.app
        self.server = server
        self.secret = secret
        self._handle = None
        self.existing: dict[str, _ScreenChoice] = {}
        self.new: dict[str, _ScreenChoice] = {}
        self.listing: ssh.ScreenListing | None = None
        self.set_content_width(560)
        self.set_content_height(700)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Ouvrir des screens", subtitle=server.display_name))
        self.refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Actualiser la liste des screens"
        )
        self.refresh_button.connect("clicked", lambda *_: self.refresh())
        header.pack_start(self.refresh_button)
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Modifier le serveur")
        edit.connect("clicked", self._on_edit)
        header.pack_end(edit)
        toolbar.add_top_bar(header)

        self.toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)
        toolbar.set_content(self.toasts)

        # Résumé du serveur
        summary = Adw.PreferencesGroup()
        self.summary_row = Adw.ActionRow(title=server.address, subtitle="Connexion…")
        badge = Gtk.Label(
            label=initials(server.display_name),
            css_classes=["server-badge", server.color],
            valign=Gtk.Align.CENTER,
        )
        self.summary_row.add_prefix(badge)
        self.summary_spinner = Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER)
        self.summary_row.add_suffix(self.summary_spinner)
        summary.add(self.summary_row)
        page.add(summary)

        # Erreur
        self.error_group = Adw.PreferencesGroup(visible=False)
        self.error_row = Adw.ActionRow(icon_name="dialog-error-symbolic", css_classes=["error-row"])
        self.error_row.set_title_lines(2)
        self.error_row.set_subtitle_lines(6)
        retry = Gtk.Button(label="Réessayer", valign=Gtk.Align.CENTER, css_classes=["pill"])
        retry.connect("clicked", lambda *_: self.refresh())
        self.error_row.add_suffix(retry)
        self.error_group.add(self.error_row)
        self.password_button = Adw.ButtonRow(title="Saisir le mot de passe…") if hasattr(Adw, "ButtonRow") else None
        if self.password_button is None:
            self.password_button = Adw.ActionRow(title="Saisir le mot de passe…", activatable=True)
            self.password_button.add_suffix(Gtk.Image.new_from_icon_name("dialog-password-symbolic"))
        self.password_button.connect("activated", self._on_enter_password)
        self.password_button.set_visible(False)
        self.error_group.add(self.password_button)
        self.terminal_row = Adw.ActionRow(
            title="Ouvrir quand même dans un terminal",
            subtitle="Pour voir les messages de ssh ou répondre à une question",
            activatable=True,
        )
        self.terminal_row.add_suffix(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        self.terminal_row.connect("activated", lambda *_: self._open(force=True))
        self.error_group.add(self.terminal_row)
        page.add(self.error_group)

        # Screen absent
        self.noscreen_group = Adw.PreferencesGroup(visible=False)
        noscreen = Adw.ActionRow(
            title="GNU screen n'est pas installé sur ce serveur",
            subtitle="Installez-le (ex. : sudo apt install screen) ou ouvrez un simple shell.",
            icon_name="dialog-warning-symbolic",
        )
        shell = Gtk.Button(label="Ouvrir un shell", valign=Gtk.Align.CENTER, css_classes=["pill"])
        shell.connect("clicked", self._on_open_shell)
        noscreen.add_suffix(shell)
        self.noscreen_group.add(noscreen)
        page.add(self.noscreen_group)

        # Nouveaux screens
        self.new_group = Adw.PreferencesGroup(
            title="Nouveaux screens",
            description="Donnez un nom à chaque terminal. Si un screen porte déjà ce nom, "
            "ScreenPort s'y rattache au lieu d'en créer un nouveau.",
        )
        self.name_row = Adw.EntryRow(title="Nom du screen (puis Entrée)", show_apply_button=True)
        self.name_row.connect("apply", self._on_add_name)
        self.name_row.connect("entry-activated", self._on_add_name)
        self.name_row.connect("changed", self._on_name_changed)
        self.new_group.add(self.name_row)
        page.add(self.new_group)

        # Screens existants
        self.existing_group = Adw.PreferencesGroup(
            title="Screens en cours sur le serveur",
            description="Cochez ceux auxquels vous voulez vous rattacher.",
        )
        self.loading_row = Adw.ActionRow(title="Recherche des screens…")
        self.loading_row.add_suffix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
        self.existing_group.add(self.loading_row)
        self.empty_row = Adw.ActionRow(
            title="Aucun screen en cours",
            subtitle="Créez-en un ci-dessus.",
            icon_name="utilities-terminal-symbolic",
            visible=False,
        )
        self.existing_group.add(self.empty_row)
        page.add(self.existing_group)

        # Barre du bas
        bottom = Gtk.Box(spacing=12, css_classes=["dialog-bottom-bar"])
        self.mosaic_check = Gtk.CheckButton(label="Afficher en mosaïque", valign=Gtk.Align.CENTER)
        self.mosaic_check.set_active(self.window.is_mosaic())
        bottom.append(self.mosaic_check)
        bottom.append(Gtk.Box(hexpand=True))
        self.open_button = Gtk.Button(
            label="Ouvrir", css_classes=["suggested-action", "pill"], sensitive=False
        )
        self.open_button.connect("clicked", lambda *_: self._open())
        bottom.append(self.open_button)
        toolbar.add_bottom_bar(bottom)

        self.set_child(toolbar)
        self.set_default_widget(self.open_button)
        self.connect("closed", self._on_closed)

        for name in preset if preset is not None else server.favorite_screens:
            self._add_new(name, checked=True)
        self._update_open_button()
        self.refresh()
        GLib.idle_add(self._focus_entry)

    def _focus_entry(self):
        self.name_row.grab_focus()
        return GLib.SOURCE_REMOVE

    # -- chargement --------------------------------------------------------
    def refresh(self):
        if self._handle is not None:
            return
        self.error_group.set_visible(False)
        self.noscreen_group.set_visible(False)
        self.summary_spinner.set_visible(True)
        self.summary_spinner.set_spinning(True)
        self.summary_row.set_subtitle(f"Connexion à {self.server.display_name}…")
        self.refresh_button.set_sensitive(False)
        self.loading_row.set_visible(True)
        self.empty_row.set_visible(False)
        self._handle = remote.list_screens(
            self.server, self.app.settings.prefs, self.secret, self._on_listed
        )

    def _on_listed(self, result: remote.RemoteResult, listing: ssh.ScreenListing):
        self._handle = None
        if result.cancelled:
            return
        self.refresh_button.set_sensitive(True)
        self.summary_spinner.set_spinning(False)
        self.summary_spinner.set_visible(False)
        self.loading_row.set_visible(False)
        for choice in self.existing.values():
            self.existing_group.remove(choice.row)
        previously_checked = {n for n, c in self.existing.items() if c.check.get_active()}
        self.existing.clear()

        if not (result.ok and listing.ok):
            err = result.error()
            self.summary_row.set_subtitle("Connexion impossible")
            self.error_row.set_title(err.title)
            self.error_row.set_subtitle(GLib.markup_escape_text(err.detail))
            self.password_button.set_visible(err.auth_failed and self.server.auth == AUTH_PASSWORD)
            self.error_group.set_visible(True)
            self.existing_group.set_visible(False)
            self._update_open_button()
            return

        self.listing = listing
        self.existing_group.set_visible(listing.has_screen)
        self.noscreen_group.set_visible(not listing.has_screen)
        self.new_group.set_visible(listing.has_screen)
        count = len(listing.sessions)
        self.summary_row.set_subtitle(
            "Connecté · " + (f"{count} screen{'s' if count > 1 else ''} en cours" if count else "aucun screen en cours")
        )
        self.empty_row.set_visible(listing.has_screen and count == 0)

        open_names = self.window.open_screen_names(self.server)
        for session in sorted(listing.sessions, key=lambda s: s.name.lower()):
            if session.dead:
                continue
            name = session.name
            row = Adw.ActionRow(title=GLib.markup_escape_text(name))
            details = [] if session.attached else [session.state_label]
            if session.date:
                details.append(f"créé le {session.pretty_date}")
            row.set_subtitle(" · ".join(details))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            check.set_active(name in previously_checked or name in self.new)
            check.connect("toggled", lambda *_: self._update_open_button())
            row.add_prefix(check)
            row.set_activatable_widget(check)
            if name in open_names:
                pill = Gtk.Label(label="Ouvert ici", css_classes=["status-pill", "open"], valign=Gtk.Align.CENTER)
                pill.set_tooltip_text("Déjà ouvert dans ScreenPort : cocher bascule sur son onglet")
                row.add_suffix(pill)
            elif session.attached:
                pill = Gtk.Label(
                    label=session.state_label, css_classes=["status-pill", "attached"], valign=Gtk.Align.CENTER
                )
                pill.set_tooltip_text("Ce screen est affiché ailleurs")
                row.add_suffix(pill)
            kill = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text="Terminer ce screen sur le serveur",
                css_classes=["flat"],
            )
            kill.connect("clicked", self._on_kill, name)
            row.add_suffix(kill)
            if not self.window.is_valid_name(name):
                row.set_sensitive(False)
                row.set_subtitle("Nom non pris en charge (caractères spéciaux)")
            self.existing_group.add(row)
            self.existing[name] = _ScreenChoice(name, row, check)
            # Un favori qui existe déjà : on le coche dans la liste existante.
            if name in self.new:
                self._remove_new(name)
                check.set_active(True)
        self._update_open_button()

    # -- noms ---------------------------------------------------------------
    def _on_name_changed(self, row):
        text = row.get_text()
        clean = sanitize_screen_name(text)
        if text and not clean:
            row.add_css_class("error")
        else:
            row.remove_css_class("error")

    def _on_add_name(self, row):
        names = parse_screen_list_input(row.get_text())
        if not names:
            if row.get_text().strip():
                self.toasts.add_toast(Adw.Toast(title="Nom invalide : lettres, chiffres, - et _ uniquement"))
            elif self.open_button.get_sensitive():
                self._open()
            return
        for name in names:
            if name in self.existing:
                self.existing[name].check.set_active(True)
            else:
                self._add_new(name, checked=True)
        row.set_text("")
        self._update_open_button()

    def _add_new(self, name: str, checked: bool = True):
        if name in self.new:
            self.new[name].check.set_active(True)
            return
        row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle="Sera créé (ou rattaché s'il existe)")
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=checked)
        check.connect("toggled", lambda *_: self._update_open_button())
        row.add_prefix(check)
        row.set_activatable_widget(check)
        remove = Gtk.Button(
            icon_name="list-remove-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Retirer",
            css_classes=["flat"],
        )
        remove.connect("clicked", lambda *_: self._remove_new(name))
        row.add_suffix(remove)
        self.new_group.add(row)
        self.new[name] = _ScreenChoice(name, row, check)
        self._update_open_button()

    def _remove_new(self, name: str):
        choice = self.new.pop(name, None)
        if choice:
            self.new_group.remove(choice.row)
        self._update_open_button()

    def _selected(self) -> list[str]:
        names = [n for n, c in self.existing.items() if c.check.get_active()]
        names += [n for n, c in self.new.items() if c.check.get_active() and n not in names]
        return names

    def _update_open_button(self):
        count = len(self._selected())
        self.open_button.set_sensitive(count > 0)
        if count <= 1:
            self.open_button.set_label("Ouvrir le terminal" if count == 1 else "Ouvrir")
        else:
            self.open_button.set_label(f"Ouvrir {count} terminaux")
        self.mosaic_check.set_sensitive(count > 1 or len(self.window.panes()) > 0)

    # -- actions -------------------------------------------------------------
    def _open(self, force: bool = False):
        names = self._selected()
        if not names and force:
            names = list(self.server.favorite_screens[:1]) or ["main"]
        if not names:
            return
        self.window.open_sessions(
            self.server, names, self.secret, mosaic=self.mosaic_check.get_active()
        )
        self.close()

    def _on_open_shell(self, *_):
        self.window.open_sessions(self.server, [ssh.SHELL_ONLY], self.secret)
        self.close()

    def _on_kill(self, _button, name: str):
        def confirmed():
            def done(result: remote.RemoteResult):
                if result.ok:
                    self.toasts.add_toast(Adw.Toast(title=f"Screen « {name} » terminé"))
                    self.window.on_screen_killed(self.server, name)
                else:
                    self.toasts.add_toast(Adw.Toast(title=f"Impossible de terminer « {name} »"))
                self.refresh()

            remote.run_remote(
                self.server, self.app.settings.prefs, self.secret, ssh.remote_kill_command(name), done
            )

        confirm_kill(self, name, self.server, confirmed)

    def _on_enter_password(self, *_):
        def got(secret: str | None, remember: bool):
            if secret is None:
                return
            self.secret = secret
            self.window.remember_secret(self.server, secret, remember)
            self.refresh()

        ask_password(self, self.server, got, note="Le mot de passe a été refusé.")

    def _on_edit(self, *_):
        self.close()
        self.window.edit_server(self.server.id)

    def _on_closed(self, *_):
        if self._handle is not None:
            self._handle.cancel()


# --------------------------------------------------------------------------
# Petits dialogues
# --------------------------------------------------------------------------
def ask_password(parent, server: Server, callback: Callable[[str | None, bool], None], note: str = ""):
    """Demande le mot de passe d'un serveur ; ``callback(secret, mémoriser)``."""
    dialog = Adw.AlertDialog(
        heading="Mot de passe requis",
        body=(note + "\n" if note else "") + f"Mot de passe de {server.target} :",
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
    box.append(entry)
    remember = Gtk.CheckButton(label="Mémoriser le mot de passe", active=server.remember_secret)
    box.append(remember)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Annuler")
    dialog.add_response("ok", "Se connecter")
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def on_response(_dialog, response):
        if response == "ok" and entry.get_text():
            callback(entry.get_text(), remember.get_active())
        else:
            callback(None, False)

    dialog.connect("response", on_response)
    dialog.present(parent)
    entry.grab_focus()


def ask_screen_name(parent, title: str, body: str, initial: str, callback: Callable[[str], None], action="Valider"):
    dialog = Adw.AlertDialog(heading=title, body=body)
    entry = Gtk.Entry(text=initial, activates_default=True, placeholder_text="ex. : main, logs, build")
    hint = Gtk.Label(
        label="Lettres, chiffres, - et _ (les espaces deviennent des tirets).",
        css_classes=["dim-label", "caption"],
        wrap=True,
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.append(entry)
    box.append(hint)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Annuler")
    dialog.add_response("ok", action)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def on_changed(*_):
        dialog.set_response_enabled("ok", bool(parse_screen_list_input(entry.get_text())))

    entry.connect("changed", on_changed)
    on_changed()

    def on_response(_dialog, response):
        if response == "ok":
            names = parse_screen_list_input(entry.get_text())
            if names:
                callback(",".join(names))

    dialog.connect("response", on_response)
    dialog.present(parent)
    entry.grab_focus()
    entry.select_region(0, -1)


def confirm_kill(parent, name: str, server: Server, callback: Callable[[], None]):
    dialog = Adw.AlertDialog(
        heading=f"Terminer le screen « {name} » ?",
        body=f"Toutes les fenêtres et programmes de ce screen sur {server.display_name} "
        "seront arrêtés. Cette action est définitive.",
    )
    dialog.add_response("cancel", "Annuler")
    dialog.add_response("kill", "Terminer")
    dialog.set_response_appearance("kill", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: callback() if r == "kill" else None)
    dialog.present(parent)


def confirm(parent, heading: str, body: str, action: str, callback: Callable[[], None], destructive=True):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Annuler")
    dialog.add_response("ok", action)
    dialog.set_response_appearance(
        "ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED
    )
    dialog.set_default_response("cancel" if destructive else "ok")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: callback() if r == "ok" else None)
    dialog.present(parent)

