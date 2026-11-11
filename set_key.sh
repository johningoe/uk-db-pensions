#!/bin/zsh
# Saves a Companies House API key to .env without echoing it or putting it in shell history.
cd "${0:A:h}" || exit 1
printf 'Paste your Companies House REST API key (input is hidden), then press Enter: '
read -rs KEY
echo
if [ -z "$KEY" ]; then
  echo "No key entered - nothing saved."
  exit 1
fi
umask 077
printf 'COMPANIES_HOUSE_API_KEY=%s\n' "$KEY" > .env
echo "Saved to .env (readable only by you). You can close this tab."
