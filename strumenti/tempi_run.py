#!/usr/bin/env python3
"""
tempi_run.py - quanto durano i run, per tipo e per macchina (x86 contro ARM), dai run di GitHub ancora presenti.

  tempi_run.py [--giorni 3]

Per ogni run finito con successo (workflow ufficiali e prove) somma, sui job "previsione", lo step "WRF" (il calcolo vero) e
prende la durata del run intero (comprende le attese in coda); raggruppa per tipo e macchina e stampa mediana e numero di run.
La macchina si deduce dal titolo del run o dalla data del passaggio ad ARM (ARM_DAL).
Serve `gh` con accesso in lettura al repository. Non cancellare i run vecchi se si vuole il confronto.
"""
import argparse
import json
import statistics
import subprocess
from datetime import datetime, timedelta, timezone

REPO = "Superbug87/wrf-meteovelletri"
ARM_DAL = "2026-10-19T19:25:00Z"                      # da qui i run usano ARM (salvo i run con "ubuntu-22.04" nel titolo)


def gh(*a):
    r = subprocess.run(["gh", *a], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(r.stderr[:300])
    return json.loads(r.stdout)


def t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--giorni", type=int, default=3)
    a = ap.parse_args()
    da = datetime.now(timezone.utc) - timedelta(days=a.giorni)
    runs = [r for r in gh("run", "list", "-R", REPO, "-L", "100", "--json", "databaseId,workflowName,displayTitle,conclusion,status,createdAt,updatedAt")
            if r["status"] == "completed" and r["conclusion"] == "success" and t(r["createdAt"]) >= da
            and r["workflowName"].startswith(("UFFICIALE", "PROVE", "Castelli", "Lazio", "Prova sperimentale"))]
    gruppi = {}
    for r in runs:
        jobs = gh("run", "view", str(r["databaseId"]), "-R", REPO, "--json", "jobs")["jobs"]
        wrf = 0.0
        for j in jobs:
            if "previsione" not in j["name"]:
                continue
            for s in j["steps"]:
                if s["name"] == "WRF" and s.get("startedAt") and s.get("completedAt"):
                    wrf += (t(s["completedAt"]) - t(s["startedAt"])).total_seconds()
        arm = "ubuntu-22.04-arm" in r["displayTitle"] or (t(r["createdAt"]) >= t(ARM_DAL) and "ubuntu-22.04" not in r["displayTitle"])
        # nome del tipo di run senza il ciclo e senza la macchina
        tipo = " ".join(w for w in r["displayTitle"].split() if not w.isdigit() and not w.startswith("ubuntu"))
        tot = (t(r["updatedAt"]) - t(r["createdAt"])).total_seconds()
        gruppi.setdefault((tipo, "ARM" if arm else "x86"), []).append((wrf / 60, tot / 60))
    print(f"{'Tipo di run':58s} {'macchina':8s} {'n':>2s} {'calcolo WRF':>12s} {'run intero':>11s}")
    for (tipo, mac), v in sorted(gruppi.items()):
        print(f"{tipo[:58]:58s} {mac:8s} {len(v):2d} {statistics.median(x[0] for x in v):9.0f} min {statistics.median(x[1] for x in v):8.0f} min")


if __name__ == "__main__":
    main()
