#!/usr/bin/env python3
"""Genera i namelist di WPS e WRF per i tre domini annidati sul Lazio (18, 6 e 2 km: vedi dominio.py).

  config.py wps   --ciclo 2026101600 --ore 48 [--pilota icon2i|ecmwf|gfs] [--geog PERCORSO]   > namelist.wps
  config.py input --ciclo 2026101600 --da 0 --a 16 [--nlev 34 --nsoil 4] > namelist.input
  config.py metgrid-info met_em.d01.*.nc          stampa "livelli_metgrid livelli_suolo"

`--da` e `--a` sono ore dall'inizio del ciclo; con --da > 0 il run riprende da un restart.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dominio as D  # noqa: E402

# s tra i file dei bordi: ICON-2I si scarica ogni 6 ore (ogni file pesa 300-450 MB), ECMWF e GFS ogni 3 ore.
# Il modello di partenza arriva dalla variabile d'ambiente PILOTA (la imposta il workflow; esegui.sh la esporta).
def intervallo(pilota=None):
    p = (pilota or os.environ.get("PILOTA") or "icon2i").lower()
    return 21600 if p == "icon2i" else 10800

# s sul dominio 1 (18 km); 30 s e 10 s nei nidi (parent_time_step_ratio 1:3:3), circa 5 s per km di passo griglia.
# Deve dividere esattamente la durata di un segmento (16 h = 57600 s), l'intervallo dei bordi (3 h) e le uscite
# orarie: con 54 s (nei Castelli, a 9 km) l'ultimo passo di d01 sforava le 16:00 e WRF non scriveva i restart di
# d01 e d02. 90 s divide 57600 (640 passi), 10800 (120) e 3600 (40); e 90 / 9 = 10 s è intero.
TIME_STEP = 90
# le osservazioni delle stazioni si rileggono ogni 4 passi del dominio 1 (360 s, 6 minuti di simulazione)
OBS_INTERVALLO_S = 4 * TIME_STEP


def _ciclo(testo):
    return datetime.strptime(testo, "%Y%m%d%H")


def _data(dt):
    return dt.strftime("%Y-%m-%d_%H:%M:%S")


def _tre(valore, fmt="{}"):
    return ", ".join(fmt.format(valore) for _ in range(3))


def namelist_wps(inizio, fine, geog, prefisso, sst=False):
    starts = D.parent_starts()
    fg = f"'{prefisso}', 'SST'" if sst else f"'{prefisso}'"   # SST: file scritti da scarica_sst.py
    return f"""&share
 wrf_core = 'ARW',
 max_dom = 3,
 start_date = {_tre(_data(inizio), "'{}'")},
 end_date   = {_tre(_data(fine), "'{}'")},
 interval_seconds = {intervallo(prefisso)},
 io_form_geogrid = 2,
/

&geogrid
 parent_id         = 1, 1, 2,
 parent_grid_ratio = {", ".join(map(str, D.RATIO))},
 i_parent_start    = {", ".join(map(str, starts))},
 j_parent_start    = {", ".join(map(str, starts))},
 e_we              = {", ".join(map(str, D.E))},
 e_sn              = {", ".join(map(str, D.E))},
 geog_data_res     = 'default', 'default', 'default',
 dx = {D.DX[0]},
 dy = {D.DX[0]},
 map_proj  = 'lambert',
 ref_lat   = {D.LAT0},
 ref_lon   = {D.LON0},
 truelat1  = {D.TRUELAT1},
 truelat2  = {D.TRUELAT2},
 stand_lon = {D.LON0},
 geog_data_path = '{geog}',
/

&ungrib
 out_format = 'WPS',
 prefix = '{prefisso}',
/

&metgrid
 fg_name = {fg},
 io_form_metgrid = 2,
