#!/usr/bin/env python3
"""
Regista: fa partire i run a orario al posto dei lanci programmati di GitHub, che nei repository nuovi arrivano con ore
di ritardo o non arrivano affatto. Lo lancia .github/workflows/regista.yml, che a sua volta viene "svegliato" da fuori
ogni 15 minuti (cron-job.org chiama l'API workflow_dispatch) e, quando GitHub lo fa, anche dal suo orario.

A ogni sveglia, per ognuno dei quattro workflow guarda l'ultimo ciclo il cui orario di partenza e' passato (al massimo
da 10 ore) e lo lancia se:
  - non e' gia' pubblicato (cartella archivio/<ciclo> nel ramo risultati-<zona>-<modello>);
  - non c'e' gia' un run di quel workflow in coda o in corso;
  - i tentativi dopo l'orario di partenza sono meno di 3 e l'ultimo e' di almeno 50 minuti fa (se un run fallisce,
    per esempio perche' i dati non sono ancora pronti, si riprova un'ora dopo, ma non all'infinito).
Fa lo stesso con la verifica giornaliera (una volta al giorno, dopo le 05:20 UTC).
Ogni sveglia lancia al massimo un run per workflow; la sveglia dopo vede il run gia' partito.

  regista.py [--prova]       --prova: dice cosa farebbe senza lanciare nulla
Serve `gh` con GH_TOKEN (permessi actions: write, contents: read).
"""
import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "Superbug87/wrf-meteovelletri")
# workflow -> (ramo dei risultati, {ora del ciclo: ritardo della partenza dopo l'inizio del ciclo})
# stessi orari dei lanci programmati scritti nei workflow (i dati ECMWF escono circa 7 ore e mezza dopo il ciclo,
# ICON-2I circa 2 ore dopo; il Lazio ICON-2I parte 75 minuti dopo i Castelli per trovare i dati gia' in cache)
PREVISIONI = {
    "castelli-icon2i.yml": ("risultati-castelli-icon2i", {0: (3, 10), 12: (3, 10)}),
    "lazio-icon2i.yml": ("risultati-lazio-icon2i", {0: (4, 25), 12: (4, 25)}),
    "castelli-ecmwf.yml": ("risultati-castelli-ecmwf", {0: (8, 40), 12: (8, 10)}),
    "lazio-ecmwf.yml": ("risultati-lazio-ecmwf", {0: (9, 10), 12: (8, 20)}),
}
VERIFICA = ("verifica.yml", (5, 20))
# prove affiancate del nudging (sperimentale.yml, non pubblicano): per ogni ciclo di questo workflow, fino a questa data,
# si lanciano queste varianti con un quarto delle stazioni della Regione tenute fuori; il confronto "solo WU" e' il run normale
PROVE = {"workflow": "castelli-ecmwf.yml", "zona": "castelli", "pilota": "ecmwf", "varianti": ("ufficiali", "tutte"),
         "dal_ciclo": "2026101812", "fino_a": "2026-10-25"}
FINESTRA = timedelta(hours=10)
TENTATIVI = 3
PAUSA = timedelta(minutes=50)          # le sveglie arrivano a distanza di circa un'ora: un po' di margine


def gh(*args, json_out=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}...: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout) if json_out else r.stdout


