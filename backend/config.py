"""Settings of the search app (overridable via environment / .env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")

# --- Input data -----------------------------------------------------------
# SAP material master extract: one ';' separated file per table (MAT_<timestamp>_<nr>_<TABLE>.txt)
INPUT_DIR = Path(os.getenv("MDSA_INPUT_DIR", APP_DIR / "input"))

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
COLLECTION = "materials"

# --- Search ---------------------------------------------------------------
LANGUAGE = os.getenv("MDSA_LANGUAGE", "D")          # SAP language key of the displayed short text
FALLBACK_LANGUAGES = ["D", "E", "F", "N"]           # if the material has no text in LANGUAGE
EMBED_LANGUAGES = ["D", "E", "F", "N"]              # short texts that go into the embedded document
CANDIDATES = 200          # hits per retriever before fusion
RRF_K = 60                # Reciprocal Rank Fusion constant
FUZZY_CUTOFF = 80         # min. rapidfuzz ratio (0-100) for a typo-tolerant token match
MIN_SEMANTIC_SIMILARITY = 0.25   # semantic hits below this cosine similarity are dropped
MAX_RESULTS = 200

# --- Duplicate check ------------------------------------------------------
DUPLICATE_CANDIDATES = 10

# --- SAP endpoint (target = source system of the extract) ---------------------
# FIS DXTO import web service, same message format as the MasterDataResearchAgent
ENDPOINT_URL = os.getenv(
    "SAP_ENDPOINT_URL",
    "http://fis127.fis-gmbh.de:8000/sap/bc/srt/rfc/fismpm/dxto_imp_call/100/imp_master_data/s4k_100",
)
OPERATION = os.getenv("SAP_OPERATION", "_-fismpm_-dxtoImpCall")   # RFC-style SOAP operation element
SOAP_ACTION = os.getenv("SAP_SOAP_ACTION", "")   # header is only sent when set
REQUEST_TIMEOUT = int(os.getenv("SAP_REQUEST_TIMEOUT", "120"))   # seconds
SEPARATOR = "^"
VARIANT = os.getenv("SAP_VARIANT", "BRUNO")
VIA_JOB = os.getenv("SAP_VIA_JOB", "X")
JOB_NAME = os.getenv("SAP_JOB_NAME", "")
KEY_FIELD = os.getenv("SAP_KEY_FIELD", "MATFS")   # key column of every table line, carries the material number
