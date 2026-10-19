import logging
import time

from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

MODEL = "gemini-embedding-2"
OUTPUT_DIMENSIONALITY = 768
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 5


def build_client() -> genai.Client:
    return genai.Client()


def article_text(article: dict) -> str:
    parts = [part for part in (article["title"], article["lead"]) if part]
    return "\n".join(parts)


def embed_text(client: genai.Client, text: str) -> list[float]:
    """Раздел 3.3 CLAUDE.md: "считать по заголовку и лиду, не по всему тексту"."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            result = client.models.embed_content(
                model=MODEL,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=OUTPUT_DIMENSIONALITY),
            )
            return result.embeddings[0].values
        except errors.APIError as exc:
            if exc.code != 429 or attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "rate limit от Gemini API, жду %sс (попытка %s/%s)", backoff, attempt + 1, MAX_RETRIES
            )
            time.sleep(backoff)
            backoff *= 2