def quando(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def run_del_workflow(wf):
    return gh("run", "list", "-R", REPO, "--workflow", wf, "-L", "30", "--json", "status,conclusion,createdAt,event,databaseId")


def pubblicato(ramo, ciclo):
    r = subprocess.run(["gh", "api", f"repos/{REPO}/contents/archivio/{ciclo}?ref={ramo}", "--silent"],
                       capture_output=True, text=True)
    return r.returncode == 0


def decidi(runs, partenza, adesso):
    """None se si deve lanciare, altrimenti il motivo per non farlo."""
    attivi = [r for r in runs if r["status"] in ("queued", "in_progress", "waiting", "pending", "requested")]
    if attivi:
        return f"run {attivi[0]['databaseId']} gia' {attivi[0]['status']}"
    dopo = sorted((quando(r["createdAt"]) for r in runs if quando(r["createdAt"]) >= partenza), reverse=True)
    if len(dopo) >= TENTATIVI:
        return f"gia' {len(dopo)} tentativi dopo le {partenza:%H:%M}: basta"
    if dopo and adesso - dopo[0] < PAUSA:
        return f"ultimo tentativo alle {dopo[0]:%H:%M}, si riprova dopo {PAUSA.seconds // 60} minuti"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prova", action="store_true")
    a = ap.parse_args()
    adesso = datetime.now(timezone.utc)
    oggi = adesso.replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"Regista alle {adesso:%Y-%m-%d %H:%M} UTC{' (prova: non lancia nulla)' if a.prova else ''}")

    for wf, (ramo, ritardi) in PREVISIONI.items():
        candidati = []
        for giorno in (oggi - timedelta(days=1), oggi):
            for ora, (h, m) in ritardi.items():
                ciclo = giorno + timedelta(hours=ora)
                partenza = ciclo + timedelta(hours=h, minutes=m)
                if partenza <= adesso < partenza + FINESTRA:
                    candidati.append((partenza, ciclo))
        if not candidati:
            print(f"  {wf}: nessun ciclo da lanciare adesso")
            continue
        partenza, ciclo = max(candidati)
        c = ciclo.strftime("%Y%m%d%H")
        try:
            if pubblicato(ramo, c):
                print(f"  {wf}: ciclo {c} gia' pubblicato")
                continue
            motivo = decidi(run_del_workflow(wf), partenza, adesso)
            if motivo:
                print(f"  {wf}: ciclo {c} non pubblicato, ma {motivo}")
                continue
            print(f"  {wf}: LANCIO il ciclo {c} (partenza prevista {partenza:%H:%M} UTC)")
            if not a.prova:
                gh("workflow", "run", wf, "-R", REPO, "--ref", "main", "-f", f"ciclo={c}", json_out=False)
        except RuntimeError as e:                    # un workflow che non risponde non ferma gli altri
            print(f"  !! {wf}: {e}")

    if adesso.date().isoformat() <= PROVE["fino_a"]:
        ritardi = PREVISIONI[PROVE["workflow"]][1]
        cand = [(g + timedelta(hours=o) + timedelta(hours=h, minutes=m), g + timedelta(hours=o))
                for g in (oggi - timedelta(days=1), oggi) for o, (h, m) in ritardi.items()]
        cand = [x for x in cand if x[0] <= adesso < x[0] + FINESTRA]
        if cand:
            partenza, ciclo = max(cand)
            c = ciclo.strftime("%Y%m%d%H")
            try:
                if c < PROVE["dal_ciclo"]:
                    raise StopIteration
                fatte = {r["displayTitle"] for r in gh("run", "list", "-R", REPO, "--workflow", "sperimentale.yml", "-L", "40",
                                                        "--json", "displayTitle")}
                for v in PROVE["varianti"]:
                    titolo = f"Prova {PROVE['zona']} {PROVE['pilota']} {v} {c}"
                    if titolo in fatte:
                        continue
                    print(f"  sperimentale.yml: LANCIO {titolo}")
                    if not a.prova:
                        gh("workflow", "run", "sperimentale.yml", "-R", REPO, "--ref", "main", "-f", f"zona={PROVE['zona']}",
                           "-f", f"pilota={PROVE['pilota']}", "-f", f"ciclo={c}", "-f", f"nudging={v}", "-f", "controllo=true",
                           json_out=False)
            except StopIteration:
                pass
            except RuntimeError as e:
                print(f"  !! sperimentale.yml: {e}")

    wf, (h, m) = VERIFICA
    partenza = oggi + timedelta(hours=h, minutes=m)
    if adesso >= partenza:
        try:
            runs = [r for r in run_del_workflow(wf) if quando(r["createdAt"]) >= partenza]
            if any(r["conclusion"] == "success" for r in runs):
                print(f"  {wf}: verifica di ieri gia' fatta")
            else:
                motivo = decidi(runs, partenza, adesso)
                if motivo:
                    print(f"  {wf}: {motivo}")
                else:
                    print(f"  {wf}: LANCIO la verifica di ieri")
                    if not a.prova:
                        gh("workflow", "run", wf, "-R", REPO, "--ref", "main", json_out=False)
        except RuntimeError as e:
            print(f"  !! {wf}: {e}")


if __name__ == "__main__":
    main()
