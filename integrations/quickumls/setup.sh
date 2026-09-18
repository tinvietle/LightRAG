#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
quickumls_venv="${repo_root}/.venv-quickumls"
nltk_data="${repo_root}/data/nltk_data"

uv venv --python 3.11 "${quickumls_venv}"
uv pip install --python "${quickumls_venv}/bin/python" \
    -r "${repo_root}/integrations/quickumls/requirements.txt"
VIRTUAL_ENV="${quickumls_venv}" \
    "${quickumls_venv}/bin/python" -m spacy download en_core_web_sm
"${quickumls_venv}/bin/python" -m nltk.downloader -d "${nltk_data}" stopwords

echo "QuickUMLS environment created at ${quickumls_venv}"
echo "Place the QuickUMLS index at ${repo_root}/data/quickumls_data"
