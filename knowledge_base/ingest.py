"""
Knowledge Base Ingest
---------------------
Chunks the UW guidelines document and indexes it into Azure AI Search
with vector embeddings for semantic retrieval.

The UW Rules Agent (via the search_uw_guidelines tool) queries this index
at inference time to retrieve the specific rules applicable to each submission.

Chunking strategy:
  - Section-aware: splits on markdown headers (##) to preserve logical units
  - Overlap: 1 sentence carried into the next chunk to preserve context
    across section boundaries
  - Target chunk size: ~400 tokens (safe for embedding model context window)

Run once to set up, then re-run whenever guidelines are updated:
  python knowledge_base/ingest.py

For production: trigger this from a CI/CD pipeline when uw_guidelines.md
is updated in source control.
"""

import os
import re
import json
import hashlib
import logging
from pathlib import Path
from dotenv import load_dotenv

from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SearchIndex,
)
from azure.search.documents.models import VectorizedQuery
from openai import AzureOpenAI

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEARCH_ENDPOINT   = os.environ["AZURE_SEARCH_ENDPOINT"]
INDEX_NAME        = os.environ.get("AZURE_SEARCH_INDEX_NAME", "uw-guidelines")
OPENAI_ENDPOINT   = os.environ["AZURE_OPENAI_ENDPOINT"]
EMBED_DEPLOYMENT  = os.environ.get("AZURE_EMBED_DEPLOYMENT", "text-embedding-3-small")
EMBED_DIMENSIONS  = 1536
GUIDELINES_PATH   = Path(__file__).parent / "uw_guidelines.md"


# ---------------------------------------------------------------------------
# Step 1: Section-aware chunking
# ---------------------------------------------------------------------------

def chunk_markdown(text: str, max_chars: int = 1800) -> list[dict]:
    """
    Split a markdown document into chunks that respect section boundaries.

    Strategy:
      1. Split on level-2 headers (##) to get logical sections.
      2. If a section exceeds max_chars, split further on paragraphs.
      3. Each chunk carries its section heading as metadata for retrieval context.

    Returns a list of dicts: {id, section, content, char_count}
    """
    chunks = []
    sections = re.split(r'\n(?=## )', text.strip())

    for section in sections:
        if not section.strip():
            continue
        chunks.extend(_chunk_section(section, max_chars, len(chunks)))

    logger.info(f"Chunked into {len(chunks)} chunks")
    return chunks


def _parse_section(section: str) -> tuple[str, str]:
    """Return (heading, body) from a markdown section block."""
    lines = section.strip().splitlines()
    heading = lines[0].lstrip("#").strip() if lines[0].startswith("#") else "Introduction"
    body = "\n".join(lines[1:]).strip()
    return heading, body


def _chunk_section(section: str, max_chars: int, chunk_offset: int) -> list[dict]:
    """Return one or more chunks for a single markdown section."""
    heading, body = _parse_section(section)

    if len(section) <= max_chars:
        chunk_text = f"{heading}\n\n{body}".strip()
        return [_make_chunk(chunk_text, heading, chunk_offset)]

    return _split_by_paragraphs(heading, body, max_chars, chunk_offset)


def _split_by_paragraphs(heading: str, body: str, max_chars: int, chunk_offset: int) -> list[dict]:
    """Split an oversized section into paragraph-bounded chunks."""
    chunks = []
    current = f"{heading}\n\n"

    for para in re.split(r'\n{2,}', body):
        para = para.strip()
        if not para:
            continue

        would_overflow = len(current) + len(para) + 2 > max_chars
        has_content_beyond_heading = len(current) > len(heading) + 4

        if would_overflow and has_content_beyond_heading:
            chunks.append(_make_chunk(current.strip(), heading, chunk_offset + len(chunks)))
            current = f"{heading} (continued)\n\n{para}\n\n"
        else:
            current += para + "\n\n"

    if current.strip():
        chunks.append(_make_chunk(current.strip(), heading, chunk_offset + len(chunks)))

    return chunks


def _make_chunk(content: str, section: str, index: int) -> dict:
    chunk_id = hashlib.md5(content.encode()).hexdigest()[:12]
    return {
        "id": f"uw-{index:03d}-{chunk_id}",
        "section": section,
        "content": content,
        "char_count": len(content),
        "source": "uw_guidelines.md",
    }


# ---------------------------------------------------------------------------
# Step 2: Generate embeddings
# ---------------------------------------------------------------------------

