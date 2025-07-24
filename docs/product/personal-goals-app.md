# Personal Goals Tracking App - Product Documentation

## Overview

The Personal Goals Tracking App is a Django application built on Django Ergo that helps users set, track, and achieve their personal goals through AI-powered coaching and knowledge management. The app leverages Ergo's workflow engine, knowledge management system, and conversational AI to provide personalized guidance and accountability.

## Core Features

### Goal Management
- **Goal Creation**: Users can create SMART goals with deadlines and success criteria
- **Goal Categories**: Support for different goal types (health, career, learning, relationships, etc.)
- **Goal Hierarchy**: Break down large goals into smaller, actionable milestones
- **Progress Tracking**: Visual progress indicators and milestone completion
- **Goal Templates**: Pre-built goal templates for common objectives

### AI-Powered Coaching
- **Daily Check-ins**: Automated daily conversations to review progress
- **Personalized Advice**: AI coach provides tailored suggestions based on user history
- **Motivation Support**: Encouragement and motivation based on user's emotional state
- **Obstacle Navigation**: Help identifying and overcoming barriers to success
- **Success Celebration**: Recognition and celebration of achievements

### Knowledge Management
- **Personal Knowledge Base**: Store user-specific insights, lessons learned, and strategies
- **Global Coaching Knowledge**: Curated knowledge base of goal-setting best practices
- **Learning from Experience**: System learns from user's past successes and failures
- **Resource Recommendations**: Suggest relevant articles, books, or resources

## Technical Architecture

### Data Models

#### User Profile Extension
```python
class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    coaching_preferences = models.JSONField(default=dict)
    timezone = models.CharField(max_length=50, default='UTC')
    notification_preferences = models.JSONField(default=dict)
    onboarding_completed = models.BooleanField(default=False)
```

#### Goal Management
```python
class Goal(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'Active'
        COMPLETED = 'completed', 'Completed'
        PAUSED = 'paused', 'Paused'
        ABANDONED = 'abandoned', 'Abandoned'
    
    class Category(models.TextChoices):
        HEALTH = 'health', 'Health & Fitness'
        CAREER = 'career', 'Career & Professional'
        LEARNING = 'learning', 'Learning & Education'
        RELATIONSHIPS = 'relationships', 'Relationships'
        FINANCES = 'finances', 'Financial'
        CREATIVITY = 'creativity', 'Creative & Hobbies'
        SPIRITUAL = 'spiritual', 'Spiritual & Personal Growth'
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='goals')
    title = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=20, choices=Category.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    
    # SMART Goal attributes
    specific_outcome = models.TextField(help_text="Specific, clear outcome")
    measurable_criteria = models.JSONField(help_text="Measurable success criteria")
    target_date = models.DateField()
    
    # Progress tracking
    progress_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    last_check_in = models.DateTimeField(null=True, blank=True)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class Milestone(models.Model):
    goal = models.ForeignKey(Goal, on_delete=models.CASCADE, related_name='milestones')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    target_date = models.DateField()
    completed_at = models.DateTimeField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    
    class Meta:
        ordering = ['order', 'target_date']

class CheckIn(models.Model):
    goal = models.ForeignKey(Goal, on_delete=models.CASCADE, related_name='check_ins')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    date = models.DateField(auto_now_add=True)
    progress_rating = models.IntegerField(choices=[(i, i) for i in range(1, 6)])  # 1-5 scale
    mood_rating = models.IntegerField(choices=[(i, i) for i in range(1, 6)])
    notes = models.TextField(blank=True)
    challenges_faced = models.TextField(blank=True)
    next_actions = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

### Knowledge Base Structure

#### Personal Knowledge Base (Per User)
- **Goal History**: Past goals, what worked, what didn't
- **Personal Insights**: User's own discoveries and lessons learned
- **Motivation Triggers**: What motivates this specific user
- **Obstacle Patterns**: Common challenges the user faces
- **Success Strategies**: Proven approaches that work for this user
- **Resource Library**: User's saved articles, books, videos

#### Global System Knowledge Base
- **Goal Setting Science**: Research-backed goal setting principles
- **Motivation Psychology**: Understanding motivation and behavior change
- **Habit Formation**: Science of building and breaking habits
- **Productivity Techniques**: Time management and productivity methods
- **Success Stories**: Anonymized success stories and case studies
- **Best Practices**: Proven strategies for different goal categories

### Workflow Definitions

#### Daily Check-in Workflow
```python
class DailyCheckInWorkflow:
    """
    Workflow for daily goal check-ins and coaching conversations.
    """
    name = "Daily Check-in Coach"
    description = "Helps users reflect on their daily progress and plan next steps"
    
    tools = [
        "get_user_goals",
        "get_recent_check_ins", 
        "create_check_in",
        "search_personal_knowledge",
        "search_coaching_knowledge",
        "update_goal_progress",
        "schedule_reminder"
    ]
    
    instructions = """
    You are a supportive goal coach helping the user with their daily check-in.
    
    1. Greet the user warmly and ask about their day
    2. Review their active goals and recent progress
    3. Guide them through reflection questions:
       - What progress did you make today?
       - What challenges did you face?
       - How are you feeling about your goals?
       - What will you focus on tomorrow?
    4. Provide encouragement and practical advice
    5. Help them identify next actions
    6. Record the check-in and update goal progress
    
    Be supportive, non-judgmental, and focus on progress over perfection.
    Use their personal knowledge base to reference past successes and lessons learned.
    """

