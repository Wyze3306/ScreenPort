#!/usr/bin/env bash
# Construit dist/screenport_<version>_all.deb à partir des sources.
# Dépendances : dpkg-deb, gzip, python3 (aucun outil debhelper requis).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' screenport/__init__.py)"
if [ -z "$VERSION" ]; then
    echo "Version introuvable dans screenport/__init__.py" >&2
    exit 1
fi

APP_ID="io.github.wyze3306.ScreenPort"
STAGE="$ROOT/build/deb/screenport_${VERSION}_all"
OUT="$ROOT/dist/screenport_${VERSION}_all.deb"

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$ROOT/dist"

# Programme
install -Dm755 bin/screenport "$STAGE/usr/bin/screenport"
install -Dm755 bin/screenport-askpass "$STAGE/usr/lib/screenport/bin/screenport-askpass"
for file in screenport/*.py screenport/style.css; do
    install -Dm644 "$file" "$STAGE/usr/lib/screenport/$file"
done

# Intégration au bureau
install -Dm644 "data/$APP_ID.desktop" "$STAGE/usr/share/applications/$APP_ID.desktop"
install -Dm644 "data/$APP_ID.metainfo.xml" "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"
install -Dm644 "data/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"

# Documentation
install -d "$STAGE/usr/share/man/man1" "$STAGE/usr/share/doc/screenport"
gzip -9n < data/screenport.1 > "$STAGE/usr/share/man/man1/screenport.1.gz"
install -m644 packaging/debian/copyright "$STAGE/usr/share/doc/screenport/copyright"
gzip -9n < packaging/debian/changelog > "$STAGE/usr/share/doc/screenport/changelog.gz"

# Métadonnées du paquet
install -m755 packaging/debian/postinst packaging/debian/prerm "$STAGE/DEBIAN/"
SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" packaging/debian/control.in > "$STAGE/DEBIAN/control"
(cd "$STAGE" && find usr -type f -exec md5sum {} + | sort -k2) > "$STAGE/DEBIAN/md5sums"
chmod 644 "$STAGE/DEBIAN/control" "$STAGE/DEBIAN/md5sums"

# Permissions homogènes quel que soit l'umask
find "$STAGE/usr" -type d -exec chmod 755 {} +

dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT" >/dev/null
echo "Paquet créé : ${OUT#"$ROOT"/}"
