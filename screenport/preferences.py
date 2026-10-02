"""Fenêtre des préférences."""

from __future__ import annotations

from gi.repository import Adw, Gtk, Pango

from . import palettes
from .ssh import ATTACH_DETACH, ATTACH_SHARE


class PalettePreview(Gtk.DrawingArea):
    """Aperçu d'une palette : fond, texte et les 16 couleurs ANSI."""

    def __init__(self, app):
        super().__init__(content_height=78, hexpand=True)
        self.app = app
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width, height):
        prefs = self.app.settings.prefs
        palette = palettes.resolve(prefs.palette, self.app.is_dark())

        def rgb(value):
            value = value.lstrip("#")
            return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))

        radius = 10
        cr.new_sub_path()
        cr.arc(width - radius, radius, radius, -1.5708, 0)
        cr.arc(width - radius, height - radius, radius, 0, 1.5708)
        cr.arc(radius, height - radius, radius, 1.5708, 3.1416)
        cr.arc(radius, radius, radius, 3.1416, 4.7124)
        cr.close_path()
        cr.set_source_rgb(*rgb(palette.background))
        cr.fill()

        cr.select_font_face("Monospace")
        cr.set_font_size(13)
        cr.move_to(14, 26)
        cr.set_source_rgb(*rgb(palette.colors[2]))
        cr.show_text("admin@serveur")
        cr.set_source_rgb(*rgb(palette.foreground))
        cr.show_text(":")
        cr.set_source_rgb(*rgb(palette.colors[4]))
        cr.show_text("~")
        cr.set_source_rgb(*rgb(palette.foreground))
        cr.show_text("$ screen -ls")

        size = min(18, (width - 28) / 16 - 2)
        for index, color in enumerate(palette.colors):
            x = 14 + index * (size + 2)
            cr.rectangle(x, height - size - 14, size, size)
            cr.set_source_rgb(*rgb(color))
            cr.fill()


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title="Préférences", search_enabled=False)
        self.app = app
        self.prefs = app.settings.prefs
        self.add(self._terminal_page())
        self.add(self._sessions_page())

    # -------------------------------------------------------------- utils
    def _changed(self):
        self.app.settings.save()
        self.app.apply_prefs()

    def _switch(self, title, subtitle, attr):
        row = Adw.SwitchRow(title=title, subtitle=subtitle or "")
        row.set_active(getattr(self.prefs, attr))

        def on_change(r, _p):
            setattr(self.prefs, attr, r.get_active())
            self._changed()

        row.connect("notify::active", on_change)
        return row

    def _spin(self, title, subtitle, attr, lower, upper, step):
        row = Adw.SpinRow.new_with_range(lower, upper, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_value(getattr(self.prefs, attr))

        def on_change(r, _p):
            setattr(self.prefs, attr, int(r.get_value()))
            self._changed()

        row.connect("notify::value", on_change)
        return row

    def _combo(self, title, subtitle, attr, choices):
        row = Adw.ComboRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_model(Gtk.StringList.new([label for _value, label in choices]))
        values = [value for value, _label in choices]
        current = getattr(self.prefs, attr)
        row.set_selected(values.index(current) if current in values else 0)

        def on_change(r, _p):
            setattr(self.prefs, attr, values[r.get_selected()])
            self._changed()

        row.connect("notify::selected", on_change)
        return row

    # -------------------------------------------------------------- pages
    def _terminal_page(self):
        page = Adw.PreferencesPage(title="Terminal", icon_name="utilities-terminal-symbolic")

        look = Adw.PreferencesGroup(title="Apparence")
        font_row = Adw.ActionRow(title="Police")
        font_dialog = Gtk.FontDialog(title="Police du terminal")
        font_dialog.set_filter(Gtk.CustomFilter.new(_is_monospace))
        font_button = Gtk.FontDialogButton(dialog=font_dialog, valign=Gtk.Align.CENTER)
        font_button.set_font_desc(Pango.FontDescription.from_string(self.prefs.font))
        font_button.set_use_font(True)

        def on_font(button, _p):
            desc = button.get_font_desc()
            if desc is not None:
                self.prefs.font = desc.to_string()
                self._changed()

        font_button.connect("notify::font-desc", on_font)
        font_row.add_suffix(font_button)
        font_row.set_activatable_widget(font_button)
        look.add(font_row)

        ids = palettes.palette_ids()
        palette_row = self._combo(
            "Couleurs", None, "palette", [(pid, palettes.palette_label(pid)) for pid in ids]
        )
        look.add(palette_row)
        preview = PalettePreview(self.app)
        preview.set_margin_top(12)
        palette_row.connect("notify::selected", lambda *_: preview.queue_draw())
        look.add(preview)

        look.add(
            self._combo(
                "Style de l'application",
                None,
                "color_scheme",
                [("default", "Suivre le système"), ("light", "Clair"), ("dark", "Sombre")],
            )
        )
        page.add(look)

        behaviour = Adw.PreferencesGroup(title="Comportement")
        behaviour.add(
            self._combo(
                "Forme du curseur",
                None,
                "cursor_shape",
                [("block", "Bloc"), ("ibeam", "Barre verticale"), ("underline", "Souligné")],
            )
        )
        behaviour.add(self._switch("Curseur clignotant", None, "cursor_blink"))
        behaviour.add(
            self._switch(
                "Copier la sélection automatiquement",
                "Le texte sélectionné est copié dans le presse-papiers",
                "copy_on_select",
            )
        )
        behaviour.add(self._switch("Bip sonore", "Sinon, l'onglet est simplement signalé", "audible_bell"))
        behaviour.add(
            self._spin("Lignes d'historique du terminal", None, "scrollback", 0, 1_000_000, 1000)
        )
        page.add(behaviour)
        return page

    def _sessions_page(self):
        page = Adw.PreferencesPage(title="Sessions", icon_name="network-server-symbolic")

        screen = Adw.PreferencesGroup(
            title="GNU screen",
            description="Un screen portant le nom demandé est rejoint s'il existe, créé sinon.",
        )
        screen.add(
            self._combo(
                "Si le screen est déjà attaché ailleurs",
                None,
                "attach_mode",
                [
                    (ATTACH_DETACH, "Le reprendre (détache l'autre affichage)"),
                    (ATTACH_SHARE, "Le partager (multi-affichage)"),
                ],
            )
        )
        screen.add(
            self._spin(
                "Historique des nouveaux screens",
                "Lignes conservées par fenêtre screen",
                "screen_history",
                100,
                1_000_000,
                1000,
            )
        )
        page.add(screen)

        sessions = Adw.PreferencesGroup(title="Onglets")
        sessions.add(
            self._switch(
                "Rouvrir les terminaux au démarrage",
                "Se rattache automatiquement aux screens ouverts lors de la dernière fermeture",
                "restore_sessions",
            )
        )
        sessions.add(
            self._switch(
                "Reconnexion automatique",
                "Après une coupure réseau, se reconnecter et se rattacher au screen",
                "auto_reconnect",
            )
        )
        sessions.add(
            self._switch(
                "Confirmer avant de fermer la fenêtre", None, "confirm_close"
            )
        )
        page.add(sessions)

        connection = Adw.PreferencesGroup(title="Connexion SSH")
        connection.add(
            self._switch(
                "Réutiliser la connexion SSH",
                "Une seule authentification par serveur : les terminaux suivants s'ouvrent instantanément",
                "multiplex",
            )
        )
        connection.add(
            self._spin(
                "Maintien de la connexion (secondes)",
                "Intervalle des signaux keep-alive, 0 pour désactiver",
                "keepalive",
                0,
                3600,
                5,
            )
        )
        connection.add(
            self._switch(
                "Accepter automatiquement les nouveaux serveurs",
                "Enregistre l'empreinte d'un serveur inconnu ; une empreinte modifiée reste toujours refusée",
                "accept_new_hostkeys",
            )
        )
        page.add(connection)
        return page


def _is_monospace(item) -> bool:
    family = item.get_family() if isinstance(item, Pango.FontFace) else item
    try:
        return family.is_monospace()
    except AttributeError:
        return True