class GoalPlanningWorkflow:
    """
    Workflow for creating and planning new goals.
    """
    name = "Goal Planning Assistant"
    description = "Helps users create well-structured, achievable goals"
    
    tools = [
        "get_user_profile",
        "search_goal_templates",
        "search_coaching_knowledge", 
        "create_goal",
        "create_milestones",
        "schedule_reminders"
    ]
    
    instructions = """
    You are a goal-setting expert helping users create effective goals.
    
    1. Understand what the user wants to achieve
    2. Help them make it SMART (Specific, Measurable, Achievable, Relevant, Time-bound)
    3. Break large goals into smaller milestones
    4. Suggest realistic timelines based on their other commitments
    5. Help identify potential obstacles and strategies
    6. Set up appropriate check-in schedules
    
    Use the coaching knowledge base to provide evidence-based advice.
    Reference similar successful goals from the system (anonymized).
    """
```

### Tools Implementation

#### Goal Management Tools
```python
@tool
async def get_user_goals(context: WorkflowContext, status_filter: str = "active") -> List[Dict]:
    """Get user's goals filtered by status."""
    goals = Goal.objects.filter(user=context.user, status=status_filter)
    return [goal.to_dict() for goal in goals]

@tool  
async def create_goal(context: WorkflowContext, goal_data: Dict) -> Dict:
    """Create a new goal for the user."""
    goal = Goal.objects.create(user=context.user, **goal_data)
    
    # Update user's personal knowledge base
    kb = get_user_knowledgebase(context.user)
    kb.articles.create(
        title=f"Goal: {goal.title}",
        content=f"Created goal: {goal.description}\nTarget date: {goal.target_date}",
        hierarchy_code=f"goals.{goal.id}"
    )
    
    return goal.to_dict()

@tool
async def update_goal_progress(context: WorkflowContext, goal_id: int, progress: float) -> Dict:
    """Update progress for a specific goal."""
    goal = Goal.objects.get(id=goal_id, user=context.user)
    goal.progress_percentage = progress
    goal.last_check_in = timezone.now()
    goal.save()
    
    return {"success": True, "new_progress": progress}

@tool
async def create_check_in(context: WorkflowContext, check_in_data: Dict) -> Dict:
    """Create a daily check-in record."""
    check_in = CheckIn.objects.create(user=context.user, **check_in_data)
    
    # Add insights to personal knowledge base
    if check_in.notes or check_in.challenges_faced:
        kb = get_user_knowledgebase(context.user)
        kb.articles.create(
            title=f"Check-in: {check_in.date}",
            content=f"Progress: {check_in.progress_rating}/5\n"
                   f"Mood: {check_in.mood_rating}/5\n"
                   f"Notes: {check_in.notes}\n"
                   f"Challenges: {check_in.challenges_faced}",
            hierarchy_code=f"checkins.{check_in.date.isoformat()}"
        )
    
    return check_in.to_dict()
```

#### Knowledge Search Tools
```python
@tool
async def search_personal_knowledge(context: WorkflowContext, query: str) -> List[Dict]:
    """Search user's personal knowledge base."""
    kb = get_user_knowledgebase(context.user)
    results = kb.articles.hybrid_search(query, top_k=5)
    return [{"title": r.title, "content": r.content[:500]} for r in results]

@tool
async def search_coaching_knowledge(context: WorkflowContext, query: str) -> List[Dict]:
    """Search global coaching knowledge base."""
    coaching_kb = get_coaching_knowledgebase()
    results = coaching_kb.articles.hybrid_search(query, top_k=3)
    return [{"title": r.title, "content": r.content[:500]} for r in results]
