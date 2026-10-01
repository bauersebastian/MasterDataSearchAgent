# Master Data Search Agent

Google-like search over the SAP material master extract in `input/` (MARA, MAKT, MARM, MECL, MEME, MTXT, EINA –
14,372 materials), with an SAP Fiori (Horizon) UI and an AI duplicate check.

- **backend/** – FastAPI service: loads the input files, hybrid search, duplicate check; serves the frontend.
- **frontend/** – SAPUI5 app in Fiori design, loaded from the SAPUI5 CDN (no Node build): search page
  (List Report style) and material object page (`sap.uxap.ObjectPageLayout`).
- **data/chroma/** – the Chroma vector DB with the embeddings of all materials. It is **part of the app** (demo):
  build it once, then ship the folder with the app; at runtime only the search query is embedded.

## Setup

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env              # fill in OPENAI_API_KEY (and deployment names if they differ)
.venv/bin/python -m backend.indexer   # build data/chroma once (~14k materials, ~300k tokens, a few minutes)
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000.

The indexer is resumable: it stores a content hash per material and only embeds new or changed materials, so an
interrupted run can simply be started again. `--rebuild` deletes the DB and embeds everything (needed after
changing the embedding model or `MDSA_EMBEDDING_DIMENSIONS`). With a new extract in `input/` (same file naming
`*_<TABLE>.txt`, the newest file per table wins), run the indexer again.

Without `OPENAI_API_KEY` or without a built index the app still works with the fuzzy text search only and says so.

## Search

One search document per material: short texts (D/E/F/N), long texts (basic data, purchase order, sales, internal
note), class names and readable feature values (see the *Suchdokument* section on the object page).

| Mode | How |
|---|---|
| **Exakt** (always) | material number (with or without leading zeros), EAN of MARA/MARM, vendor part number (EINA-IDNLF), old material number – ranked first |
| **Unscharf** | probabilistic retrieval with BM25 over all texts and codes. Every query word is expanded to vocabulary terms: exact, prefix (*Verschr* → *Verschraubung*, search-as-you-type), compound part (*mutter* → *Gegenmutter*) and typo (rapidfuzz ≥ 80: *mesing* → *messing*). Umlauts are folded (*Duebel* = *Dübel*), codes are split (*6x45* = *6 x 45*), and documents covering more query words are strongly preferred. Offers *Meinten Sie …* for corrected words. |
| **Semantisch** | `text-embedding-3-large` (1536 dimensions) in Chroma, cosine similarity – finds synonyms and descriptions (*Schraubenzieher* → *Schraubendreher*, *Befestigung für Porenbeton* → *Gasbetondübel*) |
| **Hybrid** (default) | Reciprocal Rank Fusion (k = 60) of the fuzzy and the semantic ranking |

Every hit shows which retriever found it and with which score; the *?* button explains how the query words were
expanded. Filters: material type, material group, vendor, include materials flagged for deletion.

## Duplicate check

On the object page (section *Dubletten*): candidates from semantic neighbours of the material's own embedding,
fuzzy name similarity (rapidfuzz token set ratio), BM25 with the short text as query, and identical keys (EAN,
vendor part number), fused with RRF. *KI-Bewertung* sends the reference material and the candidates (texts, units,
weights, dimensions, EANs, vendors, classification) to `gpt-5.6-luna`, which classifies each candidate as
*Dublette*, *Mögliche Dublette*, *Variante* (same family, different size/thread/colour …) or *Verschieden* with a
reason.

## OpenAI endpoint

As in the research agent, the default is the Azure OpenAI resource `https://fisg-openai-valuestream-aoi.openai.azure.com/`
(v1 API) with the key from `OPENAI_API_KEY`. `MDSA_EMBEDDING_MODEL` and `MDSA_MODEL` must be the **deployment names**
on that resource – an embeddings deployment of `text-embedding-3-large` is required. Set `MDSA_OPENAI_BASE_URL=` (empty)
to use api.openai.com instead.

## API

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/config` | – | counts, models, whether the key is configured, filter values |
| POST | `/api/search` | `{query, mode: hybrid\|semantic\|lexical, mtart[], matkl[], vendor[], include_deleted, limit}` | hits with `relevance`, `exact`, `lexical`, `semantic`, `text_html`; `did_you_mean`, `expansions` |
| GET | `/api/suggest?q=` | – | up to 8 suggestions (fuzzy text search only, no embedding call) |
| GET | `/api/materials/{matnr}` | – | all data of a material |
| GET | `/api/materials/{matnr}/duplicates` | – | duplicate candidates with signals |
| POST | `/api/materials/{matnr}/duplicates/assess` | `{candidates: [matnr]}` (optional) | LLM verdicts |

## Notes

- "Fiori elements" proper (`sap.fe` templates) needs an OData V4 service with annotations; this app uses freestyle
  SAPUI5 with the same floorplans (dynamic page / object page) and theme, like the research agent.
- The app has **no user authentication**; bind it to `127.0.0.1` or put it behind a reverse proxy with auth/SSO.
