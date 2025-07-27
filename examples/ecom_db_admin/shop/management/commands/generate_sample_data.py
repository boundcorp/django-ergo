"""
Management command to generate sample data for the EcomDBAdmin example.
"""
import random
from decimal import Decimal
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from django_ergo.models import Knowledgebase, Article, UserChat, ChatMessage
from shop.models import Product, Customer, Order, OrderItem


class Command(BaseCommand):
    help = 'Generate sample data for the EcomDBAdmin example'
    
    def handle(self, *args, **options):
        self.stdout.write('Generating sample data...')
        
        # Create admin user
        admin_user, created = User.objects.get_or_create(
            username='admin',
            defaults={
                'email': 'admin@example.com',
                'is_staff': True,
                'is_superuser': True,
            }
        )
        if created:
            admin_user.set_password('admin123')
            admin_user.save()
            self.stdout.write(self.style.SUCCESS('Created admin user (admin/admin123)'))
        
        # Create Shop Wiki knowledge base
        shop_wiki, _ = Knowledgebase.objects.get_or_create(
            name='Shop Wiki',
            defaults={
                'description': 'E-commerce store policies, procedures, and business knowledge',
                'owner': admin_user
            }
        )
        
        # Create initial KB articles
        self._create_kb_articles(shop_wiki)
        
        # Create products
        products = self._create_products()
        
        # Create customers and orders
        customers = self._create_customers()
        self._create_orders(customers, products)
        
        # Create chat history with corrections
        self._create_chat_history(admin_user, shop_wiki)
        
        self.stdout.write(self.style.SUCCESS('Sample data generation complete!'))
    
    def _create_kb_articles(self, kb):
        """Create initial knowledge base articles."""
        articles_data = [
            {
                'title': 'Return Policy',
                'content': '''**Return Policy**

Customers can return items within 30 days of purchase for a full refund.

Requirements:
- Item must be in original condition
- Original packaging required
- Receipt or order number needed

Process:
1. Contact customer service
2. Receive return authorization
3. Ship item back
4. Refund processed within 5-7 business days''',
                'tags': ['return', 'policy', 'refund']
            },
            {
                'title': 'Shipping Information',
                'content': '''**Shipping Information**

Standard Shipping:
- 5-7 business days
- $5.99 for orders under $50
- FREE for orders over $50

Express Shipping:
- 2-3 business days
- $15.99 flat rate

International Shipping:
- 10-15 business days
- Rates vary by location''',
                'tags': ['shipping', 'delivery', 'rates']
            },
            {
                'title': 'Business Hours',
                'content': '''**Business Hours**

Customer Service:
- Monday-Friday: 9 AM - 6 PM EST
- Saturday: 10 AM - 4 PM EST
- Sunday: Closed

Online orders can be placed 24/7''',
                'tags': ['hours', 'contact', 'support']
            }
        ]
        
        for data in articles_data:
            Article.objects.get_or_create(
                knowledgebase=kb,
                title=data['title'],
                defaults={
                    'content': data['content'],
                    'tags': data['tags']
                }
            )
    
    def _create_products(self):
        """Create sample products."""
        categories = ['Electronics', 'Clothing', 'Home & Garden', 'Sports', 'Books']
        
        products_data = [
            # Electronics
            ('Wireless Headphones Pro', 'WH-PRO-001', 'Premium noise-cancelling wireless headphones with 30-hour battery life', 149.99, 50, 'Electronics'),
            ('Smart Watch Ultra', 'SW-ULTRA-001', 'Advanced fitness tracking and health monitoring smartwatch', 299.99, 25, 'Electronics'),
            ('4K Webcam', 'WC-4K-001', 'Professional 4K webcam with auto-focus and noise reduction', 89.99, 100, 'Electronics'),
            ('Portable Charger 20000mAh', 'PC-20K-001', 'High-capacity portable charger with fast charging support', 49.99, 200, 'Electronics'),
            ('Bluetooth Speaker Mini', 'BS-MINI-001', 'Compact waterproof Bluetooth speaker with 360° sound', 39.99, 150, 'Electronics'),
            
            # Clothing
            ('Cotton T-Shirt Classic', 'TS-COT-001', 'Premium 100% cotton t-shirt in various colors', 24.99, 300, 'Clothing'),
            ('Denim Jeans Slim Fit', 'JN-SLIM-001', 'Modern slim fit denim jeans with stretch fabric', 79.99, 150, 'Clothing'),
            ('Running Shoes Pro', 'RS-PRO-001', 'Professional running shoes with advanced cushioning', 129.99, 75, 'Clothing'),
            ('Winter Jacket Thermal', 'WJ-THRM-001', 'Insulated winter jacket with waterproof coating', 199.99, 50, 'Clothing'),
            ('Yoga Pants Flex', 'YP-FLEX-001', 'High-performance yoga pants with 4-way stretch', 69.99, 100, 'Clothing'),
            
            # Home & Garden
            ('Smart LED Bulb Set', 'LED-SET-001', 'WiFi-enabled color changing LED bulbs (4-pack)', 59.99, 80, 'Home & Garden'),
            ('Indoor Plant Collection', 'PL-COL-001', 'Starter set of 5 easy-care indoor plants', 89.99, 30, 'Home & Garden'),
            ('Kitchen Knife Set Pro', 'KN-PRO-001', 'Professional 7-piece kitchen knife set with block', 149.99, 40, 'Home & Garden'),
            ('Robot Vacuum Smart', 'RV-SMART-001', 'AI-powered robot vacuum with mapping technology', 399.99, 20, 'Home & Garden'),
            ('Garden Tool Kit', 'GT-KIT-001', 'Complete 12-piece garden tool kit with carry bag', 79.99, 60, 'Home & Garden'),
            
            # Sports
            ('Yoga Mat Premium', 'YM-PREM-001', 'Extra-thick non-slip yoga mat with alignment guides', 49.99, 120, 'Sports'),
            ('Resistance Bands Set', 'RB-SET-001', 'Complete resistance band set with door anchor', 34.99, 200, 'Sports'),
            ('Water Bottle Insulated', 'WB-INS-001', '32oz insulated stainless steel water bottle', 29.99, 250, 'Sports'),
            ('Dumbbells Adjustable Set', 'DB-ADJ-001', 'Adjustable dumbbell set 5-50 lbs', 299.99, 30, 'Sports'),
            ('Fitness Tracker Basic', 'FT-BASIC-001', 'Essential fitness tracker with heart rate monitor', 79.99, 100, 'Sports'),
            
            # Books
            ('Python Programming Guide', 'BK-PY-001', 'Complete guide to Python programming for all levels', 39.99, 150, 'Books'),
            ('Business Strategy 101', 'BK-BS-001', 'Essential business strategy concepts and frameworks', 29.99, 100, 'Books'),
            ('Mindfulness Daily', 'BK-MD-001', 'Daily mindfulness practices and meditation guide', 24.99, 200, 'Books'),
            ('Cooking Basics Illustrated', 'BK-CB-001', 'Illustrated guide to fundamental cooking techniques', 34.99, 80, 'Books'),
            ('Digital Marketing Mastery', 'BK-DM-001', 'Comprehensive digital marketing strategies guide', 44.99, 120, 'Books'),
        ]
        
        products = []
        for name, sku, description, price, stock, category in products_data:
            product, _ = Product.objects.get_or_create(
                sku=sku,
                defaults={
                    'name': name,
                    'description': description,
                    'price': Decimal(str(price)),
                    'stock_quantity': stock,
                    'category': category,
                    'is_active': True
                }
            )
            products.append(product)
        
        self.stdout.write(f'Created {len(products)} products')
        return products
    
    def _create_customers(self):
        """Create sample customers."""
        customers_data = [
            ('john_doe', 'John', 'Doe', 'john@example.com', 'Acme Corp', '555-0101', '123 Main St', '', 'New York', 'NY', '10001'),
            ('jane_smith', 'Jane', 'Smith', 'jane@example.com', '', '555-0102', '456 Oak Ave', 'Apt 2B', 'Los Angeles', 'CA', '90001'),
            ('bob_wilson', 'Bob', 'Wilson', 'bob@example.com', 'Tech Solutions', '555-0103', '789 Pine Rd', '', 'Chicago', 'IL', '60601'),
            ('alice_brown', 'Alice', 'Brown', 'alice@example.com', '', '555-0104', '321 Elm St', '', 'Houston', 'TX', '77001'),
            ('charlie_davis', 'Charlie', 'Davis', 'charlie@example.com', 'Global Imports', '555-0105', '654 Maple Dr', 'Suite 100', 'Phoenix', 'AZ', '85001'),
        ]
        
        customers = []
        for username, first, last, email, company, phone, addr1, addr2, city, state, postal in customers_data:
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={
                    'first_name': first,
                    'last_name': last,
                    'email': email
                }
            )
            
            customer, _ = Customer.objects.get_or_create(
                user=user,
                defaults={
                    'company_name': company,
                    'phone': phone,
                    'address_line1': addr1,
                    'address_line2': addr2,
                    'city': city,
                    'state': state,
                    'postal_code': postal
                }
            )
            customers.append(customer)
        
        self.stdout.write(f'Created {len(customers)} customers')
        return customers
    
    def _create_orders(self, customers, products):
        """Create sample orders."""
        statuses = ['pending', 'processing', 'shipped', 'delivered']
        
        for i in range(20):
            customer = random.choice(customers)
            status = random.choice(statuses)
            
            order = Order.objects.create(
                order_number=f'ORD-2024-{1000 + i}',
                customer=customer,
                status=status,
                shipping_amount=Decimal('10.00') if random.random() > 0.5 else Decimal('5.99'),
                created_at=timezone.now() - timedelta(days=random.randint(1, 30))
            )
            
            # Add 1-5 items to each order
            num_items = random.randint(1, 5)
            selected_products = random.sample(products, num_items)
            
            for product in selected_products:
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    quantity=random.randint(1, 3),
                    unit_price=product.price
                )
            
            # Set shipped/delivered dates for appropriate statuses
            if status in ['shipped', 'delivered']:
                order.shipped_at = order.created_at + timedelta(days=1)
            if status == 'delivered':
                order.delivered_at = order.shipped_at + timedelta(days=random.randint(2, 5))
            
            order.save()
        
        self.stdout.write('Created 20 orders with items')
    
    def _create_chat_history(self, user, kb):
        """Create sample chat history with corrections."""
        # Chat 1: Return policy correction
        chat1 = UserChat.objects.create(
            user=user,
            title='Return policy inquiry'
        )
        
        ChatMessage.objects.create(
            chat=chat1,
            role='user',
            content='What is your return policy?'
        )
        
        ChatMessage.objects.create(
            chat=chat1,
            role='assistant',
            content='Our return policy allows customers to return items within 30 days of purchase for a full refund. Items must be in original condition with packaging.'
        )
        
        ChatMessage.objects.create(
            chat=chat1,
            role='user',
            content='Actually, we changed it to 45 days last month for the holiday season'
        )
        
        ChatMessage.objects.create(
            chat=chat1,
            role='assistant',
            content='Thank you for the correction! I\'ll update my knowledge. The return policy now allows 45 days for returns during the holiday season.'
        )
        
        # Chat 2: Shipping rate correction
        chat2 = UserChat.objects.create(
            user=user,
            title='Shipping information'
        )
        
        ChatMessage.objects.create(
            chat=chat2,
            role='user',
            content='How much is standard shipping?'
        )
        
        ChatMessage.objects.create(
            chat=chat2,
            role='assistant',
            content='Standard shipping is $5.99 for orders under $50, and free for orders over $50.'
        )
        
        ChatMessage.objects.create(
            chat=chat2,
            role='user',
            content='No, it\'s free for orders over $75 now, not $50'
        )
        
        ChatMessage.objects.create(
            chat=chat2,
            role='assistant',
            content='I apologize for the error. You\'re correct - standard shipping is now free for orders over $75, not $50.'
        )
        
        # Chat 3: Business hours correction
        chat3 = UserChat.objects.create(
            user=user,
            title='Customer service hours'
        )
        
        ChatMessage.objects.create(
            chat=chat3,
            role='user',
            content='What are your customer service hours?'
        )
        
        ChatMessage.objects.create(
            chat=chat3,
            role='assistant',
            content='Our customer service is available Monday-Friday 9 AM to 6 PM EST, and Saturday 10 AM to 4 PM EST. We\'re closed on Sundays.'
        )
        
        ChatMessage.objects.create(
            chat=chat3,
            role='user',
            content='That\'s outdated, we now offer Sunday support from 12 PM to 5 PM EST'
        )
        
        ChatMessage.objects.create(
            chat=chat3,
            role='assistant',
            content='Thank you for updating me! Customer service is now also available on Sundays from 12 PM to 5 PM EST.'
        )
        
        self.stdout.write('Created sample chat history with corrections')