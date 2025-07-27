"""
Django Ergo - Tool Approval System Demonstration

This script demonstrates how the tool approval system works with:
1. Tools that require approval vs. auto-approved tools
2. Tool whitelisting for workflows  
3. Approval workflow with pause/resume
4. Django signals for external approval interfaces

Usage:
    from django_ergo.approval_demo import demonstrate_approval_workflow
    demonstrate_approval_workflow()
"""

from django_ergo.models import Workflow, UserChat, MessageType
from django_ergo.workflow_engine import workflow_engine, tool_approval_requested, workflow_paused
from django.contrib.auth import get_user_model

User = get_user_model()


def demonstrate_approval_workflow():
    """
    Demonstrate the tool approval system with a complete workflow.
    """
    print("🔒 Django Ergo - Tool Approval System Demo")
    print("=" * 50)
    
    # 1. Create a demo workflow with tool whitelisting
    workflow = Workflow.objects.create(
        name="Demo Approval Workflow",
        description="Demonstrates tool approval system",
        instructions="You are a helpful assistant. You can search knowledge bases and manage articles.",
        tools_config={
            "approved_tools": ["search_user_kb", "get_user_articles"],  # Whitelisted tools
            "require_approval": ["delete_user_article", "send_email_notification"]  # These need approval
        }
    )
    
    # 2. Create a demo user and chat
    user, _ = User.objects.get_or_create(username="demo_user", defaults={"email": "demo@example.com"})
    chat = UserChat.objects.create(
        user=user,
        workflow=workflow,
        title="Tool Approval Demo Chat"
    )
    
    print(f"✅ Created workflow: {workflow.name}")
    print(f"✅ Created chat for user: {user.username}")
    print()
    
    # 3. Demonstrate auto-approved tool (whitelisted)
    print("🟢 Testing AUTO-APPROVED tool (whitelisted)...")
    message1 = workflow_engine.process_message(
        chat=chat,
        message_content="Please search my knowledge base for 'django'"
    )
    print(f"   Result: {message1.message_type}")
    print(f"   Content: {message1.content[:100]}...")
    print()
    
    # 4. Demonstrate tool requiring approval
    print("🔒 Testing APPROVAL-REQUIRED tool...")
    message2 = workflow_engine.process_message(
        chat=chat,
        message_content="Delete the article with ID 'demo-123' from my knowledge base"
    )
    print(f"   Result: {message2.message_type}")
    
    if message2.message_type == MessageType.TOOL_APPROVAL_REQUEST:
        print("   ✅ Tool approval request created successfully!")
        print(f"   Content preview: {message2.content[:200]}...")
        
        # Get pending approvals from metadata
        pending_approvals = message2.metadata.get("pending_approvals", [])
        print(f"   Tools pending approval: {len(pending_approvals)}")
        
        for approval in pending_approvals:
            print(f"     - {approval['tool_name']}: {approval['description']}")
        
        # 5. Simulate approval
        print("\n🎯 Simulating tool approval...")
        approved_tools = [approval["tool_call_id"] for approval in pending_approvals]
        
        final_message = workflow_engine.approve_tool_execution(
            chat=chat,
            approval_message=message2,
            approved_tools=approved_tools,
            denied_tools=[]
        )
        
        print(f"   Final result: {final_message.message_type}")
        print(f"   Content: {final_message.content[:100]}...")
    
    print()
    print("🎉 Tool Approval System Demo Complete!")
    return workflow, chat


def setup_approval_signals():
    """
    Demonstrate how to set up Django signals for approval handling.
    """
    
    def handle_tool_approval_request(sender, **kwargs):
        """Handle tool approval requests via Django signals."""
        context = kwargs['context']
        pending_approvals = kwargs['pending_approvals']
        approval_message = kwargs['approval_message']
        
        print(f"🔔 SIGNAL: Tool approval requested for user {context.user.username}")
        print(f"   Pending tools: {[a['tool_name'] for a in pending_approvals]}")
        print(f"   Approval message ID: {approval_message.id}")
        
        # Here you could:
        # - Send push notifications
        # - Create approval tasks in a queue
        # - Log to external systems
        # - Update UI state
    
    def handle_workflow_paused(sender, **kwargs):
        """Handle workflow pause events."""
        context = kwargs['context']
        approval_message = kwargs['approval_message']
        
        print(f"⏸️  SIGNAL: Workflow paused for user {context.user.username}")
        print(f"   Chat ID: {context.chat.id}")
        print(f"   Waiting for approval on message: {approval_message.id}")
    
    # Connect the signals
    tool_approval_requested.connect(handle_tool_approval_request)
    workflow_paused.connect(handle_workflow_paused)
    
    print("✅ Django signals configured for tool approval system")


def demonstrate_whitelisting():
    """
    Show how tool whitelisting works for different workflows.
    """
    print("\n🔐 Tool Whitelisting Demonstration")
    print("-" * 40)
    
    # Workflow 1: Strict security (no whitelisted tools)
    strict_workflow = Workflow.objects.create(
        name="Strict Security Workflow",
        description="All tools require approval",
        instructions="You are a security-conscious assistant.",
        tools_config={
            "approved_tools": [],  # No auto-approved tools
        }
    )
    
    # Workflow 2: Permissive (many whitelisted tools)
    permissive_workflow = Workflow.objects.create(
        name="Permissive Workflow", 
        description="Most read-only tools are auto-approved",
        instructions="You are a helpful assistant with broad permissions.",
        tools_config={
            "approved_tools": [
                "search_user_kb",
                "get_user_articles", 
                "search_grower_kb",
                "search_garden_kb"
            ]
        }
    )
    
    print(f"✅ Strict workflow: {len(workflow_engine.get_tool_whitelist(strict_workflow))} whitelisted tools")
    print(f"✅ Permissive workflow: {len(workflow_engine.get_tool_whitelist(permissive_workflow))} whitelisted tools")
    
    # Test whitelisting
    test_tool = "search_user_kb"
    print(f"\nTesting tool '{test_tool}':")
    print(f"  Strict workflow allows: {workflow_engine.is_tool_whitelisted(strict_workflow, test_tool)}")
    print(f"  Permissive workflow allows: {workflow_engine.is_tool_whitelisted(permissive_workflow, test_tool)}")
    
    return strict_workflow, permissive_workflow


if __name__ == "__main__":
    """
    Run the complete demonstration.
    """
    # Set up signal handlers
    setup_approval_signals()
    
    # Run main demo
    workflow, chat = demonstrate_approval_workflow()
    
    # Show whitelisting
    demonstrate_whitelisting()
    
    print("\n" + "=" * 50)
    print("🎯 Key Features Demonstrated:")
    print("  ✅ Tool approval workflow with pause/resume")
    print("  ✅ Context serialization for workflow continuity")
    print("  ✅ Tool whitelisting for security control")
    print("  ✅ Django signals for external approval interfaces")
    print("  ✅ New message types for approval tracking")
    print("  ✅ Approval-required vs auto-approved tools")
    print("=" * 50)