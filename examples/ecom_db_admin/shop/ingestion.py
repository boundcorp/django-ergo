"""
Chat history ingestion for learning from user corrections.
"""
import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from django.contrib.auth.models import User
from django_ergo.models import UserChat, ChatMessage, Knowledgebase, Article

logger = logging.getLogger(__name__)


class UserChatHistoryKBIngestion:
    """
    Ingests chat history to extract facts and corrections for knowledge base updates.
    
    This helper identifies when users correct the assistant's responses and 
    automatically creates or updates knowledge base articles with the corrected information.
    """
    
    # Patterns that indicate a correction
    CORRECTION_PATTERNS = [
        r"actually[,\s]+(.+)",
        r"no[,\s]+(?:it's|its|it is)\s+(.+)",
        r"(?:that's|thats)\s+(?:not right|wrong|incorrect)[,\s]+(.+)",
        r"correction:\s*(.+)",
        r"(?:the correct|the right)\s+(?:answer|information)\s+is\s+(.+)",
        r"(?:you're|youre|you are)\s+wrong[,\s]+(.+)",
        r"(?:that's|thats)\s+outdated[,\s]+(.+)",
        r"(?:we changed|it changed|now it's|now its)\s+(.+)",
    ]
    
    # Topics to categorize corrections
    TOPIC_KEYWORDS = {
        'return_policy': ['return', 'refund', 'exchange', 'days', 'policy'],
        'shipping': ['shipping', 'delivery', 'ship', 'deliver', 'transit'],
        'pricing': ['price', 'cost', 'discount', 'sale', 'coupon'],
        'inventory': ['stock', 'inventory', 'available', 'quantity', 'in stock'],
        'product_info': ['product', 'item', 'description', 'feature', 'specification'],
        'business_hours': ['hours', 'open', 'closed', 'business hours', 'schedule'],
        'contact': ['contact', 'email', 'phone', 'support', 'customer service'],
    }
    
    def __init__(self, knowledgebase: Knowledgebase):
        self.knowledgebase = knowledgebase
    
    def ingest_user_chat(self, chat: UserChat) -> List[Article]:
        """
        Ingest a single chat conversation and extract corrections.
        
        Returns a list of created or updated articles.
        """
        corrections = self._extract_corrections(chat)
        articles = []
        
        for correction in corrections:
            article = self._create_or_update_article(correction)
            if article:
                articles.append(article)
        
        return articles
    
    def ingest_all_chats(self, user: Optional[User] = None) -> Dict[str, Any]:
        """
        Ingest all chat history, optionally filtered by user.
        
        Returns statistics about the ingestion process.
        """
        query = UserChat.objects.all()
        if user:
            query = query.filter(user=user)
        
        stats = {
            'chats_processed': 0,
            'corrections_found': 0,
            'articles_created': 0,
            'articles_updated': 0,
            'errors': 0
        }
        
        for chat in query:
            try:
                articles = self.ingest_user_chat(chat)
                stats['chats_processed'] += 1
                
                for article in articles:
                    if article._state.adding:  # New article
                        stats['articles_created'] += 1
                    else:
                        stats['articles_updated'] += 1
                
                corrections = self._extract_corrections(chat)
                stats['corrections_found'] += len(corrections)
                
            except Exception as e:
                logger.error(f"Error ingesting chat {chat.id}: {str(e)}")
                stats['errors'] += 1
        
        return stats
    
    def _extract_corrections(self, chat: UserChat) -> List[Dict[str, Any]]:
        """Extract corrections from a chat conversation."""
        corrections = []
        messages = list(chat.messages.order_by('created_at'))
        
        for i in range(1, len(messages)):
            current_msg = messages[i]
            
            # Only look at user messages
            if current_msg.role != 'user':
                continue
            
            # Check if this message contains a correction
            correction_match = self._is_correction(current_msg.content)
            if not correction_match:
                continue
            
            # Find the previous assistant message being corrected
            assistant_msg = None
            for j in range(i - 1, -1, -1):
                if messages[j].role == 'assistant':
                    assistant_msg = messages[j]
                    break
            
            if not assistant_msg:
                continue
            
            # Extract the topic from both messages
            topic = self._categorize_topic(
                assistant_msg.content + " " + current_msg.content
            )
            
            corrections.append({
                'original_statement': self._extract_key_statement(assistant_msg.content),
                'correction': correction_match,
                'topic': topic,
                'user_message': current_msg.content,
                'assistant_message': assistant_msg.content,
                'timestamp': current_msg.created_at
            })
        
        return corrections
    
    def _is_correction(self, message: str) -> Optional[str]:
        """Check if a message contains a correction and extract it."""
        message_lower = message.lower()
        
        for pattern in self.CORRECTION_PATTERNS:
            match = re.search(pattern, message_lower, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1).strip()
        
        return None
    
    def _categorize_topic(self, text: str) -> str:
        """Categorize the topic of a correction."""
        text_lower = text.lower()
        
        # Count keyword matches for each topic
        topic_scores = {}
        for topic, keywords in self.TOPIC_KEYWORDS.items():
            score = sum(1 for keyword in keywords if keyword in text_lower)
            if score > 0:
                topic_scores[topic] = score
        
        # Return the topic with the highest score
        if topic_scores:
            return max(topic_scores, key=topic_scores.get)
        
        return 'general'
    
    def _extract_key_statement(self, text: str) -> str:
        """Extract the key statement from assistant's message."""
        # Look for sentences containing key information
        sentences = text.split('.')
        
        # Prioritize sentences with numbers, policies, or specific claims
        for sentence in sentences:
            if any(word in sentence.lower() for word in ['policy', 'days', 'hours', 'price', '%']):
                return sentence.strip()
        
        # Return the first substantial sentence
        for sentence in sentences:
            if len(sentence.strip()) > 20:
                return sentence.strip()
        
        return text[:200] + "..." if len(text) > 200 else text
    
    def _create_or_update_article(self, correction: Dict[str, Any]) -> Optional[Article]:
        """Create or update a knowledge base article from a correction."""
        try:
            # Generate a title for the article
            title = f"{correction['topic'].replace('_', ' ').title()} - Updated Information"
            
            # Check if an article already exists for this topic
            existing_article = Article.objects.filter(
                knowledgebase=self.knowledgebase,
                title__icontains=correction['topic'].replace('_', ' ')
            ).first()
            
            if existing_article:
                # Update existing article
                existing_article.content += f"\n\n**Update ({correction['timestamp'].strftime('%Y-%m-%d')}):**\n"
                existing_article.content += f"- Previous information: {correction['original_statement']}\n"
                existing_article.content += f"- Corrected information: {correction['correction']}\n"
                existing_article.save()
                return existing_article
            else:
                # Create new article
                content = f"**Topic**: {correction['topic'].replace('_', ' ').title()}\n\n"
                content += f"**Current Information**: {correction['correction']}\n\n"
                content += f"**History**:\n"
                content += f"- Original statement: {correction['original_statement']}\n"
                content += f"- Corrected on: {correction['timestamp'].strftime('%Y-%m-%d')}\n"
                content += f"- User correction: \"{correction['user_message']}\"\n"
                
                article = Article.objects.create(
                    knowledgebase=self.knowledgebase,
                    title=title,
                    content=content,
                    tags=[correction['topic'], 'correction', 'user_feedback']
                )
                return article
                
        except Exception as e:
            logger.error(f"Error creating/updating article: {str(e)}")
            return None


def run_ingestion(knowledgebase_name: str = "Shop Wiki") -> Dict[str, Any]:
    """
    Run the ingestion process on all chat history.
    
    This is typically called from a management command or scheduled task.
    """
    try:
        kb = Knowledgebase.objects.get(name=knowledgebase_name)
    except Knowledgebase.DoesNotExist:
        logger.error(f"Knowledgebase '{knowledgebase_name}' not found")
        return {'error': f"Knowledgebase '{knowledgebase_name}' not found"}
    
    ingestion = UserChatHistoryKBIngestion(kb)
    stats = ingestion.ingest_all_chats()
    
    logger.info(f"Ingestion complete: {stats}")
    return stats