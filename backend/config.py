"""Settings of the search app (overridable via environment / .env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")

# --- Input data -----------------------------------------------------------
# One folder per master data object, one ';' separated file per table (<OBJ>_<timestamp>_<nr>_<TABLE>.txt)
INPUT_DIR = Path(os.getenv("MDSA_INPUT_DIR", APP_DIR / "input"))
# material master (MARA, MAKT, ...); older setups kept the files directly in input/
MATERIAL_INPUT_DIR = Path(os.getenv("MDSA_MATERIAL_INPUT_DIR", INPUT_DIR / "MAT"))
if not MATERIAL_INPUT_DIR.is_dir() and any(INPUT_DIR.glob("*_MARA.txt")):
    MATERIAL_INPUT_DIR = INPUT_DIR
# business partners (GPA extract: G000, G100, GADC, G050, GKA1, GLA1, ...)
PARTNER_INPUT_DIR = Path(os.getenv("MDSA_PARTNER_INPUT_DIR", INPUT_DIR / "GPA"))

# --- OpenAI ---------------------------------------------------------------
# Azure OpenAI resource (v1 API, used with the standard OpenAI client); the key is OPENAI_API_KEY.
# Set MDSA_OPENAI_BASE_URL to an empty value to use api.openai.com instead.
OPENAI_BASE_URL = os.getenv("MDSA_OPENAI_BASE_URL",
                            "https://fisg-openai-valuestream-aoi.openai.azure.com/openai/v1/") or None
EMBEDDING_MODEL = os.getenv("MDSA_EMBEDDING_MODEL", "text-embedding-3-large")   # on Azure: the deployment name
EMBEDDING_DIMENSIONS = int(os.getenv("MDSA_EMBEDDING_DIMENSIONS", "1536"))
EMBEDDING_BATCH = int(os.getenv("MDSA_EMBEDDING_BATCH", "256"))    # documents per embeddings request
MODEL = os.getenv("MDSA_MODEL", "gpt-5.6-luna")                   # duplicate check; on Azure: the deployment name
REASONING_EFFORT = os.getenv("MDSA_REASONING_EFFORT", "low")

# --- Vector DB ------------------------------------------------------------
# Bundled with the app (demo): built once with `python -m backend.indexer`
CHROMA_DIR = Path(os.getenv("MDSA_CHROMA_DIR", APP_DIR / "data" / "chroma"))
MATERIAL_COLLECTION = "materials"
# HNSW graph of the approximate nearest neighbour search. Defaults of Chroma (16 / 100) miss close documents;
# 32 / 200 / 400 measured 99 % recall at 2 ms per query. Changing M or ef_construction rebuilds the graph from the
# stored embeddings on the next indexer run (no embedding calls).
HNSW_M = int(os.getenv("MDSA_HNSW_M", "32"))
HNSW_CONSTRUCTION_EF = int(os.getenv("MDSA_HNSW_CONSTRUCTION_EF", "200"))
HNSW_SEARCH_EF = int(os.getenv("MDSA_HNSW_SEARCH_EF", "400"))
PARTNER_COLLECTION = "partners"

# --- Search ---------------------------------------------------------------
LANGUAGE = os.getenv("MDSA_LANGUAGE", "D")          # SAP language key of the displayed (material) short text
FALLBACK_LANGUAGES = ["D", "E", "F", "N"]           # if the material has no text in LANGUAGE
EMBED_LANGUAGES = ["D", "E", "F", "N"]              # short texts that go into the embedded document
CANDIDATES = 200          # hits per retriever before fusion
RRF_K = 60                # Reciprocal Rank Fusion constant (duplicate check)
FUZZY_CUTOFF = 80         # min. rapidfuzz ratio (0-100) for a typo-tolerant token match
MIN_SEMANTIC_SIMILARITY = 0.25   # semantic hits below this cosine similarity are dropped
# Hybrid search: relevance = weighted sum of the lexical score (0-1) and the calibrated semantic similarity
# (cosine MIN_SEMANTIC_SIMILARITY -> 0, SEMANTIC_FULL_SIMILARITY and above -> 1)
HYBRID_SEMANTIC_WEIGHT = float(os.getenv("MDSA_HYBRID_SEMANTIC_WEIGHT", "0.5"))
SEMANTIC_FULL_SIMILARITY = float(os.getenv("MDSA_SEMANTIC_FULL_SIMILARITY", "0.75"))
MAX_RESULTS = 200

# --- Duplicate check ------------------------------------------------------
DUPLICATE_CANDIDATES = 10
DUPLICATE_MIN_SCORE = 80   # business partners: candidates below this similarity are not shown by default

# --- SAP endpoint (target = source system of the extract) ---------------------
# FIS DXTO import web service, same message format as the MasterDataResearchAgent
ENDPOINT_URL = os.getenv(
    "SAP_ENDPOINT_URL",
    "http://fis127.fis-gmbh.de:8000/sap/bc/srt/rfc/fismpm/dxto_imp_call/100/imp_master_data/s4k_100",
)
OPERATION = os.getenv("SAP_OPERATION", "_-fismpm_-dxtoImpCall")   # RFC-style SOAP operation element
SOAP_ACTION = os.getenv("SAP_SOAP_ACTION", "")   # header is only sent when set
REQUEST_TIMEOUT = int(os.getenv("SAP_REQUEST_TIMEOUT", "120"))   # seconds
# Logon language of the SOAP call (URL parameter sap-language + Accept-Language). Without it SAP may fall back to
# the service or system language (e.g. EN), and the import converts the German unit codes (ST, KAR, ...) wrongly.
SAP_LOGON_LANGUAGE = os.getenv("SAP_LOGON_LANGUAGE", "DE")
SEPARATOR = "^"
VARIANT = os.getenv("SAP_VARIANT", "BRUNO")
VIA_JOB = os.getenv("SAP_VIA_JOB", "X")
JOB_NAME = os.getenv("SAP_JOB_NAME", "")
# key column of every table line: MATNR changes the existing material (MATFS would create a new one)
KEY_FIELD = os.getenv("SAP_KEY_FIELD", "MATNR")
# business partners (GPA tables G000, G100, ...): key column of every line and DXTO variant of the partner import
PARTNER_KEY_FIELD = os.getenv("SAP_PARTNER_KEY_FIELD", "PARTNER")
PARTNER_VARIANT = os.getenv("SAP_PARTNER_VARIANT", "BRUNO_GPA")

# --- Golden record (merge of duplicate business partners) -------------------
# log of the merges sent to DXTO (SQLite)
MERGE_DB = Path(os.getenv("MDSA_MERGE_DB", APP_DIR / "data" / "merges.sqlite"))
# block reasons set with the customer block (KNA1-AUFSD / LIFSD / FAKSD / CASSD); empty = field not set.
# KNA1-SPERR (posting block) and for vendors LFA1-SPERR / SPERM are flags and always set.
MERGE_ORDER_BLOCK = os.getenv("MDSA_MERGE_ORDER_BLOCK", "01")
MERGE_DELIVERY_BLOCK = os.getenv("MDSA_MERGE_DELIVERY_BLOCK", "01")
MERGE_BILLING_BLOCK = os.getenv("MDSA_MERGE_BILLING_BLOCK", "01")
MERGE_SALES_BLOCK = os.getenv("MDSA_MERGE_SALES_BLOCK", "")
# a deletion check older than this is repeated before the merge is sent
MERGE_CHECK_MAX_AGE = int(os.getenv("MDSA_MERGE_CHECK_MAX_AGE", "3600"))   # seconds

# --- SAP OData (live data, read only, same user) -----------------------------
SAP_CLIENT = os.getenv("SAP_CLIENT", "100")
# base URL of the OData services (standard APIs API_SALES_ORDER_SRV, API_SALES_QUOTATION_SRV, ...)
SAP_ODATA_URL = os.getenv("SAP_ODATA_URL", "http://fis127.fis-gmbh.de:8000/sap/opu/odata/sap").rstrip("/")