```

## User Experience Flow

### Onboarding Process
1. **Welcome & Introduction**: Explain how the AI coach works
2. **Goal Categories**: Help user identify which areas they want to focus on
3. **Initial Goals**: Create 1-3 starter goals with the planning workflow
4. **Preferences Setup**: Configure check-in frequency, notification preferences
5. **Knowledge Base Seeding**: Import any existing goal history or preferences

### Daily Usage Pattern
1. **Morning Planning**: Optional morning check-in to set daily intentions
2. **Progress Tracking**: Log progress throughout the day (optional)
3. **Evening Reflection**: Main daily check-in with the AI coach
4. **Weekly Reviews**: Deeper reflection on weekly progress and adjustments
5. **Monthly Planning**: Review and adjust goals, celebrate achievements

### Key User Interactions

#### Daily Check-in Conversation
```
AI Coach: Good evening! How did your day go with your fitness goal?

User: I managed a 30-minute walk but skipped the gym again.

AI Coach: That's great that you got your walk in! I see from your history that you've been consistent with walking but struggling with gym visits. What's making the gym challenging right now?

User: I keep running out of time after work, and by then I'm too tired.

AI Coach: I understand. Looking at your past successful periods, you mentioned that morning workouts worked well for you. Would you like to explore shifting your gym time to mornings, or would you prefer to find a different approach?

User: Yeah, mornings might work better. But I'd need to adjust my sleep schedule.

AI Coach: That's a smart connection! From what I know about habit formation, pairing a new habit with an existing routine works well. What if we start with just 2 gym mornings per week and see how that feels? I can help you plan a gradual schedule shift.
```

### Personalization Features

#### Adaptive Coaching Style
- **Communication Preferences**: Formal vs casual, brief vs detailed
- **Motivation Style**: Encouragement vs challenge-based motivation  
- **Feedback Frequency**: Daily, weekly, or user-initiated check-ins
- **Focus Areas**: Prioritize certain types of advice or coaching

#### Smart Notifications
- **Progress Reminders**: Contextual reminders based on goal deadlines
- **Motivation Boosts**: Timely encouragement during difficult periods
- **Success Celebrations**: Automatic recognition of milestones and achievements
- **Weekly Insights**: Summary of progress and upcoming priorities

## Analytics & Insights

### User Dashboard
- **Progress Overview**: Visual progress across all active goals
- **Streak Tracking**: Consecutive days of progress or check-ins
- **Mood & Energy Trends**: Track correlation between mood and goal progress
- **Success Patterns**: Identify what conditions lead to better progress
- **Challenge Analysis**: Common obstacles and how they're overcome

### System-Wide Insights (Anonymized)
- **Goal Success Rates**: Track completion rates by category and approach
- **Effective Strategies**: Identify which coaching approaches work best
- **Common Obstacles**: Understand frequent challenges across users
- **Seasonal Patterns**: How goal setting and completion varies throughout the year

## Implementation Considerations

### Privacy & Security
- **Data Isolation**: Each user's personal knowledge base is completely private
- **Anonymization**: System-wide learning uses anonymized data only
- **Data Export**: Users can export their goal history and insights
- **Deletion Rights**: Complete removal of user data upon request

### Performance Optimization
- **Caching**: Cache frequent knowledge base searches and user preferences
- **Background Processing**: Process check-ins and knowledge updates asynchronously
- **Efficient Embeddings**: Optimize embedding generation for personal content
- **Search Optimization**: Fast hybrid search across personal and global knowledge

### Integration Opportunities
- **Calendar Integration**: Sync with Google Calendar, Outlook for goal scheduling
- **Fitness Trackers**: Import data from Fitbit, Apple Health, etc.
- **Habit Tracking Apps**: Integration with existing habit tracking tools
- **Social Features**: Optional goal sharing and accountability partners

## Success Metrics

### User Engagement
- Daily active users and check-in completion rates
- Goal completion rates compared to industry benchmarks
- User retention and long-term engagement
- Satisfaction scores with AI coaching interactions

### Goal Achievement
- Percentage of goals completed within target timeframes
- Average time to complete different types of goals
- Progress consistency (regular vs sporadic progress patterns)
- User-reported success attribution to the coaching system

### Knowledge Effectiveness
- Relevance ratings for AI-suggested advice and resources
- Adoption rates for AI-recommended strategies
- User growth in personal knowledge base content
- Cross-goal learning and strategy application

This personal goals tracking app demonstrates Django Ergo's capabilities while providing genuine value to users seeking to achieve their personal objectives through AI-powered coaching and knowledge management.