#!/usr/bin/env bash
# Collaudo di tutto quello che si puo' provare senza far girare WRF (pochi minuti, nessuna chiave).
# Lo lancia GitHub a ogni modifica (.github/workflows/prova.yml); si puo' lanciare anche a mano:
#   bash wrf/prove.sh [CARTELLA]        (default: una cartella temporanea)
# Con PROVA_RADAR=1 prova anche il radar vero (serve internet).
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
RADICE=$(dirname "$QUI")
T=${1:-$(mktemp -d)}
mkdir -p "$T"
cd "$RADICE"
passo() { echo; echo "== $*"; }

passo "sintassi Python e nomi non definiti"
python3 -m py_compile wrf/*.py previsioni_lazio.py
python3 -m pyflakes wrf/*.py previsioni_lazio.py

passo "script bash"
shellcheck -S warning wrf/*.sh

passo "compilazione a tappe: avanzamento, tempo massimo e guardiano (comandi inventati)"
bash wrf/prova_costruzione.sh "$T/costruzione"

passo "domini e namelist (riletti con f90nml)"
python3 wrf/dominio.py
python3 - <<'EOF'
import subprocess, f90nml
def nml(*a):
    return f90nml.reads(subprocess.check_output(["python3", "wrf/config.py", *a], text=True))
for pil, prefisso, secondi in (("ecmwf", "ECMWF", 10800), ("icon2i", "ICON2I", 21600)):
    w = nml("wps", "--ciclo", "2026101600", "--ore", "48", "--sst", "--pilota", pil)
    assert w["metgrid"]["fg_name"] == [prefisso, "SST"], w["metgrid"]["fg_name"]
    assert w["share"]["interval_seconds"] == secondi, (pil, w["share"]["interval_seconds"])
fine = None
for da, a in ((0, 16), (16, 32), (32, 48)):
    n = nml("input", "--ciclo", "2026101600", "--da", str(da), "--a", str(a), "--obs-minuti", "530")
    tc = n["time_control"]
    assert tc["restart"] == (da > 0) and tc["run_hours"] == a - da and tc["restart_interval"] == (a - da) * 60
    inizio = (tc["start_day"][0], tc["start_hour"][0])
    assert fine is None or inizio == fine, "un segmento non riparte dove finisce il precedente"
    fine = (tc["end_day"][0], tc["end_hour"][0])
    assert n["fdda"]["fdda_end"][0] == 530.0
import importlib.util
sp = importlib.util.spec_from_file_location("config", "wrf/config.py"); cfg = importlib.util.module_from_spec(sp); sp.loader.exec_module(cfg)
for secondi in (16 * 3600, 3 * 3600, 3600):   # segmento, intervallo dei bordi, uscite orarie
    assert secondi % cfg.TIME_STEP == 0, f"il passo di {cfg.TIME_STEP} s non divide {secondi} s"
assert cfg.TIME_STEP % 9 == 0, "i nidi (rapporto 3 poi 3) vogliono un passo intero"
print("namelist ok")
EOF

passo "le mappe stanno dentro il dominio a 2 km (almeno 15 km dal bordo) e il Lazio sta dentro le mappe"
python3 - <<'EOF'
import sys
sys.path.insert(0, "wrf"); sys.path.insert(0, ".")
import dominio as D
import previsioni_lazio as pc
from pyproj import Proj, Transformer
lcc = Proj(proj="lcc", lat_1=D.TRUELAT1, lat_2=D.TRUELAT2, lat_0=D.LAT0, lon_0=D.LON0, R=6370000)
fw = Transformer.from_proj("EPSG:4326", lcc, always_xy=True)
semi = D.celle(2) * D.DX[2] / 2000.0
for lat in (pc.LAT_MIN, pc.LAT_MAX):
    for lon in (pc.LON_MIN, pc.LON_MAX):
        x, y = fw.transform(lon, lat)
        assert max(abs(x), abs(y)) / 1000 <= semi - 15, (lat, lon, x / 1000, y / 1000, semi)
# estremi del Lazio (gradi): sud, nord, ovest, est
assert pc.LAT_MIN < 40.78 and pc.LAT_MAX > 42.85 and pc.LON_MIN < 11.45 and pc.LON_MAX > 14.0
print("mappe e dominio ok")
EOF

passo "confini del Lazio e delle province, laghi veri: le città del Lazio dentro, quelle vicine fuori, cinque province, laghi con superficie e centro giusti"
python3 - <<'EOF'
import json, sys
sys.path.insert(0, ".")
import numpy as np
from matplotlib.path import Path
import previsioni_lazio as pc
d = json.load(open("dati/confini_lazio.json"))
assert d["regione"] and "CC BY" in d["fonte"], "file dei confini incompleto"
nel = sorted(p["nome"] for p in d["province"] if p["regione"] == "Lazio")
assert nel == ["Frosinone", "Latina", "Rieti", "Roma", "Viterbo"], nel
anelli = [Path(np.array(a)) for a in d["regione"]]
def nel_lazio(la, lo):
    return any(a.contains_point((lo, la)) for a in anelli)
for nome, la, lo in pc.PAESI + [pc.STAZIONE]:
    assert nel_lazio(la, lo), nome + " dovrebbe stare dentro il Lazio"
for nome, la, lo in (("Siena", 43.32, 11.33), ("L'Aquila", 42.35, 13.40), ("Caserta", 41.07, 14.33), ("Terni", 42.56, 12.64)):
    assert not nel_lazio(la, lo), nome + " dovrebbe stare fuori dal Lazio"
# il disegno deve usare le linee: confine spesso, province sottili
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
pc.disegna_confini(ax)
spessori = sorted({round(l.get_linewidth(), 1) for l in ax.lines})
assert spessori == [0.7, 1.0, 2.6], spessori
assert sum(1 for l in ax.lines if round(l.get_linewidth(), 1) == 2.6) == 1, "il confine spesso è solo la terraferma"
print("confini ok:", len(d["regione"]), "anelli del Lazio,", len(d["province"]), "province, linee", spessori)
# laghi veri: Bolsena, Bracciano, Vico e Albano contengono il loro centro e hanno la superficie giusta (km²)
laghi = json.load(open("dati/laghi_lazio.json"))
assert "OpenStreetMap" in laghi["fonte"]
attesi = {"Lago di Bolsena": ((42.595, 11.935), 100, 130), "Lago di Bracciano": ((42.122, 12.228), 50, 65),
          "Lago di Vico": ((42.320, 12.170), 10, 14), "Lago Albano": ((41.748, 12.667), 5, 7)}
for l in laghi["laghi"]:
    if l["nome"] in attesi:
        (la, lo), amin, amax = attesi.pop(l["nome"])
        assert amin <= l["area_km2"] <= amax, (l["nome"], l["area_km2"])
        assert any(Path(np.array(a)).contains_point((lo, la)) for a in l["esterni"]), l["nome"] + ": centro fuori dal contorno"
assert not attesi, "laghi mancanti: " + ", ".join(attesi)
fig, ax = plt.subplots()
pc.disegna_laghi(ax)
assert len(ax.patches) == len(laghi["laghi"]), (len(ax.patches), len(laghi["laghi"]))
print("laghi ok:", len(laghi["laghi"]), "laghi veri (niente cerchi)")
# vento: linee di flusso continue con tante freccette (prima 100 frecce corte), il verso è quello del vento
lon_f = np.linspace(pc.LON_MIN, pc.LON_MAX, 80); lat_f = np.linspace(pc.LAT_MIN, pc.LAT_MAX, 80)
LON, LAT = np.meshgrid(lon_f, lat_f)
u = 5.0 + 3 * np.sin((LAT - 41) * 3) + 0 * LON
v = 2.0 + 3 * np.cos((LON - 12) * 3)
fig, ax = plt.subplots()
ax.set_xlim(pc.LON_MIN, pc.LON_MAX); ax.set_ylim(pc.LAT_MIN, pc.LAT_MAX)
pc.disegna_flusso(ax, lon_f, lat_f, u, v, densita=1.8)
tri = [c for c in ax.collections if c.__class__.__name__ == "PolyCollection"]
linee = [c for c in ax.collections if c.__class__.__name__ == "LineCollection"]
n_tri = sum(len(c.get_paths()) for c in tri)
assert n_tri >= 400, n_tri
assert sum(len(c.get_paths()) for c in linee) >= 60
# il verso: la punta di ogni freccetta guarda dove soffia il vento (u > 0 qui: verso est)
punte = [p.vertices[0][0] - p.vertices[1:3, 0].mean() for p in tri[0].get_paths()]
assert np.mean(np.array(punte) > 0) > 0.95, "le freccette non guardano verso il vento"
print("vento ok:", n_tri, "freccette lungo", sum(len(c.get_paths()) for c in linee), "linee di flusso")
EOF

passo "file met_em fatti come quelli di metgrid 4.7 (livelli letti dalla dimensione e dall'attributo)"
python3 - "$T" <<'EOF'
import subprocess, sys
from netCDF4 import Dataset
t = sys.argv[1]
def crea(nome, nlev=34, nsoil=4):
    with Dataset(f"{t}/{nome}", "w") as ds:
        ds.createDimension("Time", None)
        ds.createDimension("num_metgrid_levels", nlev)
        ds.createDimension("west_east", 3)
        ds.setncattr("TITLE", "OUTPUT FROM METGRID V4.7.0")
        if nsoil is not None:
            ds.setncattr("NUM_METGRID_SOIL_LEVELS", nsoil)
def info(nome):
    return subprocess.run(["python3", "wrf/config.py", "metgrid-info", f"{t}/{nome}"], capture_output=True, text=True)
crea("met_ok.nc")
r = info("met_ok.nc")
assert r.stdout.split() == ["34", "4"], (r.stdout, r.stderr)
crea("met_zero.nc", nsoil=0)
r = info("met_zero.nc")
assert r.returncode != 0 and "livelli di suolo" in r.stderr, r
crea("met_senza.nc", nsoil=None)
r = info("met_senza.nc")
assert r.returncode != 0 and "NUM_METGRID_SOIL_LEVELS" in r.stderr, r
print("met_em ok")
EOF

passo "file WRF e geo_em inventati"
python3 wrf/prova_sintetica.py --out "$T/sint" --geo "$T/geo"

passo "tutte le stazioni: ricerca a riquadri senza perdite, controllo di qualità con guasti inventati, rilettura con i formati di WRF"
python3 wrf/stazioni_wrf.py --prova --ciclo 2026101600 --geo "$T/geo" --out "$T/obs"
gfortran wrf/leggi_obs.f90 -o "$T/leggi_obs"
for f in "$T"/obs/OBS_DOMAIN101 "$T"/obs/OBS_DOMAIN201 "$T"/obs/OBS_DOMAIN301; do "$T/leggi_obs" "$f"; done

passo "ricerca e lettura delle stazioni usate dalle mappe (API inventata, tutte le stazioni, in parallelo)"
python3 - "$T" <<'EOF'
import sys
from datetime import datetime, timezone
sys.path.insert(0, "wrf"); sys.path.insert(0, ".")
import previsioni_lazio as pc
import stazioni_wrf as sw
dom = sw.carica_domini(sys.argv[1] + "/geo")
api = sw.ApiFinta(dom, datetime.now(timezone.utc))
sw.wu_get = api
trovate = pc.cerca_stazioni("finta")
dentro = sum(1 for la, lo in api.pos if pc.LAT_MIN <= la <= pc.LAT_MAX and pc.LON_MIN <= lo <= pc.LON_MAX)
assert dentro > 500 and len(trovate) >= 0.995 * dentro, (dentro, len(trovate))
def finto(url, params, validita):
    if params["stationId"].endswith("7"):
        raise KeyError("niente")
    return {"observations": [{"lat": 41.7, "lon": 12.7, "obsTimeLocal": "2026-10-16 12:00:00", "metric": {"elev": 100, "temp": 18.2, "dewpt": 9.0}}]}
pc._get_json = finto
ids = [f"IF{i:05d}" for i in range(200)]
oss = pc.leggi_stazioni("finta", ids)
assert len(oss) == sum(1 for i in ids if not i.endswith("7")), len(oss)
print(f"mappe: {len(trovate)} stazioni trovate su {dentro} nel riquadro, {len(oss)} lette su {len(ids)}")
EOF

passo "temperatura del mare: formato intermedio di WPS riletto"
python3 - "$T" <<'EOF'
import struct, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, "wrf")
import scarica_sst as s
lat, lon = np.linspace(39, 44.5, 23), np.linspace(9, 16.5, 31)
k = np.where(np.random.default_rng(0).random((23, 31)) > 0.3, 290.0, -1.0e30)
p = Path(sys.argv[1]) / "SST:2026-10-16_00"
s.scrivi_intermedio(p, "2026-10-16_00:00:00", lat, lon, k)
b = p.read_bytes(); pos = 0
def rec():
    global pos
    n = struct.unpack(">i", b[pos:pos + 4])[0]; r = b[pos + 4:pos + 4 + n]
    assert struct.unpack(">i", b[pos + 4 + n:pos + 8 + n])[0] == n; pos += n + 8; return r
assert struct.unpack(">i", rec())[0] == 5
h = rec(); assert h[:19] == b"2026-10-16_00:00:00" and h[60:69].strip() == b"SST"
nx, ny, iproj = struct.unpack(">iii", h[-12:]); assert (nx, ny, iproj) == (31, 23, 0)
rec(); rec()
z = np.frombuffer(rec(), ">f4").reshape(ny, nx); assert np.allclose(z, k) and pos == len(b)
print("SST ok")
EOF

passo "grandezze derivate (wrf/diagnostiche.py): casi con risposta nota"
python3 - <<'EOF_D'
import sys
sys.path.insert(0, "wrf")
import numpy as np
import diagnostiche as d
import config
# livelli tipo WRF: 40 livelli, atmosfera standard
z = (np.linspace(0, 1, 40) ** 1.8 * 16000.0)[None, :] + 100.0
tk = 288.15 - 0.0065 * np.minimum(z, 11000.0) + 0.0 * z
p = 101325.0 * (tk / 288.15) ** 5.2559
p[:, z[0] > 11000] = p[0, np.argmax(z[0] > 11000) - 1] * np.exp(-(z[0, z[0] > 11000] - 11000) / 6300)
# T a 850: attesa vicino a 5 °C in atmosfera standard (1457 m)
t850 = d.a_pressione(p, tk, 85000.0, t_suolo=tk[:, 0])[0] - 273.15
assert abs(t850 - 5.0) < 1.0, t850
# livello sotto il suolo: estrapolazione, più caldo del primo livello se il suolo sta più in alto
p_alto = p.copy() * 0.80
t_est = d.a_pressione(p_alto, tk, 85000.0, t_suolo=tk[:, 0])[0]
assert t_est > tk[0, 0], t_est
# zero termico: standard a 0 °C = 15,15/0,0065 = 2331 m s.l.m.
zt = d.zero_termico(tk, z)[0]
assert abs(zt - 2331 - 0) < 120, zt
assert d.zero_termico(tk + 80, z)[0] == z[0, -1]
assert d.zero_termico(tk - 30, z)[0] < z[0, 0]                      # già sotto zero a terra: sotto il suolo, ma non negativo
# nuvole: strato basso al 100 % -> totale 1; due strati al 50 % in alto -> totale 0,75 (casuale), non 1
cf = np.zeros_like(z); psfc = p[:, 0]
cf[0, 5] = 1.0
n = d.nuvolosita(cf, p, psfc)
assert abs(n["nubi_tot"][0] - 100) < 1e-6 and abs(n["nubi_bassa"][0] - 100) < 1e-6
assert n["nubi_media"][0] < 1e-6 and n["nubi_alta"][0] < 1e-6, n
cf[:] = 0; cf[0, 30] = 0.5; cf[0, 32] = 0.5
n = d.nuvolosita(cf, p, psfc)
assert 50 < n["nubi_tot"][0] <= 76 and n["nubi_alta"][0] > 40 and n["nubi_bassa"][0] < 1e-6, n
# vento che ruota in senso orario con la quota (profilo tipico da supercella): SRH > 0; ruotato al contrario: SRH < 0
zagl = (z - 100.0)
def profilo(segno):
    ang = segno * np.clip(zagl / 6000.0, 0, 1) * np.pi / 2           # 90° di rotazione in 6 km
    vel = 8.0 + 20.0 * np.clip(zagl / 6000.0, 0, 1)
    return -vel * np.sin(ang + np.pi), -vel * np.cos(ang + np.pi)
risultati = {}
for segno in (1, -1):
    u, v = profilo(segno)
    U, V = d.vento_a_quote(u, v, zagl, u[:, 0], v[:, 0])
    risultati[segno] = d.shear_srh(U, V)
    assert risultati[segno]["shear_06"][0] > 15 and risultati[segno]["shear_01"][0] < risultati[segno]["shear_06"][0]
# il vento che gira in senso orario con la quota (ingrediente delle supercelle) da' SRH grande e positiva;
# in senso antiorario il "right-mover" non trova elicita': quasi nulla o negativa
assert risultati[1]["srh_03"][0] > 100 and risultati[1]["srh_01"][0] > 30, risultati[1]
assert risultati[-1]["srh_03"][0] < 20 and risultati[-1]["srh_01"][0] < 5, risultati[-1]
# vento uniforme: shear 0 e SRH 0
u = np.full_like(z, 10.0); v = np.zeros_like(z)
r = d.shear_srh(*d.vento_a_quote(u, v, zagl, u[:, 0], v[:, 0]))
assert r["shear_06"][0] < 1e-6 and abs(r["srh_03"][0]) < 1e-6
# raffica: aria neutra -> vento + 7,71 u*; neve: 1 mm a -10 °C ~ 1,45 cm, a +5 °C nulla
rf = d.raffica_turbolenta(np.array([5.0]), np.array([0.5]), np.array([800.0]), np.array([0.0]), np.array([285.0]), np.array([1e5]))
assert abs(rf[0] - (5.0 + 7.71 * 0.5)) < 1e-6, rf
assert d.raffica_turbolenta(np.array([5.0]), np.array([0.5]), np.array([1500.0]), np.array([200.0]), np.array([300.0]), np.array([1e5]))[0] > rf[0]
assert abs(d.neve_cm(np.array([1.0]), np.array([263.15]))[0] - 1.45) < 0.1
assert d.neve_cm(np.array([1.0]), np.array([278.15]))[0] == 0
# indici di rischio: 0 senza ingredienti, monotoni
assert d.indice_supercella(np.array([0.0]), np.array([0.0]), np.array([300.0]), np.array([30.0]))[0] == 0
a = d.indice_supercella(np.array([1500.0]), np.array([-20.0]), np.array([150.0]), np.array([20.0]))[0]
b = d.indice_supercella(np.array([3000.0]), np.array([-20.0]), np.array([300.0]), np.array([30.0]))[0]
assert 0 < a < b, (a, b)
# il passo di calcolo divide i tempi dei segmenti e delle uscite
for s in (57600, 10800, 3600):
    assert s % config.TIME_STEP == 0, (s, config.TIME_STEP)
print("diagnostiche ok")
EOF_D

passo "mappe da wrfout inventati"
python3 wrf/postprocess.py --uscite "$T/sint" --out "$T/mappe" --ciclo 2026101600 --senza-stazioni --da-ora 2026-10-16T08:00
[ "$(find "$T/mappe" -maxdepth 1 -name '*.png' | wc -l)" -ge 9 ] || { echo "mancano mappe"; exit 1; }

passo "mappe con dati inventati (previsioni_lazio.py --simula, senza rete)"
python3 previsioni_lazio.py --simula --inizio 2026-10-16T08:00 --out "$T/simula" --senza-stazioni
python3 previsioni_lazio.py --simula --inizio 2026-10-16T08:00 --ora 30 --out "$T/simula" --senza-stazioni
[ "$(find "$T/simula" -maxdepth 1 -name '*.png' | wc -l)" -ge 11 ] || { echo "mancano mappe nella simulazione"; exit 1; }

passo "immagini orarie: una per ora e variabile, con indice"
python3 - "$T/mappe/orarie" <<'EOF2'
import json, sys
from pathlib import Path
d = Path(sys.argv[1]); idx = json.loads((d / "indice.json").read_text())
assert idx["ciclo"] == "2026101600" and idx["run"] == "2026-10-16T00:00:00Z", idx
assert len(idx["variabili"]) >= 9 and len(idx["ore"]) >= 24, (len(idx["variabili"]), len(idx["ore"]))
for v in idx["variabili"]:
    for t in idx["ore"]:
        f = d / idx["ciclo"] / v["id"] / (t[:13].replace("-", "").replace("T", "") + ".png")
        assert f.stat().st_size > 5000, f
print("orarie ok:", len(idx["variabili"]), "variabili x", len(idx["ore"]), "ore")
EOF2

passo "pubblicazione nel ramo risultati (su un repository finto): un solo commit, 14 cicli di immagini orarie al massimo"
R="$T/pubblica"; rm -rf "$R"; mkdir -p "$R"; git init -q --bare "$R/remoto.git"
for g in $(seq 1 9); do for h in 00 12; do
  c="2026100${g}${h}"
  rm -rf "$R/img"; mkdir -p "$R/img/orarie/$c/temperatura_2m"; echo x > "$R/img/01_t.png"
  echo x > "$R/img/orarie/$c/temperatura_2m/a.png"; echo "{\"ciclo\": \"$c\"}" > "$R/img/orarie/indice.json"
  cp "$R/img/orarie/indice.json" "$R/img/orarie/$c/indice.json"
  REPO_URL="$R/remoto.git" GITHUB_REPOSITORY=u/r bash wrf/pubblica.sh "$R/img" "$c" 2>/dev/null >/dev/null
done; done
git clone -q -b risultati "$R/remoto.git" "$R/vedi"
[ "$(git -C "$R/vedi" rev-list --count HEAD)" = 1 ] || { echo "la storia si accumula"; exit 1; }
[ "$(ls -d "$R"/vedi/orarie/*/ | wc -l)" = 14 ] || { echo "cicli orari sbagliati"; exit 1; }
[ -d "$R/vedi/orarie/2026100900" ] && [ ! -d "$R/vedi/orarie/2026100100" ] || { echo "cicli orari sbagliati: non tolgo i piu' vecchi"; exit 1; }
python3 -c "import json; assert len(json.load(open('$R/vedi/orarie/cicli.json'))['cicli']) == 14"

