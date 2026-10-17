#!/usr/bin/env bash
# Pacchetti di sistema per compilare e far girare WRF/WPS su Ubuntu 22.04 (runner di GitHub).
# JasPer (per il GRIB2) non è più nei pacchetti Ubuntu: lo compila costruisci.sh.
set -euo pipefail
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  gfortran gcc g++ make m4 csh perl file \
  mpich libmpich-dev \
  libnetcdf-dev libnetcdff-dev netcdf-bin \
  libpng-dev zlib1g-dev cmake git
