#!/usr/bin/env bash
# Carica sul sito (FTP) le pagine della cartella sito/. Le credenziali arrivano dall'ambiente (segreti di GitHub):
#   FTP_HOST (es. ftp.pyquake.altervista.org), FTP_USER, FTP_PASSWORD, FTP_CARTELLA (es. 5archi; vuota = radice)
# Uso: carica_sito.sh FILE...
# Con TLS, se il server lo offre (--ssl: se non lo offre prosegue in chiaro). Nessuna credenziale finisce nel log.
set -euo pipefail
if [ -z "${FTP_HOST:-}" ] || [ -z "${FTP_USER:-}" ] || [ -z "${FTP_PASSWORD:-}" ]; then
  echo "::notice::Segreti FTP non impostati (FTP_HOST, FTP_USER, FTP_PASSWORD): niente caricamento sul sito."
  exit 0
fi
CARTELLA=${FTP_CARTELLA:-}; CARTELLA=${CARTELLA#/}; CARTELLA=${CARTELLA%/}
BASE="ftp://${FTP_HOST}${FTP_PORTA:+:$FTP_PORTA}/${CARTELLA:+$CARTELLA/}"
carica() {   # carica FILE [opzioni di curl]
  local f=$1; shift
  curl --fail --silent --show-error --ssl --ftp-create-dirs --connect-timeout 30 --max-time 300 "$@" \
       --user "${FTP_USER}:${FTP_PASSWORD}" -T "$f" "${BASE}$(basename "$f")"
}
for f in "$@"; do
  rc=0; carica "$f" || rc=$?
  if [ "$rc" = 60 ]; then
    # Capita sugli hosting condivisi: il certificato e' intestato a un nome diverso da quello del server FTP.
    echo "::warning::Il certificato del server FTP non corrisponde al nome '${FTP_HOST}': ecco a chi e' intestato."
    openssl s_client -connect "${FTP_HOST}:${FTP_PORTA:-21}" -starttls ftp </dev/null 2>/dev/null \
      | openssl x509 -noout -subject -ext subjectAltName 2>/dev/null || true
    echo "::warning::Carico lo stesso con TLS ma senza verificare l'identita' del server (la password non viaggia in chiaro)."
    rc=0; carica "$f" --insecure || rc=$?
  fi
  if [ "$rc" = 67 ]; then       # accesso negato: il controllo piu' comune e' uno spazio o un "a capo" copiato coi segreti
    for v in FTP_HOST FTP_USER FTP_PASSWORD FTP_CARTELLA; do
      if [[ "${!v}" =~ [[:space:]] ]]; then echo "::warning::$v contiene spazi o a-capo: rimetti il segreto senza."; fi
    done
    echo "::warning::Il server rifiuta utente o password (FTP_USER, FTP_PASSWORD). Per Altervista l'utente e' di solito il nome del sito."
  fi
  if [ "$rc" != 0 ]; then
    echo "!! Caricamento di $(basename "$f") non riuscito (controlla host, utente, password e cartella)"; exit 1
  fi
  echo "caricato: $(basename "$f") -> ${CARTELLA:-/}"
done
