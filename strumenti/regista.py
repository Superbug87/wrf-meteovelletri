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

Memoria: l'elenco delle prove gia' lanciate sta nel file lanciate.txt del ramo "regista" (non nella lista dei run di GitHub, che
si puo' ripulire senza che il regista rilanci prove gia' fatte). Nello stesso ramo scrive stato.json, letto dalla pagina
stato-wrf.html del sito: ultimo ciclo pubblicato, run in corso e prove attive.

  regista.py [--prova]       --prova: dice cosa farebbe senza lanciare nulla
Serve `gh` con GH_TOKEN (permessi actions: write, contents: read).
"""
import argparse
import base64
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "Superbug87/wrf-meteovelletri")
# workflow -> (ramo dei risultati, {ora del ciclo: ritardo della partenza dopo l'inizio del ciclo})
# stessi orari dei lanci programmati scritti nei workflow (i dati ECMWF escono circa 7 ore e mezza dopo il ciclo,
# ICON-2I circa 2 ore dopo; il Lazio ICON-2I parte 20 minuti dopo i Castelli, che in circa 7 minuti mettono i dati in cache)
PREVISIONI = {
    "castelli-icon2i.yml": ("risultati-castelli-icon2i", {0: (3, 10), 12: (3, 10)}),
    "lazio-icon2i.yml": ("risultati-lazio-icon2i", {0: (3, 30), 12: (3, 30)}),   # i Castelli mettono ICON-2I in cache in ~7 min
    "castelli-ecmwf.yml": ("risultati-castelli-ecmwf", {0: (7, 50), 12: (7, 50)}),   # ECMWF a 48 h esce verso +7:35
    "lazio-ecmwf.yml": ("risultati-lazio-ecmwf", {0: (7, 50), 12: (7, 50)}),
}
VERIFICA = ("verifica.yml", (5, 20))
# prove affiancate del nudging (sperimentale.yml, non pubblicano): per ogni ciclo di questo workflow, fino a questa data,
# si lanciano queste varianti con un quarto delle stazioni della Regione tenute fuori; il confronto "solo WU" e' il run normale
PROVE = [
    {"workflow": "castelli-ecmwf.yml", "zona": "castelli", "pilota": "ecmwf", "varianti": ("ufficiali", "tutte"),
     "controllo": "true", "dal_ciclo": "2026101812", "fino_a": "2026-10-25"},
    # (la prova del Lazio a 3 km, zona lazio3, e' finita il 20/10/2026: il Lazio normale e' passato a 27-9-3 km)
]
# prove di fisica (sperimentale.yml con il campo "fisica": non pubblicano, lasciano l'estratto per la pagella): per ogni zona e
# modello, una prova per ciascuna di queste varianti, con il nudging in uso (wu). Partono dopo i run normali, cosi' i dati
# globali sono gia' in cache: ICON-2I 3:30 dopo il ciclo (come il Lazio normale), ECMWF 8:20 (30 minuti dopo il normale).
FISICHE = ("topo2", "mynn", "noahmp", "mynn-noahmp")
RITARDI_PROVE = {"icon2i": {0: (3, 30), 12: (3, 30)}, "ecmwf": {0: (8, 20), 12: (8, 20)}}
for _wf, _z, _p in (("castelli-ecmwf.yml", "castelli", "ecmwf"), ("castelli-icon2i.yml", "castelli", "icon2i"),
                    ("lazio-ecmwf.yml", "lazio", "ecmwf"), ("lazio-icon2i.yml", "lazio", "icon2i")):
    PROVE.append({"workflow": _wf, "zona": _z, "pilota": _p, "varianti": ("wu",), "fisiche": FISICHE, "controllo": "false",
                  "ritardi": RITARDI_PROVE[_p], "dal_ciclo": "2026101900", "fino_a": "2026-10-31"})
# prove della frequenza della radiazione (solo Castelli, dominio da 1 km: 6 e 15 minuti invece di 3): stessi ritardi delle altre prove di fisica
for _wf, _p in (("castelli-ecmwf.yml", "ecmwf"), ("castelli-icon2i.yml", "icon2i")):
    PROVE.append({"workflow": _wf, "zona": "castelli", "pilota": _p, "varianti": ("wu",), "fisiche": ("radt6", "radt15"),
                  "controllo": "false", "ritardi": RITARDI_PROVE[_p], "dal_ciclo": "2026102000", "fino_a": "2026-10-31"})
# prova del modello urbano (UCM, Roma): come le altre prove di fisica, una per zona e modello, con il nudging wu; non pubblica
for _wf, _z, _p in (("castelli-ecmwf.yml", "castelli", "ecmwf"), ("castelli-icon2i.yml", "castelli", "icon2i"),
                    ("lazio-ecmwf.yml", "lazio", "ecmwf"), ("lazio-icon2i.yml", "lazio", "icon2i")):
    PROVE.append({"workflow": _wf, "zona": _z, "pilota": _p, "varianti": ("wu",), "fisiche": ("urbano",), "controllo": "false",
                  "ritardi": RITARDI_PROVE[_p], "dal_ciclo": "2026102100", "fino_a": "2026-10-31"})
# prove di piloti diversi (ICON-EU del DWD e GFS di NOAA, entrambi con acqua di nube ai bordi): stesse zone, nudging wu, mai pubblicate;
# l'estratto va nel ramo prove-fisica e la pagella le mette a confronto. ICON-EU e GFS sono pronti circa 4 ore dopo il ciclo.
for _z in ("castelli", "lazio"):
    for _p in ("iconeu", "gfs"):
        PROVE.append({"workflow": f"{_z}-icon2i.yml", "zona": _z, "pilota": _p, "varianti": ("wu",), "controllo": "false",
                      "ritardi": {0: (5, 0), 12: (5, 0)}, "dal_ciclo": "2026101912", "fino_a": "2026-10-31"})
# GitHub non ha priorita' tra i lavori: con 20 lavori contemporanei (limite dei runner gratuiti, per account) un segmento dei run
# normali che finisce il suo turno si rimette in coda dietro agli altri. Per lasciare sempre posti liberi ai run normali (al
# massimo 4 insieme) le prove ne usano al piu' MAX_PROVE alla volta; le altre aspettano la sveglia dopo.
MAX_PROVE = 12
ATTIVI = ("queued", "in_progress", "waiting", "pending", "requested")
FINESTRA = timedelta(hours=10)
TENTATIVI = 3
PAUSA = timedelta(minutes=50)          # le sveglie arrivano a distanza di circa un'ora: un po' di margine


def gh(*args, json_out=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}...: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout) if json_out else r.stdout


RAMO_STATO = "regista"


def leggi_file(nome):
    """(testo, sha) di un file del ramo regista; ("", None) se manca."""
    r = subprocess.run(["gh", "api", f"repos/{REPO}/contents/{nome}?ref={RAMO_STATO}"], capture_output=True, text=True)
    if r.returncode != 0:
        return "", None
    d = json.loads(r.stdout)
    return base64.b64decode(d["content"]).decode(), d["sha"]


def scrivi_file(nome, testo, sha, messaggio):
    args = ["api", "-X", "PUT", f"repos/{REPO}/contents/{nome}", "-f", f"message={messaggio}", "-f", f"branch={RAMO_STATO}",
            "-f", "content=" + base64.b64encode(testo.encode()).decode()]
    if sha:
        args += ["-f", f"sha={sha}"]
    gh(*args, json_out=False)


def stato_attuale(adesso):
    """Fotografia per la pagina del sito: per ogni run normale l'ultimo ciclo pubblicato e il run in corso."""
    ufficiali = []
    for wf, (ramo, _) in PREVISIONI.items():
        try:
            nomi = sorted(x["name"] for x in gh("api", f"repos/{REPO}/contents/archivio?ref={ramo}") if x["name"].isdigit())
        except RuntimeError:
            nomi = []
        runs = run_del_workflow(wf)
        attivi = [r for r in runs if r["status"] in ATTIVI]
        finiti = [r for r in runs if r["status"] == "completed"]
        ufficiali.append({
            "workflow": wf, "ultimo_pubblicato": nomi[-1] if nomi else None,
            "in_corso": ({"stato": attivi[0]["status"], "da": attivi[0]["createdAt"], "id": attivi[0]["databaseId"]} if attivi else None),
            "ultimo_esito": ({"esito": finiti[0]["conclusion"], "da": finiti[0]["createdAt"]} if finiti else None)})
    prove = gh("run", "list", "-R", REPO, "--workflow", "sperimentale.yml", "-L", "100", "--json", "displayTitle,status,createdAt,conclusion")
    return {"aggiornato": adesso.strftime("%Y-%m-%dT%H:%M:%SZ"), "ufficiali": ufficiali,
            "prove": [{"titolo": r["displayTitle"], "stato": r["status"], "da": r["createdAt"], "esito": r["conclusion"]}
                      for r in prove if r["status"] in ATTIVI or r["status"] == "completed"][:60]}


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

    lanciate_ora = 0                 # la lista dei run di GitHub puo' non mostrare subito quelli appena lanciati
    testo_lanciate, sha_lanciate = leggi_file("lanciate.txt")
    memoria = {r.strip().lower() for r in testo_lanciate.splitlines() if r.strip()}
    nuove = []
    for prova in PROVE:
        if adesso.date().isoformat() > prova["fino_a"]:
            continue
        ritardi = prova.get("ritardi") or PREVISIONI[prova["workflow"]][1]
        cand = [(g + timedelta(hours=o) + timedelta(hours=h, minutes=m), g + timedelta(hours=o))
                for g in (oggi - timedelta(days=1), oggi) for o, (h, m) in ritardi.items()]
        cand = [x for x in cand if x[0] <= adesso < x[0] + FINESTRA]
        if not cand:
            continue
        c = max(cand)[1].strftime("%Y%m%d%H")
        if c < prova["dal_ciclo"]:
            continue
        try:
            elenco = gh("run", "list", "-R", REPO, "--workflow", "sperimentale.yml", "-L", "100", "--json", "displayTitle,status")
            fatte = {r["displayTitle"].lower() for r in elenco} | memoria
            in_corso = sum(r["status"] in ATTIVI for r in elenco) + lanciate_ora
            for v in prova["varianti"]:
                for f in prova.get("fisiche") or ("",):
                    # stesso titolo che sperimentale.yml da' al run (run-name): se c'e' gia', anche fallito, non si rilancia
                    titolo = f"PROVA {prova['zona']} {prova['pilota']} {v}{' ' + f if f else ''} {c}"
                    if titolo.lower() in fatte:
                        continue
                    if in_corso >= MAX_PROVE:
                        print(f"  sperimentale.yml: rimando {titolo} ({in_corso} prove gia' in coda o in corso)")
                        continue
                    in_corso += 1; lanciate_ora += 1
                    print(f"  sperimentale.yml: LANCIO {titolo}")
                    if not a.prova:
                        nuove.append(titolo.lower()); memoria.add(titolo.lower())
                        gh("workflow", "run", "sperimentale.yml", "-R", REPO, "--ref", "main", "-f", f"zona={prova['zona']}",
                           "-f", f"pilota={prova['pilota']}", "-f", f"ciclo={c}", "-f", f"nudging={v}",
                           "-f", f"controllo={prova['controllo']}", "-f", f"fisica={f}", json_out=False)
        except RuntimeError as e:
            print(f"  !! sperimentale.yml: {e}")
    if nuove and not a.prova:
        try:
            scrivi_file("lanciate.txt", "\n".join(sorted(memoria)) + "\n", sha_lanciate, f"{len(nuove)} prove lanciate")
        except RuntimeError as e:
            print(f"  !! non riesco a salvare l'elenco delle prove lanciate: {e}")
    if not a.prova:
        try:
            scrivi_file("stato.json", json.dumps(stato_attuale(adesso), ensure_ascii=False, indent=1), leggi_file("stato.json")[1],
                        "stato dei run")
        except RuntimeError as e:
            print(f"  !! stato.json: {e}")

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
