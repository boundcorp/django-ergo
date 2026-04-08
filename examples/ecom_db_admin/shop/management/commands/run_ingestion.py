"""
Management command to run ingestion workflows and test KB learning.
"""
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django_ergo.models import Knowledgebase, Article, UserChat
from shop.ingestion import run_chat_history_ingestion


class Command(BaseCommand):
    help = 'Run ingestion workflows to test KB learning from chat history'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--user',
            type=str,
            default='admin',
            help='Username to run ingestion for (default: admin)'
        )
        parser.add_argument(
            '--kb-name',
            type=str,
            default='Shop Wiki',
            help='Knowledge base name (default: Shop Wiki)'
        )
        parser.add_argument(
            '--topic',
            type=str,
            default='business configuration',
            help='Topic to focus ingestion on (default: business configuration)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually running ingestion'
        )
    
    def handle(self, *args, **options):
        username = options['user']
        kb_name = options['kb_name']
        topic = options['topic']
        dry_run = options['dry_run']
        
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(f'User "{username}" not found. Please create user first or run generate_sample_data.')
            )
            return
        
        # Get existing chats
        chats = UserChat.objects.filter(user=user)
        if not chats.exists():
            self.stdout.write(
                self.style.WARNING('No chat history found. Run generate_sample_data first.')
            )
            return
        
        self.stdout.write(f'Found {chats.count()} chat conversations for user {username}')
        
        # Show chat summaries
        for chat in chats:
            message_count = chat.messages.count()
            self.stdout.write(f'  - "{chat.title}": {message_count} messages')
            
            # Look for correction patterns
            corrections_found = []
            for msg in chat.messages.filter(role='user'):
                content_lower = msg.content.lower()
                if any(word in content_lower for word in ['no', 'actually', 'sorry', 'wrong', 'changed', 'correction']):
                    corrections_found.append(msg.content[:50] + "...")
            
            if corrections_found:
                self.stdout.write(f'    Potential corrections: {len(corrections_found)}')
                for correction in corrections_found:
                    self.stdout.write(f'      "{correction}"')
        
        # Get current KB state
        try:
            kb = Knowledgebase.objects.get(name=kb_name, owner=user)
            initial_article_count = kb.articles.count()
            self.stdout.write(f'\nKnowledge base "{kb_name}" currently has {initial_article_count} articles')
            
            if initial_article_count > 0:
                self.stdout.write('Existing articles:')
                for article in kb.articles.all():
                    self.stdout.write(f'  - {article.title} ({article.hierarchy_code})')
        except Knowledgebase.DoesNotExist:
            initial_article_count = 0
            self.stdout.write(f'\nKnowledge base "{kb_name}" does not exist yet (will be created)')
        
        if dry_run:
            self.stdout.write(self.style.WARNING('\nDRY RUN: Would run ingestion but not making actual changes'))
            return
        
        # Run ingestion
        self.stdout.write(f'\nRunning chat history ingestion for topic: {topic}')
        
        try:
            result = run_chat_history_ingestion(
                user=user,
                kb_name=kb_name,
                topic=topic
            )
            
            self.stdout.write(self.style.SUCCESS(f'Ingestion completed: {result}'))
            
            # Check what changed
            try:
                kb = Knowledgebase.objects.get(name=kb_name, owner=user)
                final_article_count = kb.articles.count()
                
                if final_article_count > initial_article_count:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Created {final_article_count - initial_article_count} new articles'
                        )
                    )
                    
                    # Show new articles
                    self.stdout.write('\nNew/Updated articles:')
                    for article in kb.articles.all():
                        self.stdout.write(f'  - {article.title} ({article.hierarchy_code})')
                        if 'EST' in article.content or 'timezone' in article.content.lower():
                            self.stdout.write('    ⭐ Contains timezone information!')
                        if 'correction' in article.content.lower():
                            self.stdout.write('    📝 Contains correction information!')
                else:
                    self.stdout.write('No new articles were created')
                    
            except Knowledgebase.DoesNotExist:
                self.stdout.write(self.style.WARNING('Knowledge base was not created'))
                
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Ingestion failed: {str(e)}')
            )
        
        # Test querying the KB
        self.stdout.write('\n' + '='*50)
        self.stdout.write('TESTING KB QUERIES AFTER INGESTION')
        self.stdout.write('='*50)
        
        try:
            kb = Knowledgebase.objects.get(name=kb_name, owner=user)
            
            # Test timezone query
            timezone_articles = kb.articles.filter(content__icontains='EST')
            if timezone_articles.exists():
                self.stdout.write(self.style.SUCCESS('\n✅ Timezone information found in KB:'))
                for article in timezone_articles:
                    self.stdout.write(f'   Article: "{article.title}"')
                    # Show relevant excerpt
                    content_lines = article.content.split('\n')
                    for line in content_lines:
                        if 'EST' in line or 'timezone' in line.lower():
                            self.stdout.write(f'   Content: "{line.strip()}"')
                            break
                            
                self.stdout.write('\n💡 Future "get me today\'s sales" queries should now use EST timezone!')
            else:
                self.stdout.write(self.style.WARNING('\n⚠️  No timezone information found in KB'))
            
            # Test business hours
            hours_articles = kb.articles.filter(content__icontains='hours')
            if hours_articles.exists():
                self.stdout.write(self.style.SUCCESS('\n✅ Business hours information found in KB'))
            
            # Test return policy
            return_articles = kb.articles.filter(content__icontains='return')
            if return_articles.exists():
                self.stdout.write(self.style.SUCCESS('\n✅ Return policy information found in KB'))
                
        except Knowledgebase.DoesNotExist:
            self.stdout.write(self.style.ERROR('\n❌ Knowledge base not found after ingestion'))
        
        self.stdout.write('\n' + '='*50)
        self.stdout.write('INGESTION TEST COMPLETE')
        self.stdout.write('='*50)