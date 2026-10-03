"""ScreenPort — client SSH graphique pour gérer des sessions GNU screen."""

APP_ID = "io.github.wyze3306.ScreenPort"
APP_NAME = "ScreenPort"
VERSION = "1.1.0"
WEBSITE = "https://github.com/Wyze3306/ScreenPort"

# Versions des bibliothèques GObject utilisées (doit précéder tout import
# depuis gi.repository). Les absences sont signalées au lancement.
try:
    import gi as _gi

    for _namespace, _version in (("Gtk", "4.0"), ("Gdk", "4.0"), ("Adw", "1"), ("Vte", "3.91")):
        try:
            _gi.require_version(_namespace, _version)
        except ValueError:
            pass
except ImportError:  # pragma: no cover
    pass
