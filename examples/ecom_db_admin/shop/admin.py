from django.contrib import admin
from django.utils.html import format_html
from .models import Product, Customer, Order, OrderItem


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['name', 'sku', 'price', 'stock_quantity', 'category', 'is_active', 'created_at']
    list_filter = ['category', 'is_active', 'created_at']
    search_fields = ['name', 'sku', 'description']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'sku', 'category', 'is_active')
        }),
        ('Details', {
            'fields': ('description', 'price', 'stock_quantity')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.prefetch_related('orderitem_set')


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['user', 'company_name', 'city', 'state', 'created_at']
    list_filter = ['state', 'country', 'created_at']
    search_fields = ['user__username', 'user__email', 'company_name', 'city']
    readonly_fields = ['created_at', 'full_address_display']
    
    fieldsets = (
        ('User Information', {
            'fields': ('user', 'company_name', 'phone')
        }),
        ('Address', {
            'fields': ('address_line1', 'address_line2', 'city', 'state', 'postal_code', 'country')
        }),
        ('Additional Info', {
            'fields': ('created_at', 'full_address_display'),
            'classes': ('collapse',)
        }),
    )
    
    def full_address_display(self, obj):
        return format_html('<pre>{}</pre>', obj.full_address)
    full_address_display.short_description = 'Full Address'


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ['line_total']
    fields = ['product', 'quantity', 'unit_price', 'line_total']


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'customer', 'status', 'total_amount', 'item_count', 'created_at']
    list_filter = ['status', 'created_at', 'shipped_at']
    search_fields = ['order_number', 'customer__user__username', 'customer__user__email']
    readonly_fields = ['subtotal', 'tax_amount', 'total_amount', 'created_at', 'updated_at']
    inlines = [OrderItemInline]
    
    fieldsets = (
        ('Order Information', {
            'fields': ('order_number', 'customer', 'status')
        }),
        ('Financial', {
            'fields': ('subtotal', 'tax_amount', 'shipping_amount', 'total_amount')
        }),
        ('Dates', {
            'fields': ('created_at', 'updated_at', 'shipped_at', 'delivered_at')
        }),
        ('Notes', {
            'fields': ('notes',),
            'classes': ('collapse',)
        }),
    )
    
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:
            obj.calculate_totals()
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('customer', 'customer__user').prefetch_related('items', 'items__product')