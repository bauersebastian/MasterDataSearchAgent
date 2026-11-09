# Master Data Search Agent

Google-like search over two SAP master data objects, with an SAP Fiori (Horizon) UI:

- **Material master** – extract in `input/MAT/` (MARA, MAKT, MARM, MECL, MEME, MTXT, EINA – 14,372 materials – plus
  the bills of material STKO / STPO): BOM relationship visualization, AI duplicate check, posting a material to SAP.
- **Business partners** – GPA extract in `input/GPA/` (G000 general data, G100 roles, GADC/GAD2/GAD3/GAD6/GADU
  addresses and communication, G050/GRAC/GRA2/GRA3/GRA6 relationships and contact persons, GKA1/GKVP customer and
  GLA1/GLT3 vendor data, G0BK bank details, GTAX tax numbers, G0ID identification numbers – 1,868 partners; table
  descriptions in `input/GPA/objects.md`).

The switch *Material | Geschäftspartner* above the search field changes the object; each search keeps its own query.

- **backend/** – FastAPI service. Shared engine: `textsearch.py` (exact keys, BM25 with fuzzy expansion, RRF),
  `vectordb.py` (Chroma, one collection per object), `tables.py` (extract files), `llm.py`, `indexer.py`.
  One package per object with its data model, search, AI search and API router: `backend/material/`,
  `backend/partner/`.
- **frontend/** – SAPUI5 app in Fiori design, loaded from the SAPUI5 CDN (no Node build): `view/material/` and
  `view/partner/` (search page in List Report style, object page `sap.uxap.ObjectPageLayout`), likewise `controller/`.
- **data/chroma/** – the Chroma vector DB with the collections `materials` and `partners`. It is not part of the
  repository: build it on the machine that runs the app (see *Setup*); at runtime only the search query is embedded.

## Setup

```bash
git clone ssh://git@code.fis-gmbh.de:2222/fis-gruppe/intern/ai-solutions/ai-apps/ai-services/mpm-master_data_search.git && cd mpm-master_data_search
uv venv && uv pip install -r requirements.txt
cp .env.example .env              # fill in OPENAI_API_KEY, SAP_USER, SAP_PASSWORD (and deployment names if they differ)
.venv/bin/python -m backend.indexer   # build data/chroma once (~14k materials + ~1.9k partners, a few minutes)
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000.

The indexer is resumable: it stores a content hash per object and only embeds new or changed ones, so an
interrupted run can simply be started again. `python -m backend.indexer partner` (or `material`) indexes one object
only; `--rebuild` deletes the collection(s) and embeds everything (needed after changing the embedding model or
`MDSA_EMBEDDING_DIMENSIONS`). With a new extract in `input/MAT/` or `input/GPA/` (same file naming `*_<TABLE>.txt`,
the newest file per table wins), run the indexer again. Folders: `MDSA_MATERIAL_INPUT_DIR`, `MDSA_PARTNER_INPUT_DIR`
(material files directly in `input/`, as in older setups, are still found).

Without `OPENAI_API_KEY` or without a built index the app still works with the fuzzy text search only and says so.

## Material search

One search document per material: short texts (D/E/F/N), long texts (basic data, purchase order, sales, internal
note), class names, readable feature values and the BOM context: the names of the direct components
(*Stückliste aus: …*) and of the BOMs the material is used in (*Verwendet in Stückliste: …*) – so the semantic search
finds an assembly through its parts (see the *Suchdokument* section on the object page). The BOM context is not used
for the fuzzy text search, otherwise every assembly would match the words of its components.

| Mode | How |
|---|---|
| **Exakt** (always) | material number (with or without leading zeros), EAN of MARA/MARM, vendor part number (EINA-IDNLF), old material number – ranked first |
| **Unscharf** | probabilistic retrieval with BM25 over all texts and codes. Every query word is expanded to vocabulary terms: exact, prefix (*Verschr* → *Verschraubung*, search-as-you-type), compound part (*mutter* → *Gegenmutter*) and typo (rapidfuzz ≥ 80: *mesing* → *messing*). Umlauts are folded (*Duebel* = *Dübel*), codes are split (*6x45* = *6 x 45*), and documents covering more query words are strongly preferred. Offers *Meinten Sie …* for corrected words. |
| **Semantisch** | `text-embedding-3-large` (1536 dimensions) in Chroma, cosine similarity – finds synonyms and descriptions (*Schraubenzieher* → *Schraubendreher*, *Befestigung für Porenbeton* → *Gasbetondübel*) |
| **Hybrid** (default) | Reciprocal Rank Fusion (k = 60) of the fuzzy and the semantic ranking |
| **KI-Suche** | for requests in own words with requirements, e.g. *Veganes Waschmittel ohne Mikroplastik mit Inhaltsstoffen aus EU-Anbau*. 1) the LLM extracts the searched product, search keywords and the requirements; 2) the hybrid search retrieves 30 candidates with the keywords (completed by semantic hits of the whole request); 3) the LLM checks every candidate against each requirement **using only its master data** (texts, long texts, classes, features, units …, no general product knowledge): *belegt*, *nicht belegt* or *widerspricht*, with the evidence. Only *passend* / *teilweise* candidates are shown; an empty result is a valid answer. Two LLM calls per search (some seconds) |

Every hit shows which retriever found it and with which score; the *?* button explains how the query words were
expanded. Filters: material type, material group, vendor, include materials flagged for deletion.

## Business partner search

One search document per partner: name (organisation names 1-4, first and last name of a person, group names),
search terms, partner type and roles (Kunde, Lieferant, Kontaktperson …), the addresses with country, e-mail and web
addresses, customer / vendor number with account group, blocks – plus the context: the **contact persons** of an
organisation with function and department (or, for a person, the organisations it is the contact of, BUR001), other
relationships and, for vendors, the **materials supplied** from the purchasing info records EINA of the material
extract (an evenly spread sample of the short texts) – so *Lieferant für Kabelverschraubungen* finds the vendors
of cable glands. As for the BOM context, contact names and materials are not used for the fuzzy text search.

| Mode | How |
|---|---|
| **Exakt** (always) | partner number (with or without leading zeros), customer / vendor number, external partner number, tax number (USt-ID), IBAN, identification number, e-mail, phone, fax, website; for a contact person also the business e-mail / phone of the relationship |
| **Unscharf**, **Semantisch**, **Hybrid** | same engine as for materials (`backend/textsearch.py`), collection `partners` |
| **KI-Suche** | e.g. *Lieferant für Akkuschrauber in Unterfranken mit E-Mail-Adresse*: the LLM extracts the searched partner, keywords and requirements; 30 hybrid candidates are checked against each requirement using only their master data (roles, addresses – a region may be derived from postcode and city –, communication, contacts, customer / vendor data, bank details, materials supplied) |

Filters: partner type (Organisation / Person / Gruppe), role, country of the standard address, grouping (BU_GROUP),
include partners flagged for deletion (archiving flag XDELE or LOEVM of customer / vendor).

The object page shows general data and roles, the addresses with their communication data, the **relationship
graph** (organisation → contact persons, other relationships; click opens the partner) with the contact data from
G050/GRAC/GRA2/GRA3/GRA6, the **materials supplied** (click opens the material; conversely the vendor in the
material's purchasing info records opens the partner), all tables of the extract unchanged and the search document.

## Bills of material

`STKO` (BOM header, keyed by the header material) and `STPO` (items, component in `IDNRK`) are optional input files.
The object page of a material that is a BOM header or a component shows the section *Stücklisten*:

- **Beziehungsgraph** (`sap.suite.ui.commons.networkgraph`, layered, left to right): the material in the middle, all
  BOMs it is used in on the left (all levels up to the top), its components on the right (all levels down). Node
  button → open the material; lines carry quantity and BOM (usage / alternative / plant)
- **Stücklisten des Materials**: its BOM headers (usage, alternative, plant, base quantity, items)
- **Strukturstückliste (mehrstufig)**: multi-level explosion. Top level: all BOMs of the material; below, like
  CS12, one BOM per component – same plant (or plant independent), preferably the same usage, lowest alternative
- **Verwendungsnachweis (mehrstufig)**: multi-level where-used list (like CS15) up to the top-level materials
- filter by usage (1 Fertigung, 2 Konstruktion, 3 Universal, 5 Vertrieb …) and plant for graph and trees

Cycles (a BOM containing the material itself, as FG126 in the extract) are detected and marked, not followed.
Search hits show the badges *Stückliste* and *in n Stücklisten*. After adding or changing BOM files, run the indexer
again – it only re-embeds the materials whose BOM context changed.

## Duplicate check for materials

On the material object page (section *Dubletten*): candidates from semantic neighbours of the material's own embedding,
fuzzy name similarity (rapidfuzz token set ratio), BM25 with the short text as query, and identical keys (EAN,
vendor part number), fused with RRF. *KI-Bewertung* sends the reference material and the candidates (texts, units,
weights, dimensions, EANs, vendors, classification) to `gpt-5.6-luna`, which classifies each candidate as
*Dublette*, *Mögliche Dublette*, *Variante* (same family, different size/thread/colour …) or *Verschieden* with a
reason.

## Duplicate check for business partners

On the partner object page (section *Dubletten*), candidates from:

- semantic neighbours of the partner's own embedding, BM25 with name and city as query
- fuzzy name similarity **without the legal form** (*Müller GmbH & Co.KG* = *Müller KG*, rapidfuzz token set ratio)
- the **same address** (country, postcode, street and house number; *Str.* = *Straße*)
- **shared keys**: tax number / VAT ID, IBAN, identification number, external partner number (strong), e-mail,
  phone, fax, website of the own addresses (weaker: test data shares info@ addresses)
- **customer ↔ vendor cross references** (KNA1-LIFNR / LFA1-KUNNR pointing to another partner: in S/4HANA one
  partner should carry both roles); for persons the same name with the same birth date

fused with RRF. The similarity shown is the name or semantic similarity, at least 95 % for a strong shared key and
90 % for the same name at the same address; the same name in another city (branches) is lowered and flagged
*anderer Ort*. Badges: shared keys, *anderer Partnertyp*, *steht in Beziehung* (e.g. own contact person).
Only candidates with a similarity of at least `DUPLICATE_MIN_SCORE` (80 %) are shown; without one the section
just says *Keine relevante Dublette gefunden*, a link shows all candidates.
*KI-Bewertung* sends the reference partner and the candidates (names, roles, addresses, communication, customer /
vendor data, tax numbers, bank accounts, relations, materials supplied) to the LLM, which classifies each candidate
as *Dublette*, *Mögliche Dublette*, *Verwandt* (branch / site, company of the same group, contact person) or
*Verschieden* with a reason; candidates rated *Verschieden* are hidden afterwards.

### Duplicate overview

*Dublettenübersicht* (button on the partner search page, `#dubletten`) lists all partners with probable duplicates.
The heuristic check above runs for every partner (about 10 ms each, computed in the background at startup and
cached; *Neu berechnen* recomputes, e.g. after the vector index was rebuilt). No LLM call is made here; the LLM
assessment stays on the partner page, where a click on a partner in the overview lands in the *Dubletten* section.
Each relevant candidate pair is counted once and gets the tier of its strongest evidence:

1. *Starker Schlüssel*: tax number, IBAN, ID number, external partner number, birth date, or customer / vendor cross reference
2. *Gleiche Adresse*: similar name (≥ 80 %) at the same address
3. *Ähnlicher Name*: similar name only (often branches)
4. *Nur semantisch*: similar embeddings only (many false hits, off by default)

Pairs with a similar name are merged into groups (A–B and B–C → A, B, C). Pairs that share a key but have different
names (usually copied test bank details or tax numbers) are not merged and stay separate, flagged *abweichender Name
– Schlüssel prüfen*; *Nur gleicher Name* hides them. Filters: tier (default up to 3), deleted partners, text. The
shown groups can be exported as CSV (one line per partner).

## Send to SAP

*An SAP senden* on the object page posts the material to the FIS DXTO import web service (`SAP_ENDPOINT_URL`,
default `fis127 … /s4k_100`, the source system of the extract) – same SOAP message as the MasterDataResearchAgent:

- empty `<soap-env:Header/>`, HTTP basic auth (`SAP_USER` / `SAP_PASSWORD`), `Content-Type: text/xml`,
  `SOAPAction` only when `SAP_SOAP_ACTION` is set; parameters `IfSep=^`, `IfVariant=BRUNO`, `IfViaJob=X`, `IfJobname`
- logon language German: URL parameter `sap-language=DE` and `Accept-Language: de` (`SAP_LOGON_LANGUAGE`);
  otherwise SAP may log on in the service/system language (EN) and convert the German unit codes wrongly
- the material is sent **unchanged as stored in the extract**: all tables (MARA, MAKT, MARM, MECL, MEME, MTXT, EINA and,
  if the material is a BOM header, its STKO / STPO rows),
  all rows, every field that is filled in at least one row, in the column order of the extract files; values are not
  converted. Every line starts with the key field `MATNR` (`SAP_KEY_FIELD`) holding the material number (18 digits,
  as in the extract) and ends with `^` – with `MATNR` the import **changes the existing material** (`MATFS`, as used by
  the research agent, would create a new one)
- the dialog shows the XML (read-only, copy / download), asks for confirmation (with a warning for materials flagged
  for deletion) and shows HTTP status and SAP response

*An SAP senden* on the **business partner** object page sends the partner the same way (same endpoint, message
format and dialog, `GET /api/partners/{partner}/xml`, then `POST /api/sap/post`):

- one `item` per GPA table in extract order (`TObj` = table name, e.g. `G000`, `G100`, `G0BK`, `GTAX`, `GADC`,
  `GAD2`, `GAD6`, `G021`, `G050`, `GKA1`, `GKVP`, `GLA1`, `GLT3`, …), header line with the SAP field names, one line
  per row, all fields filled in at least one row of the table, values unchanged
- every line starts with the key field `PARTNER` (`SAP_PARTNER_KEY_FIELD`) holding the partner number as in the
  extract (10 digits)
- `XDELE` is only sent in `G000` (archiving flag of the partner); in the other tables it marks deleted extract rows,
  which are not loaded and not sent
- `IfVariant` is `SAP_PARTNER_VARIANT` (default `BRUNO_GPA`; materials keep `SAP_VARIANT`, default `BRUNO`)

## Deletion check

*Löschprüfung* on the partner object page checks – on demand only, read only – whether open documents in SAP still
reference the partner (preparation for marking partners as deleted). Live via the standard OData APIs
(`SAP_ODATA_URL`, client `SAP_CLIENT`), `POST /api/partners/{partner}/deletion-check`. All checks run in parallel.

**Customer role** – the customer in **any partner function** (sold-to, ship-to, bill-to, payer, … – header and item
partners):

| Area | API | Blocking |
|---|---|---|
| Sales orders | `API_SALES_ORDER_SRV` | overall processing status not *C* (completed) – also delivered but not yet billed orders, and orders without delivery-relevant items; fully rejected orders only as a warning |
| Quotations | `API_SALES_QUOTATION_SRV` | not completed and still valid; expired quotations are only a warning |
| Billing documents | `API_BILLING_DOCUMENT_SRV` | not cancelled, sold-to or payer, not transferred to accounting (transfer status not *C* / *D* / *E*) |
| Open items customer (FI) | `API_OPLACCTGDOCITEMCUBE_SRV` | not cleared items of the customer account (account type *D*), all company codes, incl. special G/L (down payments); noted items (category *S*, e.g. down payment requests) only as a warning; the summary shows the balance of the open items |
| Outbound deliveries | `API_OUTBOUND_DELIVERY_SRV;v=0002` | goods issue not (fully) posted (goods movement status *A* / *B*), sold-to or ship-to |
| Contracts | `API_SALES_CONTRACT_SRV` | overall processing status not *C*; contracts whose validity has ended only as a warning |
| Scheduling agreements | `API_SALES_SCHEDULING_AGREEMENT` | overall processing status not *C*; ended validity only as a warning |
| Returns | `API_CUSTOMER_RETURN_SRV` | overall processing status not *C* |
| Credit memo requests | `API_CREDIT_MEMO_REQUEST_SRV` | overall processing status not *C* |

For all sales documents (orders, quotations, contracts, scheduling agreements, returns, credit memo requests), fully
rejected ones (`OverallSDDocumentRejectionSts` = *C*) are only a warning: they are effectively closed even when the
processing status is not *C*. Logon language `SAP_LOGON_LANGUAGE` makes document types come in German (*TA* instead
of *OR*). The SAP user needs read authorization for each API (`/IWFND/MAINT_SERVICE`: service active, role with
`S_SERVICE` and display authorization for the documents).

**Supplier role** – the supplier as supplier (*LF*), invoicing party (*RS*), goods supplier (*WL*), ordering address
(*BA*) or bidder (*BI*), from the header fields or the purchasing partner table, wherever the API has them:

| Area | API | Blocking |
|---|---|---|
| Open items supplier (FI) | `API_OPLACCTGDOCITEMCUBE_SRV` | not cleared items of the supplier account (account type *K*), as for customers |
| Supplier invoices | `API_SUPPLIERINVOICE_PROCESS_SRV` | not posted (parked / held, status not *5*), invoicing party; posted invoices are in the FI open items |
| Purchase orders | `API_PURCHASEORDER_PROCESS_SRV` | not deleted, with items not deleted and not completely delivered; value of the open items = order quantity × net price. The final invoice flag is not maintained on fis127 (no item has it), so "not finally invoiced" is no criterion |
| Scheduling agreements (purchasing) | `API_SCHED_AGRMT_PROCESS_SRV` | not deleted; validity ended only as a warning |
| Contracts (purchasing) | `API_PURCHASECONTRACT_PROCESS_SRV;v=0002` | not deleted; validity ended only as a warning |
| Inbound deliveries | `API_INBOUND_DELIVERY_SRV;v=0002` | goods receipt not (fully) posted (goods movement status *A* / *B*) |
| Purchase requisitions | `API_PURCHASEREQ_PROCESS_SRV` | warning only: not deleted, not closed, not yet converted (processing status *N*), supplier as fixed or desired source |
| Requests for quotation | `API_RFQ_PROCESS_SRV` | warning only: draft or published (lifecycle status *01* / *02*), supplier as bidder – also covers supplier quotations, which always answer an RFQ (`API_QTN_PROCESS_SRV` is therefore not needed) |
| Purchasing info records | extract (`EINA`) | warning only: not deleted info records from the material extract (master data, not live) – `API_INFORECORD_PROCESS_SRV` is not needed |

The purchasing partner table (EKPA) holds the partners of all purchasing documents, so the partner entities of the
scheduling agreement and contract APIs also return purchase order numbers; only numbers found as a header count.

Filters use the 10-digit SAP number (`Partner.kunnr_sap` / `lifnr_sap`); the services accept it with or without
leading zeros but return numbers **without** them, so results are compared via `same_number`.

Areas whose API is not available (HTTP 403/404) are reported as **nicht geprüft**, never as passed – after the
activation of all services above there are none left; the lists `NOT_AVAILABLE_CUSTOMER` / `NOT_AVAILABLE_VENDOR` in
`backend/partner/deletion_check.py` stay for future areas. The result is *Löschen nicht möglich* (open documents),
*unvollständig* (no open documents in the checked areas, but areas not checked or failed) or *keine offenen Belege*;
warnings never block. The dialog shows business texts only – no service names or SAP error messages; the
response still carries `service` (and `error` for failed areas), and failures are logged with service and SAP message.

## OpenAI endpoint

As in the research agent, the default is the Azure OpenAI resource `https://fisg-openai-valuestream-aoi.openai.azure.com/`
(v1 API) with the key from `OPENAI_API_KEY`. `MDSA_EMBEDDING_MODEL` and `MDSA_MODEL` must be the **deployment names**
on that resource – an embeddings deployment of `text-embedding-3-large` is required. Set `MDSA_OPENAI_BASE_URL=` (empty)
to use api.openai.com instead.

## API

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/config` | – | counts, models, whether the key is configured, filter values (partner: under `partner`) |
| POST | `/api/search` | `{query, mode: hybrid\|semantic\|lexical, mtart[], matkl[], vendor[], include_deleted, limit}` | hits with `relevance`, `exact`, `lexical`, `semantic`, `text_html`; `did_you_mean`, `expansions` |
| GET | `/api/suggest?q=` | – | up to 8 suggestions (fuzzy text search only, no embedding call) |
| GET | `/api/materials/{matnr}` | – | all data of a material |
| GET | `/api/materials/{matnr}/bom?stlan=&werks=` | – | BOM headers, explosion, where-used list, graph, filter values |
| GET | `/api/materials/{matnr}/duplicates` | – | duplicate candidates with signals |
| POST | `/api/materials/{matnr}/duplicates/assess` | `{candidates: [matnr]}` (optional) | LLM verdicts |
| GET | `/api/materials/{matnr}/xml` | – | SOAP message of the material, tables, endpoint |
| POST | `/api/sap/post` | `{xml}` | `{ok, status_code, reason, fault, body}` |
| POST | `/api/partners/search` | `{query, mode: hybrid\|semantic\|lexical\|ai, type[], role[], country[], group[], include_deleted, limit}` | hits with `relevance`, `exact`, `lexical`, `semantic`, `name_html`, `address_html` |
| GET | `/api/partners/suggest?q=` | – | up to 8 suggestions |
| GET | `/api/partners/tables` | – | GPA table descriptions (objects.md) |
| GET | `/api/partners/duplicates?max_tier=3&include_deleted=false&refresh=false` | – | duplicate groups over all partners (heuristic), pair counts per tier |
| GET | `/api/partners/{partner}` | – | all data of a partner: addresses, relations, materials, all extract tables |
| GET | `/api/partners/{partner}/xml` | – | SOAP message of the partner, tables, endpoint, variant |
| POST | `/api/partners/{partner}/deletion-check` | – | open documents blocking the deletion: `verdict` (blocked / incomplete / ok), `checks` with documents |
| GET | `/api/partners/{partner}/duplicates` | – | duplicate candidates with signals |
| POST | `/api/partners/{partner}/duplicates/assess` | `{candidates: [partner]}` (optional) | LLM verdicts |

## Notes

The app currently has **no user authentication**: anyone who can reach it can post to SAP with the server's
credentials. Bind it to `127.0.0.1` or put it behind a reverse proxy with auth/SSO.
