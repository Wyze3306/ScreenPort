"""Palettes de couleurs pour les terminaux."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    id: str
    label: str
    foreground: str
    background: str
    colors: tuple[str, ...]  # 16 couleurs ANSI
    cursor: str | None = None
    dark: bool = True


_ADWAITA = (
    "#241f31", "#c01c28", "#2ec27e", "#f5c211", "#1e78e4", "#9841bb", "#0ab9dc", "#c0bfbc",
    "#5e5c64", "#ed333b", "#57e389", "#f8e45c", "#51a1ff", "#c061cb", "#4fd2fd", "#f6f5f4",
)
_SOLARIZED = (
    "#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5",
    "#002b36", "#cb4b16", "#586e75", "#657b83", "#839496", "#6c71c4", "#93a1a1", "#fdf6e3",
)

PALETTES: dict[str, Palette] = {
    p.id: p
    for p in (
        Palette(
            "tokyo-night", "Tokyo Night", "#c0caf5", "#1a1b26",
            ("#15161e", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#7dcfff", "#a9b1d6",
             "#414868", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#7dcfff", "#c0caf5"),
            cursor="#c0caf5",
        ),
        Palette(
            "catppuccin-mocha", "Catppuccin Mocha", "#cdd6f4", "#1e1e2e",
            ("#45475a", "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#f5c2e7", "#94e2d5", "#bac2de",
             "#585b70", "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#f5c2e7", "#94e2d5", "#a6adc8"),
            cursor="#f5e0dc",
        ),
        Palette(
            "dracula", "Dracula", "#f8f8f2", "#282a36",
            ("#21222c", "#ff5555", "#50fa7b", "#f1fa8c", "#bd93f9", "#ff79c6", "#8be9fd", "#f8f8f2",
             "#6272a4", "#ff6e6e", "#69ff94", "#ffffa5", "#d6acff", "#ff92df", "#a4ffff", "#ffffff"),
        ),
        Palette(
            "nord", "Nord", "#d8dee9", "#2e3440",
            ("#3b4252", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#88c0d0", "#e5e9f0",
             "#4c566a", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead", "#8fbcbb", "#eceff4"),
        ),
        Palette(
            "one-dark", "One Dark", "#abb2bf", "#282c34",
            ("#282c34", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#abb2bf",
             "#5c6370", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#ffffff"),
        ),
        Palette(
            "gruvbox-dark", "Gruvbox sombre", "#ebdbb2", "#282828",
            ("#282828", "#cc241d", "#98971a", "#d79921", "#458588", "#b16286", "#689d6a", "#a89984",
             "#928374", "#fb4934", "#b8bb26", "#fabd2f", "#83a598", "#d3869b", "#8ec07c", "#ebdbb2"),
        ),
        Palette("solarized-dark", "Solarized sombre", "#839496", "#002b36", _SOLARIZED),
        Palette("adwaita-dark", "Adwaita sombre", "#ffffff", "#1e1e1e", _ADWAITA),
        Palette("adwaita-light", "Adwaita clair", "#1e1e1e", "#ffffff", _ADWAITA, dark=False),
        Palette("solarized-light", "Solarized clair", "#657b83", "#fdf6e3", _SOLARIZED, dark=False),
    )
}

# Palette spéciale qui suit le thème clair/sombre du système.
SYSTEM_ID = "system"
SYSTEM_LABEL = "Suivre le thème du système"


def palette_ids() -> list[str]:
    return [SYSTEM_ID, *PALETTES.keys()]


def palette_label(palette_id: str) -> str:
    if palette_id == SYSTEM_ID:
        return SYSTEM_LABEL
    palette = PALETTES.get(palette_id)
    return palette.label if palette else palette_id


def resolve(palette_id: str, dark_theme: bool) -> Palette:
    if palette_id == SYSTEM_ID:
        return PALETTES["adwaita-dark" if dark_theme else "adwaita-light"]
    return PALETTES.get(palette_id, PALETTES["tokyo-night"])
