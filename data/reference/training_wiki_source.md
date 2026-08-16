# Training requirements — wiki source & refresh

`data/de/training.json` is derived from the Leitstellenspiel wiki, scraped
2026-08-16. This file records the source and the raw mapping so the data can be
audited and refreshed.

## Sources

- Vehicle → course: <https://wiki.leitstellenspiel.de/index.php?title=Fahrzeug_Übersicht>
- Course → vehicles: <https://wiki.leitstellenspiel.de/index.php?title=Lehrgänge>

MediaWiki serves raw wikitext at `?title=<Page>&action=raw`, which extracts far
more cleanly than the rendered page.

## To refresh

1. Re-read both pages (raw).
2. Update `vehicle_training` in `data/de/training.json` — only vehicles that
   *require* a course need an entry; anything omitted needs none.
3. Names are matched normalised (case/space/punctuation-insensitive) against the
   game's `vehicle_type_caption`, so minor formatting differences are fine.
   Exotic names (long parenthetical THW/SEG designations) may not match a live
   caption exactly — verify against a real fleet and adjust.

## Course → vehicles (raw scrape, for reference)

Fire: GW-Messtechnik→GW-Messtechnik · GW-Gefahrgut→GW-Gefahrgut, WLF(AB-Gefahrgut)
· GW-Höhenrettung→GW-Höhenrettung · Flugfeldlöschfahrzeug→FLF · Rettungstreppen→
Rettungstreppe · ELW 2→ELW2, WLF(AB-Einsatzleitung) · Wechsellader→WLF · Dekon-P→
Dekon-P, WLF(AB-Dekon-P) · Feuerwehrkran→FwK · GW-Taucher→GW-Taucher ·
GW-Wasserrettung→GW-Wasserrettung, WLF(AB-MZB) · Notarzt→NEF,NAW,RTH,ITW,GRTW ·
Werkfeuerwehr→GW-Werkfeuerwehr,ULF mit Löscharm,TM 50 · Intensivpflege→ITW ·
Bahnrettung→RW-Schiene,HLF Schiene,AB-Schiene

Rettungsdienst: Notarzt→NEF,RTH,GRTW · LNA→KdoW-LNA · OrgL→KdoW-OrgL ·
SEG-Einsatzleitung→ELW 1 (SEG) · SEG-GW-SAN→GW-SAN · GW-Taucher→GW-Taucher ·
GW-Wasserrettung→GW-Wasserrettung · Rettungshundeführer(SEG)→Rettungshundefahrzeug
· Drohnenoperator→GW-UAS · Höhenretter→Anh Höhenrettung · Einsatzleiter
Bergrettung→ELW Bergrettung

Polizei: Zugführer→leBefKw · Hundertschaftsführer→FüKw · Polizeihubschrauber→
Polizeihubschrauber · Wasserwerfer→Wasserwerfer · Motorradstaffel→Polizeimotorrad
· Hundeführer→DHuFüKw · MEK→MEK-MTF,MEK-ZF · SEK→SEK-MTF,SEK-ZF · Kriminalpolizei→
Zivilstreifenwagen · Dienstgruppenleitung→FuStW(DGL)

THW: Zugtrupp→MTW-TZ · Fachgruppe Räumen→LKW K9,MLW 5 · Fachgruppe Wassergefahren→
LKW 7Lkr 19tm · Bergungstaucher→Tauchkraftwagen · Fachgruppe Rettungshundeführer→
MTW-OV · Fachgruppe Wasserschaden/Pumpen→LKW 7 Lbw (FGr WP),MLW 4 · Fachgruppe
Schwere Bergung→MzGW SB · Fachgruppe Elektroversorgung→LKW 7 Lbw (FGr E) · Trupp
Unbemannte Luftfahrtsysteme→MTW-Tr UL · Fachzug Führung und Kommunikation→FmKW,
FüKW (THW),FüKomKW,MTW-FGr K

Seenotrettung: Seenotretter→Seenotrettungskreuzer