passo "pagina del sito: sintassi del JavaScript"
python3 - "$T/pagina.js" <<'EOF3'
import re, sys
html = open("sito/previsioni-wrf-lazio.html", encoding="utf-8").read()
open(sys.argv[1], "w", encoding="utf-8").write(re.search(r"<script>(.*?)</script>", html, re.S).group(1))
EOF3
if command -v node >/dev/null; then node --check "$T/pagina.js" && echo "pagina ok"; else echo "node non c'e': salto"; fi

passo "movimento della pioggia su un caso inventato"
python3 - <<'EOF'
import sys
sys.path.insert(0, "wrf")
import numpy as np
from scipy.ndimage import gaussian_filter, shift
import radar
rng = np.random.default_rng(0)
a = np.zeros((500, 500), "float32")
for _ in range(40):
    y, x = rng.integers(100, 400, 2)
    a[y - 6:y + 6, x - 8:x + 8] = rng.uniform(1, 20)
a = gaussian_filter(a, 3) * 3
v, _ = radar.movimento(a, shift(a, (6, 18), order=1), 6)
assert np.allclose(v, (3, 1), atol=0.2), v
v, _ = radar.movimento(a, shift(a, (1, 2), order=1), 6)
assert not v.any(), v
r = radar.estrapola(a, np.broadcast_to(np.float32([3, 1]), a.shape + (2,)).copy(), 6)
assert np.abs(r[150:350, 150:350] - shift(a, (6, 18), order=1)[150:350, 150:350]).max() < 0.5
print("movimento ok")
EOF

if [ "${PROVA_RADAR:-0}" = 1 ]; then
  passo "radar vero (Radar-DPC) con WRF inventato"
  python3 wrf/pioggia_radar.py --uscite "$T/sint" --out "$T/mappe" --ciclo 2026101600
fi

echo; echo "TUTTO OK  (file in $T)"
