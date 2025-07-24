from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.utils.functional import cached_property
from pgvector.django import VectorField
import openai
from django.conf import settings
from typing import Optional, Dict, Any, List


def generate_summary(
    text: str, max_tokens: int = 100, user_context: Optional[Dict[str, Any]] = None
) -> str:
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": f"Return a summary of the text that is {max_tokens} tokens or less. Optimize for token usage, omit non-essential information.",
        }
    ]

    if user_context:
        messages.append(
            {
                "role": "function",
                "name": "user_context",
                "content": str(user_context),
            }
        )

    messages.append({"role": "user", "content": text})

    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,  # type: ignore
    )

    content = response.choices[0].message.content
    if content is None:
        raise ValueError("OpenAI returned empty content")

    return content


def generate_embedding(text: str) -> list[float]:
    """
    Generate embeddings using OpenAI's text-embedding-3-small model.
    Returns a 1536-dimensional vector.
    """
    try:
        print(text)
        response = openai.embeddings.create(
            model="text-embedding-3-small", input=text, encoding_format="float"
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"Error generating embedding: {e}")
        raise e


class SummarizedVectorField(models.TextField):
    def __init__(self, *args, **kwargs):
        self.generate_summary = kwargs.pop("generate_summary", generate_summary)
        self.generate_embedding = kwargs.pop("generate_embedding", generate_embedding)
        self.embedding_field_name = kwargs.pop("embedding_field_name", "embedding")
        self.summary_field_name = kwargs.pop("summary_field_name", "summary")
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.embedding_field_name:
            kwargs["embedding_field_name"] = self.embedding_field_name
        if self.summary_field_name:
            kwargs["summary_field_name"] = self.summary_field_name
        return name, path, args, kwargs

    def pre_save(self, model_instance, add):
        # Get current text value
        current_text = getattr(model_instance, self.attname)

        # Determine if we need to regenerate summary and embedding
        has_changed = self._has_field_changed(model_instance, current_text)

        if has_changed:
            # Generate summary (~100 tokens)
            summary = self.generate_summary(current_text)

            # Generate embedding vector
            embedding = self.generate_embedding(summary)

            # Set summary and embedding fields on model instance
            setattr(model_instance, self.summary_field_name, summary)
            setattr(model_instance, self.embedding_field_name, embedding)

        return current_text

    def _has_field_changed(self, instance, current_text):
        if instance._state.adding:
            return True  # New instance always triggers generation
        else:
            # Check database for previous value
            old_value = (
                instance.__class__.objects.filter(pk=instance.pk)
                .values_list(self.attname, flat=True)
                .first()
            )
            return old_value != current_text