def embed_chunks(chunks: list[dict]) -> list[dict]:
    """
    Generate vector embeddings for each chunk using Azure OpenAI.
    Batches requests to stay within API rate limits.
    """
    openai_client = AzureOpenAI(
        azure_endpoint=OPENAI_ENDPOINT,
        azure_deployment=EMBED_DEPLOYMENT,
        api_version="2024-02-01",
        azure_ad_token_provider=lambda: DefaultAzureCredential()
            .get_token("https://cognitiveservices.azure.com/.default").token,
    )

    BATCH_SIZE = 16
    embedded = []

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["content"] for c in batch]

        logger.info(f"Embedding batch {i // BATCH_SIZE + 1} "
                    f"({len(batch)} chunks)...")

        response = openai_client.embeddings.create(
            input=texts,
            model=EMBED_DEPLOYMENT,
            dimensions=EMBED_DIMENSIONS,
        )

        for chunk, emb_obj in zip(batch, response.data):
            embedded.append({**chunk, "content_vector": emb_obj.embedding})

    logger.info(f"Embedded {len(embedded)} chunks")
    return embedded


# ---------------------------------------------------------------------------
# Step 3: Create or update the Azure AI Search index
# ---------------------------------------------------------------------------

def create_index(index_client: SearchIndexClient) -> None:
    """
    Create the Azure AI Search index with both keyword and vector fields.
    Idempotent — safe to run multiple times.
    """
    fields = [
        SimpleField(name="id",      type=SearchFieldDataType.String, key=True),
        SimpleField(name="source",  type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="section", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBED_DIMENSIONS,
            vector_search_profile_name="uw-vector-profile",
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="uw-hnsw")],
        profiles=[VectorSearchProfile(
            name="uw-vector-profile",
            algorithm_configuration_name="uw-hnsw",
        )],
    )

    index = SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
    )

    existing = [i.name for i in index_client.list_indexes()]
    if INDEX_NAME in existing:
        logger.info(f"Index '{INDEX_NAME}' exists — updating schema")
        index_client.create_or_update_index(index)
    else:
        logger.info(f"Creating index '{INDEX_NAME}'")
        index_client.create_index(index)


# ---------------------------------------------------------------------------
# Step 4: Upload documents
# ---------------------------------------------------------------------------

def upload_chunks(search_client: SearchClient, chunks: list[dict]) -> None:
    """
    Upload embedded chunks to Azure AI Search in batches.
    Uses merge_or_upload so re-runs are safe (idempotent).
    """
    BATCH_SIZE = 100

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        results = search_client.merge_or_upload_documents(documents=batch)
        succeeded = sum(1 for r in results if r.succeeded)
        logger.info(f"Uploaded batch {i // BATCH_SIZE + 1}: "
                    f"{succeeded}/{len(batch)} documents succeeded")


# ---------------------------------------------------------------------------
# Step 5: Smoke test — verify retrieval works
# ---------------------------------------------------------------------------

def smoke_test(search_client: SearchClient, openai_client: AzureOpenAI) -> None:
    """
    Run a sample query to confirm the index is working end-to-end.
    Tests both keyword and vector retrieval paths.
    """
    test_queries = [
        "timber frame flood zone mandatory exclusion",
        "Zone 3b decline criteria",
        "claims history 3 or more referral",
        "Flood Re eligibility council tax band",
    ]

    logger.info("\n--- Smoke test ---")
    for query in test_queries:
        # Vector search
        embedding = openai_client.embeddings.create(
            input=[query],
            model=EMBED_DEPLOYMENT,
            dimensions=EMBED_DIMENSIONS,
        ).data[0].embedding

        vector_query = VectorizedQuery(
            vector=embedding,
            k_nearest_neighbors=2,
            fields="content_vector",
        )

        results = list(search_client.search(
            search_text=query,
            vector_queries=[vector_query],
            select=["id", "section", "content"],
            top=2,
        ))

        logger.info(f"\nQuery: '{query}'")
        for r in results:
            preview = r["content"][:120].replace("\n", " ")
            logger.info(f"  [{r['section']}] {preview}...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    credential    = DefaultAzureCredential()
    index_client  = SearchIndexClient(SEARCH_ENDPOINT, credential)
    search_client = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, credential)
    openai_client = AzureOpenAI(
        azure_endpoint=OPENAI_ENDPOINT,
        azure_deployment=EMBED_DEPLOYMENT,
        api_version="2024-02-01",
        azure_ad_token_provider=lambda: credential
            .get_token("https://cognitiveservices.azure.com/.default").token,
    )

    # 1. Load source document
    logger.info(f"Loading {GUIDELINES_PATH}")
    text = GUIDELINES_PATH.read_text(encoding="utf-8")

    # 2. Chunk
    chunks = chunk_markdown(text)
    logger.info(f"Chunk sizes: min={min(c['char_count'] for c in chunks)}, "
                f"max={max(c['char_count'] for c in chunks)}, "
                f"avg={sum(c['char_count'] for c in chunks)//len(chunks)}")

    # 3. Embed
    chunks = embed_chunks(chunks)

    # 4. Create index
    create_index(index_client)

    # 5. Upload
    upload_chunks(search_client, chunks)
    logger.info(f"Ingest complete — {len(chunks)} chunks indexed into '{INDEX_NAME}'")

    # 6. Smoke test
    smoke_test(search_client, openai_client)


if __name__ == "__main__":
    main()
