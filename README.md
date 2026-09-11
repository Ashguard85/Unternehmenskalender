# Stiftungskalender v2 — Docker Desktop

Eigenständiger, desktop-first Stiftungskalender für mehrere Unternehmen.

## Architektur

- eine Docker-/Flask-Web-App
- SQLite unter `/app/data/stiftungskalender.sqlite`
- keine PWA, kein Service Worker, kein Offline-App-Cache
- kein Web Push und keine Geräteverwaltung
- keine Confluence-/Jira-Abhängigkeit
- Updates über Docker/Portainer

## Kernfunktionen

- Unternehmen zentral verwalten
- Termine einmalig speichern und entweder für **alle Unternehmen** oder für **eine Mehrfachauswahl** zuordnen
- mehrere Termine pro Tag
- Ganztag sowie frei wählbare Von-/Bis-Datums- und Zeitbereiche
- Serienerstellung nach Wochentag
- Terminliste mit Unternehmens-, Jahres- und Suchfilter
- Jahresplan mit Unternehmens- und Monatsfilter
- PDF-, CSV- und ICS-Export
- schreibgeschützte Gesamtkalender- und Unternehmensfeeds mit widerrufbaren Tokens
- Ferien/Feiertage manuell, per ICS-Datei oder automatischem Kalender-Abo
- Änderungsverlauf mit `vorher → nachher` und Wiederherstellung gelöschter Termine
- JSON-Komplettbackup und automatische SQLite-Sicherungen

## Desktop-Oberfläche

Die Hauptnavigation befindet sich oben:

`Übersicht · Termine · Jahresplan · Unternehmen · Einstellungen`

Unternehmen sind ein eigener Arbeitsbereich und nicht mehr in den Einstellungen versteckt.

## Docker / Portainer

Beispiel:

```yaml
services:
  stiftungskalender:
    build:
      context: https://github.com/USER/stiftungskalender.git#main
    container_name: stiftungskalender
    ports:
      - "8090:8080"
    volumes:
      - /home/USER/docker/stiftungskalender/data:/app/data
    environment:
      - APP_TITLE=Stiftungskalender
      - APP_URL=https://kalender.example.ch
      - AUTH_ENABLED=true
      - APP_USER=admin
      - APP_PASSWORD=${APP_PASSWORD}
      - SECRET_KEY=${SECRET_KEY}
      - ICAL_TOKEN=${ICAL_TOKEN}
      - BACKUP_KEEP=50
    restart: unless-stopped
```

`APP_URL` wird für externe Kalenderlinks benötigt und sollte bei produktivem Betrieb eine HTTPS-Adresse sein.

## Persistenz / Backup

Persistente Nutzdaten liegen ausschließlich unter `/app/data`. Die App erzeugt zusätzlich konsistente SQLite-Sicherungen unter `/app/data/backups`.

Das JSON-Komplettbackup enthält Unternehmen, Termine, Zeiträume und Kalender-Abos.

## Versionierung

Dieses eigenständige Desktop-Projekt beginnt neu bei v1. Diese Ausgabe ist **v2** und entfernt vollständig die frühere PWA-/Push-Architektur.
