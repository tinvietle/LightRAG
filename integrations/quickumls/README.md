# QuickUMLS detector

QuickUMLS runs in a persistent Python 3.11 subprocess so its legacy compiled
dependencies remain isolated from LightRAG's Python 3.12 environment. The two
processes exchange one JSON object per line over standard input/output.

## Setup

Place the complete index at `data/quickumls_data`, then run:

```bash
./integrations/quickumls/setup.sh
```

The script creates `.venv-quickumls`, installs the pinned direct dependencies
and spaCy English model, and downloads NLTK stopwords to `data/nltk_data`.

## Configuration

QuickUMLS is disabled by default. Enable it in `.env` with:

```dotenv
ENABLE_QUICKUMLS_NER=true
QUICKUMLS_PYTHON=.venv-quickumls/bin/python
QUICKUMLS_INDEX_DIR=data/quickumls_data
QUICKUMLS_NLTK_DATA=data/nltk_data
```

`ENABLE_GLINER_NER` and `ENABLE_QUICKUMLS_NER` are independent, allowing four
evaluation modes: neither detector, GLiNER only, QuickUMLS only, or both.
QuickUMLS supplies original source spans as hints; it does not normalize entity
names or merge graph nodes.
