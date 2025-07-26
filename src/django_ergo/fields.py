from django.db import models
from django.conf import settings
from pgvector.django import VectorField
from typing import Optional, Dict, Any, List
import openai


def generate_summary(
    text: str, max_tokens: int = 100, user_context: Optional[Dict[str, Any]] = None
) -> str:
    """
    Generate a summary of the text using OpenAI's API.
    
    Args:
        text: The text to summarize
        max_tokens: Maximum tokens for the summary
        user_context: Optional context for the summary
        
    Returns:
        str: Generated summary
    """
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


def generate_embedding(text: str) -> List[float]:
    """
    Generate embeddings using OpenAI's text-embedding-3-small model.
    Returns a 1536-dimensional vector.
    
    Args:
        text: The text to embed
        
    Returns:
        List[float]: 1536-dimensional embedding vector
    """
    try:
        response = openai.embeddings.create(
            model="text-embedding-3-small", 
            input=text, 
            encoding_format="float"
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"Error generating embedding: {e}")
        raise e


class SummarizedVectorField(models.TextField):
    """
    A custom Django field that automatically generates summaries and embeddings
    for text content when the field value changes.
    
    This field extends TextField and automatically:
    1. Generates a summary of the content (configurable max tokens)
    2. Creates an embedding vector from the summary
    3. Stores both in related fields on the model
    """
    
    def __init__(self, *args, **kwargs):
        self.generate_summary = kwargs.pop("generate_summary", generate_summary)
        self.generate_embedding = kwargs.pop("generate_embedding", generate_embedding)
        self.embedding_field_name = kwargs.pop("embedding_field_name", "embedding")
        self.summary_field_name = kwargs.pop("summary_field_name", "summary")
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        """Return the field's deconstruction for migrations."""
        name, path, args, kwargs = super().deconstruct()
        if self.embedding_field_name != "embedding":
            kwargs["embedding_field_name"] = self.embedding_field_name
        if self.summary_field_name != "summary":
            kwargs["summary_field_name"] = self.summary_field_name
        return name, path, args, kwargs

    def pre_save(self, model_instance, add):
        """
        Process the field value before saving to the database.
        Generates summary and embedding if the content has changed.
        """
        # Get current text value
        current_text = getattr(model_instance, self.attname)

        # Skip processing if text is empty
        if not current_text or not current_text.strip():
            return current_text

        # Determine if we need to regenerate summary and embedding
        has_changed = self._has_field_changed(model_instance, current_text)

        if has_changed:
            try:
                # Generate summary (~100 tokens)
                summary = self.generate_summary(current_text)

                # Generate embedding vector from summary
                embedding = self.generate_embedding(summary)

                # Set summary and embedding fields on model instance
                setattr(model_instance, self.summary_field_name, summary)
                setattr(model_instance, self.embedding_field_name, embedding)

            except Exception as e:
                print(f"Error processing SummarizedVectorField: {e}")
                # Set empty values on error to avoid breaking saves
                setattr(model_instance, self.summary_field_name, "")
                setattr(model_instance, self.embedding_field_name, None)

        return current_text

    def _has_field_changed(self, instance, current_text):
        """
        Check if the field value has changed since the last save.
        
        Args:
            instance: The model instance
            current_text: Current field value
            
        Returns:
            bool: True if the field has changed or is new
        """
        if instance._state.adding:
            return True  # New instance always triggers generation
        else:
            # Check database for previous value
            try:
                old_value = (
                    instance.__class__.objects.filter(pk=instance.pk)
                    .values_list(self.attname, flat=True)
                    .first()
                )
                return old_value != current_text
            except Exception:
                # If we can't determine the old value, assume it changed
                return True