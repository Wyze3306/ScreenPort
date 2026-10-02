PYTHON ?= python3

.PHONY: run test deb check clean deps

## Lance l'application depuis les sources
run:
	$(PYTHON) bin/screenport

## Tests unitaires
test:
	$(PYTHON) -m unittest discover -s tests -t . -v

## Construit dist/screenport_<version>_all.deb
deb:
	packaging/build-deb.sh

## Vérifie le paquet avec lintian (si installé)
check: deb
	lintian --tag-display-limit 0 dist/*.deb || true

## Installe les dépendances pour lancer depuis les sources (Debian/Ubuntu)
deps:
	sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91 gir1.2-secret-1 openssh-client

clean:
	rm -rf build dist
	find . -name __pycache__ -type d -exec rm -rf {} +