/
"""


def namelist_input(inizio, fine, riprendi, nlev, nsoil, obs_minuti=None, obs_vento=False):
    ore = int(round((fine - inizio).total_seconds() / 3600))
    starts = D.parent_starts()

    def riga(nome, valori):
        return f" {nome:<26}= {', '.join(str(v) for v in valori)},\n"

    t = ""
    t += "&time_control\n"
    t += " run_days = 0,\n run_hours = %d,\n run_minutes = 0,\n run_seconds = 0,\n" % ore
    for nome, dt in (("start", inizio), ("end", fine)):
        for campo, fmt in (("year", "%Y"), ("month", "%m"), ("day", "%d"), ("hour", "%H")):
            t += riga(f"{nome}_{campo}", [int(dt.strftime(fmt))] * 3)
        t += riga(f"{nome}_minute", [0] * 3)
        t += riga(f"{nome}_second", [0] * 3)
    t += f" interval_seconds = {intervallo()},\n"
    t += riga("input_from_file", [".true."] * 3)
    t += riga("history_interval", [60] * 3)
    t += riga("frames_per_outfile", [1000] * 3)
    t += f" restart = {'.true.' if riprendi else '.false.'},\n"
    t += f" restart_interval = {ore * 60},\n"
    t += " io_form_history = 2,\n io_form_restart = 2,\n io_form_input = 2,\n io_form_boundary = 2,\n"
    t += " history_outname = 'wrfout_d<domain>_<date>',\n"
    # massimi orari (tra cui il vento a 10 m) per stimare le raffiche
    t += " nwp_diagnostics = 1,\n"       # massimi orari: vento a 10 m, corrente ascendente, graupel, elicità
    t += " output_diagnostics = 1,\n auxhist3_outname = 'wrfxtrm_d<domain>_<date>',\n"
    t += riga("auxhist3_interval", [60] * 3)
    t += riga("frames_per_auxhist3", [1000] * 3)
    t += " io_form_auxhist3 = 2,\n debug_level = 0,\n"
    if obs_minuti is not None:                       # le osservazioni si rileggono ogni 4 passi del dominio 1
        t += riga("auxinput11_interval_s", [OBS_INTERVALLO_S] * 3)
        t += riga("auxinput11_end_h", [int(obs_minuti // 60) + 2] * 3)
    t += "/\n\n"

    t += "&domains\n"
    t += f" time_step = {TIME_STEP},\n time_step_fract_num = 0,\n time_step_fract_den = 1,\n max_dom = 3,\n"
    t += riga("e_we", D.E) + riga("e_sn", D.E) + riga("e_vert", [D.E_VERT] * 3)
    t += riga("dx", D.DX) + riga("dy", D.DX)
    t += riga("grid_id", [1, 2, 3]) + riga("parent_id", [0, 1, 2])
    t += riga("i_parent_start", starts) + riga("j_parent_start", starts)
    t += riga("parent_grid_ratio", D.RATIO) + riga("parent_time_step_ratio", D.RATIO)
    t += " feedback = 0,\n smooth_option = 0,\n p_top_requested = 5000,\n"
    t += f" num_metgrid_levels = {nlev},\n num_metgrid_soil_levels = {nsoil},\n"
    t += " interp_type = 2,\n extrap_type = 2,\n t_extrap_type = 2,\n lowest_lev_from_sfc = .false.,\n"
    t += " use_levels_below_ground = .true.,\n use_surface = .true.,\n lagrange_order = 2,\n"
    t += " zap_close_levels = 500,\n force_sfc_in_vinterp = 1,\n"
    t += "/\n\n"

    t += "&physics\n"
    t += riga("mp_physics", [8] * 3)                       # Thompson
    t += riga("ra_lw_physics", [4] * 3) + riga("ra_sw_physics", [4] * 3)   # RRTMG
    t += riga("radt", [18, 6, 6])                           # minuti tra due calcoli della radiazione (RRTMG): 12, 12 e 36 passi; nel dominio a 2 km ogni 6 min invece di 3 (molto meno calcolo, effetto trascurabile)
    t += riga("sf_sfclay_physics", [1] * 3) + riga("sf_surface_physics", [2] * 3)  # Noah
    t += riga("bl_pbl_physics", [1] * 3) + riga("bldt", [0] * 3)           # YSU
    t += riga("cu_physics", [1, 1, 0]) + riga("cudt", [0] * 3)             # Kain-Fritsch a 18 e 6 km, convezione esplicita a 2 km
    t += riga("topo_wind", [1] * 3)
    t += riga("lightning_option", [3, 3, 3])   # LPI in TUTTI i domini: solo nel nido azzera il terreno nella cornice del dominio (provato)
    t += riga("slope_rad", [0] * 3) + riga("topo_shading", [0] * 3)        # servono sotto circa 1-2 km di passo
    t += " isfflx = 1,\n ifsnow = 1,\n icloud = 1,\n surface_input_source = 1,\n"
    t += " num_soil_layers = 4,\n num_land_cat = 21,\n sst_update = 0,\n"
    t += riga("sf_urban_physics", [0] * 3)
    t += "/\n\n"

    t += "&dynamics\n"
    t += " hybrid_opt = 2,\n use_theta_m = 0,\n w_damping = 1,\n"
    t += riga("diff_opt", [1] * 3) + riga("km_opt", [4] * 3)
    t += riga("diff_6th_opt", [0] * 3) + riga("diff_6th_factor", [0.12] * 3)
    t += " base_temp = 290.,\n damp_opt = 3,\n zdamp = 5000., 5000., 5000.,\n dampcoef = 0.2, 0.2, 0.2,\n"
    t += riga("khdif", [0] * 3) + riga("kvdif", [0] * 3)
    t += riga("non_hydrostatic", [".true."] * 3)
    t += riga("moist_adv_opt", [1] * 3) + riga("scalar_adv_opt", [1] * 3)
    t += " gwd_opt = 0,\n/\n\n"

    if obs_minuti is not None:
        # Nudging verso le stazioni fino all'ultima osservazione (fdda_end, minuti dall'inizio del ciclo);
        # dopo il modello prosegue libero. Raggi di influenza (km) piccoli nei domini fini: la quota conta.
        # Il Lazio è montuoso (Appennino fino a 2200 m): raggio di 50 km nel nido fine.
        t += "&fdda\n"
        t += riga("obs_nudge_opt", [1] * 3) + " max_obs = 150000,\n"
        t += riga("fdda_start", ["0."] * 3) + riga("fdda_end", [f"{float(obs_minuti):.1f}"] * 3)
        t += riga("obs_nudge_wind", [1 if obs_vento else 0] * 3) + riga("obs_coef_wind", ["6.E-4"] * 3)
        t += riga("obs_nudge_temp", [1] * 3) + riga("obs_coef_temp", ["6.E-4"] * 3)
        t += riga("obs_nudge_mois", [1] * 3) + riga("obs_coef_mois", ["6.E-4"] * 3)
        t += riga("obs_rinxy", ["200.", "100.", "50."])
        t += " obs_rinsig = 0.1,\n"
        t += riga("obs_twindo", ["0.6666667"] * 3) + " obs_npfi = 10,\n"
        t += riga("obs_ionf", [2] * 3) + " obs_idynin = 0,\n obs_dtramp = 40.,\n"
        t += " obs_prt_max = 10,\n obs_prt_freq = 100, 100, 100,\n"
        t += " obs_ipf_in4dob = .false.,\n obs_ipf_errob = .false.,\n obs_ipf_nudob = .false.,\n obs_ipf_init = .true.,\n"
        t += "/\n\n"

    t += "&bdy_control\n spec_bdy_width = 5,\n spec_zone = 1,\n relax_zone = 4,\n"
    t += riga("specified", [".true.", ".false.", ".false."]) + riga("nested", [".false.", ".true.", ".true."])
    t += "/\n\n&namelist_quilt\n nio_tasks_per_group = 0,\n nio_groups = 1,\n/\n"
    return t


def info_metgrid(percorso):
    """Livelli verticali e livelli di suolo di un file met_em di WPS (letto in WPS 4.7.0, 16 ottobre 2026).
    I livelli verticali sono la dimensione `num_metgrid_levels` (non un attributo: real.exe la legge da li');
    i livelli di suolo sono l'attributo globale `NUM_METGRID_SOIL_LEVELS`, che metgrid scrive sempre ma a 0
    se non trova nessun campo di suolo (ST, SOILT, STC_WPS)."""
    from netCDF4 import Dataset

    with Dataset(percorso) as ds:
        if "num_metgrid_levels" in ds.dimensions:
            nlev = len(ds.dimensions["num_metgrid_levels"])
        elif "NUM_METGRID_LEVELS" in ds.ncattrs():
            nlev = int(ds.getncattr("NUM_METGRID_LEVELS"))
        else:
            sys.exit(f"{percorso}: manca la dimensione num_metgrid_levels (dimensioni: {sorted(ds.dimensions)})")
        if "NUM_METGRID_SOIL_LEVELS" not in ds.ncattrs():
            sys.exit(f"{percorso}: manca l'attributo NUM_METGRID_SOIL_LEVELS (attributi: {sorted(ds.ncattrs())[:40]})")
        nsoil = int(ds.getncattr("NUM_METGRID_SOIL_LEVELS"))
    if nsoil < 1:
        sys.exit(f"{percorso}: metgrid non ha trovato livelli di suolo (NUM_METGRID_SOIL_LEVELS = 0): "
                 "controllare che ungrib scriva ST/SM (Vtable.ECMWF_OD)")
    return nlev, nsoil


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wps")
    w.add_argument("--ciclo", default="2026010100")
    w.add_argument("--ore", type=int, default=24)
    w.add_argument("--geog", default="/percorso/WPS_GEOG")
    w.add_argument("--pilota", choices=["ecmwf", "gfs", "icon2i"], default="icon2i",
                   help="modello globale da cui WRF prende condizioni iniziali e al contorno")
    w.add_argument("--sst", action="store_true", help="metgrid usa anche i file SST:* (temperatura del mare ad alta risoluzione)")
    i = sub.add_parser("input")
    i.add_argument("--ciclo", required=True)
    i.add_argument("--da", type=int, default=0)
    i.add_argument("--a", type=int, required=True)
    i.add_argument("--nlev", type=int, default=34)
    i.add_argument("--nsoil", type=int, default=4)
    i.add_argument("--obs-minuti", type=int, default=None,
                   help="attiva il nudging sulle stazioni fino a questo minuto dall'inizio del ciclo")
    i.add_argument("--obs-vento", action="store_true", help="nudging anche del vento (sconsigliato)")
    m = sub.add_parser("metgrid-info")
    m.add_argument("file")
    a = ap.parse_args()

    D.verifica()
    if a.cmd == "wps":
        c = _ciclo(a.ciclo)
        sys.stdout.write(namelist_wps(c, c + timedelta(hours=a.ore), a.geog, a.pilota.upper(), a.sst))
    elif a.cmd == "input":
        c = _ciclo(a.ciclo)
        if a.a <= a.da:
            sys.exit("--a deve essere maggiore di --da")
        sys.stdout.write(namelist_input(c + timedelta(hours=a.da), c + timedelta(hours=a.a), a.da > 0,
                                        a.nlev, a.nsoil, a.obs_minuti, a.obs_vento))
    else:
        print(*info_metgrid(a.file))


if __name__ == "__main__":
    main()
