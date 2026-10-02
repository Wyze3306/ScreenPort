"""Dialogue d'import des hôtes de ~/.ssh/config."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .models import COLORS
from .sshconfig import load_user_ssh_config


class ImportDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title="Importer depuis ~/.ssh/config")
        self.window = window
        self.set_content_width(520)
        self.set_content_height(600)
        self.checks: list[tuple[Gtk.CheckButton, object]] = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Annuler")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.import_button = Gtk.Button(label="Importer", css_classes=["suggested-action"])
        self.import_button.connect("clicked", self._on_import)
        header.pack_end(self.import_button)
        toolbar.add_top_bar(header)

        hosts = load_user_ssh_config()
        existing = {(s.host, s.user, s.port) for s in window.app.store.servers}
        if not hosts:
            page = Adw.StatusPage(
                icon_name="document-open-symbolic",
                title="Aucun hôte trouvé",
                description="Le fichier ~/.ssh/config est absent ou ne contient aucune entrée « Host ».",
            )
            self.import_button.set_sensitive(False)
            toolbar.set_content(page)
        else:
            page = Adw.PreferencesPage()
            group = Adw.PreferencesGroup(
                title="Hôtes trouvés",
                description="Les options de ~/.ssh/config (ProxyJump, clés...) restent appliquées.",
            )
            for host in hosts:
                server = host.to_server()
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(host.alias),
                    subtitle=GLib.markup_escape_text(host.description),
                )
                check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                already = (server.host, server.user, server.port) in existing
                check.set_active(not already)
                if already:
                    row.set_sensitive(False)
                    row.set_subtitle(GLib.markup_escape_text(host.description) + " · déjà importé")
                check.connect("toggled", lambda *_: self._update())
                row.add_prefix(check)
                row.set_activatable_widget(check)
                group.add(row)
                if not already:
                    self.checks.append((check, server))
            page.add(group)
            toolbar.set_content(page)
        self.set_child(toolbar)
        self._update()

    def _update(self):
        count = sum(1 for check, _ in self.checks if check.get_active())
        self.import_button.set_sensitive(count > 0)
        self.import_button.set_label(f"Importer ({count})" if count else "Importer")

    def _on_import(self, *_):
        colors = list(COLORS)
        count = 0
        for index, (check, server) in enumerate(self.checks):
            if check.get_active():
                server.color = colors[index % len(colors)]
                self.window.app.store.upsert(server)
                count += 1
        self.window.app.refresh_servers()
        self.window.toast(f"{count} serveur{'s' if count > 1 else ''} importé{'s' if count > 1 else ''}")
        self.close()
