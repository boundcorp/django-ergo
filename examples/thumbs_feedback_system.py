"""
Simple Thumbs Up/Down Feedback System for RAG Responses

This system provides a straightforward way to collect human feedback
using thumbs up/thumbs down voting with optional comments.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model

User = get_user_model()


@dataclass
class ThumbsVote:
    """Simple thumbs up/down vote"""

    user_id: str
    response_id: str
    vote: bool  # True = thumbs up, False = thumbs down
    timestamp: datetime
    comment: str | None = None


class SimpleThumbsFeedback:
    """
    Simple feedback system using thumbs up/down voting.

    Example usage:
        >>> feedback = SimpleThumbsFeedback()
        >>> response_id = feedback.create_trackable_response(
        ...     query="How to install?",
        ...     llm_response="To install, run pip install...",
        ...     sources=[chunk1, chunk2]
        ... )
        >>>
        >>> # User gives thumbs up
        >>> feedback.thumbs_up(response_id, user_id="user123",
        ...                    comment="Very helpful!")
        >>>
        >>> # Check feedback
        >>> stats = feedback.get_feedback_stats(response_id)
        >>> print(f"Score: {stats['score']}")  # Score: +1
    """

    def __init__(self):
        self.responses = {}  # In production, use database
        self.votes = {}
        self.learning_data = []

    def create_trackable_response(
        self,
        query: str,
        llm_response: str,
        sources: list[dict],
        user_id: str | None = None,
    ) -> str:
        """
        Create a response that can be voted on.

        Args:
            query: Original user query
            llm_response: LLM generated response
            sources: List of source chunks used
            user_id: Optional user who made the query

        Returns:
            response_id: Unique ID for tracking votes
        """
        response_id = str(uuid.uuid4())

        self.responses[response_id] = {
            "id": response_id,
            "query": query,
            "response": llm_response,
            "sources": sources,
            "user_id": user_id,
            "created_at": datetime.now(),
            "votes": {"thumbs_up": 0, "thumbs_down": 0},
            "comments": [],
        }

        return response_id

    def thumbs_up(
        self, response_id: str, user_id: str, comment: str | None = None
    ) -> dict[str, Any]:
        """
        Record a thumbs up vote.

        Args:
            response_id: ID of response being voted on
            user_id: ID of user voting
            comment: Optional comment explaining the vote

        Returns:
            Updated vote statistics
        """
        if response_id not in self.responses:
            raise ValueError(f"Response {response_id} not found")

        # Record the vote
        vote = ThumbsVote(
            user_id=user_id,
            response_id=response_id,
            vote=True,  # thumbs up
            timestamp=datetime.now(),
            comment=comment,
        )

        # Store vote
        vote_key = f"{response_id}:{user_id}"
        if vote_key in self.votes:
            # User already voted, update it
            old_vote = self.votes[vote_key]
            if not old_vote.vote:  # Was thumbs down, now thumbs up
                self.responses[response_id]["votes"]["thumbs_down"] -= 1
                self.responses[response_id]["votes"]["thumbs_up"] += 1
        else:
            # New vote
            self.responses[response_id]["votes"]["thumbs_up"] += 1

        self.votes[vote_key] = vote

        # Add comment if provided
        if comment:
            self.responses[response_id]["comments"].append(
                {
                    "user_id": user_id,
                    "comment": comment,
                    "timestamp": datetime.now(),
                    "vote_type": "thumbs_up",
                }
            )

        # Learn from positive feedback
        self._learn_from_positive_feedback(response_id, vote)

        return self.get_feedback_stats(response_id)

    def thumbs_down(
        self, response_id: str, user_id: str, comment: str | None = None
    ) -> dict[str, Any]:
        """
        Record a thumbs down vote.

        Args:
            response_id: ID of response being voted on
            user_id: ID of user voting
            comment: Optional comment explaining the vote

        Returns:
            Updated vote statistics
        """
        if response_id not in self.responses:
            raise ValueError(f"Response {response_id} not found")

        # Record the vote
        vote = ThumbsVote(
            user_id=user_id,
            response_id=response_id,
            vote=False,  # thumbs down
            timestamp=datetime.now(),
            comment=comment,
        )

        # Store vote
        vote_key = f"{response_id}:{user_id}"
        if vote_key in self.votes:
            # User already voted, update it
            old_vote = self.votes[vote_key]
            if old_vote.vote:  # Was thumbs up, now thumbs down
                self.responses[response_id]["votes"]["thumbs_up"] -= 1
                self.responses[response_id]["votes"]["thumbs_down"] += 1
        else:
            # New vote
            self.responses[response_id]["votes"]["thumbs_down"] += 1

        self.votes[vote_key] = vote

        # Add comment if provided
        if comment:
            self.responses[response_id]["comments"].append(
                {
                    "user_id": user_id,
                    "comment": comment,
                    "timestamp": datetime.now(),
                    "vote_type": "thumbs_down",
                }
            )

        # Learn from negative feedback
        self._learn_from_negative_feedback(response_id, vote)

        return self.get_feedback_stats(response_id)

    def get_feedback_stats(self, response_id: str) -> dict[str, Any]:
        """
        Get voting statistics for a response.

        Returns:
            Dictionary with vote counts, score, and comments
        """
        if response_id not in self.responses:
            return {}

        response = self.responses[response_id]
        thumbs_up = response["votes"]["thumbs_up"]
        thumbs_down = response["votes"]["thumbs_down"]
        total_votes = thumbs_up + thumbs_down

        return {
            "response_id": response_id,
            "thumbs_up": thumbs_up,
            "thumbs_down": thumbs_down,
            "total_votes": total_votes,
            "score": thumbs_up - thumbs_down,
            "approval_rate": thumbs_up / total_votes if total_votes > 0 else 0,
            "comments": response["comments"],
            "needs_review": thumbs_down > thumbs_up and total_votes >= 3,
        }

    def get_user_vote(self, response_id: str, user_id: str) -> bool | None:
        """
        Get a specific user's vote on a response.

        Returns:
            True for thumbs up, False for thumbs down, None if no vote
        """
        vote_key = f"{response_id}:{user_id}"
        if vote_key in self.votes:
            return self.votes[vote_key].vote
        return None

    def _learn_from_positive_feedback(self, response_id: str, vote: ThumbsVote):
        """Learn from thumbs up votes"""
        response = self.responses[response_id]

        # Boost relevance of sources used in upvoted responses
        for source in response["sources"]:
            self._boost_source_score(source["id"], boost_factor=1.1)

        # Learn successful query patterns
        self._record_successful_pattern(response["query"], response["sources"])

        # Store learning data
        self.learning_data.append(
            {
                "type": "positive_feedback",
                "response_id": response_id,
                "query": response["query"],
                "sources": response["sources"],
                "comment": vote.comment,
                "timestamp": vote.timestamp,
            }
        )

    def _learn_from_negative_feedback(self, response_id: str, vote: ThumbsVote):
        """Learn from thumbs down votes"""
        response = self.responses[response_id]

        # Decrease relevance of sources used in downvoted responses
        for source in response["sources"]:
            self._decrease_source_score(source["id"], decrease_factor=0.9)

        # Flag problematic patterns
        self._flag_problematic_pattern(response["query"], response["sources"])

        # Store learning data
        self.learning_data.append(
            {
                "type": "negative_feedback",
                "response_id": response_id,
                "query": response["query"],
                "sources": response["sources"],
                "comment": vote.comment,
                "timestamp": vote.timestamp,
            }
        )

        # Auto-flag for review if heavily downvoted
        stats = self.get_feedback_stats(response_id)
        if stats["needs_review"]:
            self._flag_for_review(response_id, reason="Heavy downvotes")

    def _boost_source_score(self, source_id: str, boost_factor: float):
        """Boost the relevance score of a source"""
        # In production, update source relevance in database
        print(f"Boosting source {source_id} by factor {boost_factor}")

    def _decrease_source_score(self, source_id: str, decrease_factor: float):
        """Decrease the relevance score of a source"""
        # In production, update source relevance in database
        print(f"Decreasing source {source_id} by factor {decrease_factor}")

    def _record_successful_pattern(self, query: str, sources: list[dict]):
        """Record patterns that lead to successful responses"""
        print(f"Recording successful pattern for query: {query}")

    def _flag_problematic_pattern(self, query: str, sources: list[dict]):
        """Flag patterns that lead to poor responses"""
        print(f"Flagging problematic pattern for query: {query}")

    def _flag_for_review(self, response_id: str, reason: str):
        """Flag response for expert review"""
        print(f"Flagging response {response_id} for review: {reason}")

    def get_learning_insights(self, time_period: str | None = None) -> dict[str, Any]:
        """
        Get insights from accumulated voting data.

        Returns:
            Analytics on voting patterns and learning opportunities
        """
        # Filter by time period if specified
        data = self.learning_data
        if time_period:
            # In production, filter by timestamp
            pass

        positive_feedback = [d for d in data if d["type"] == "positive_feedback"]
        negative_feedback = [d for d in data if d["type"] == "negative_feedback"]

        return {
            "total_feedback": len(data),
            "positive_count": len(positive_feedback),
            "negative_count": len(negative_feedback),
            "approval_rate": len(positive_feedback) / len(data) if data else 0,
            "common_positive_patterns": self._extract_common_patterns(
                positive_feedback
            ),
            "common_negative_patterns": self._extract_common_patterns(
                negative_feedback
            ),
            "responses_needing_review": self._get_responses_needing_review(),
            "learning_recommendations": self._generate_learning_recommendations(),
        }

    def _extract_common_patterns(self, feedback_data: list[dict]) -> list[str]:
        """Extract common patterns from feedback data"""
        # Simplified pattern extraction
        queries = [d["query"] for d in feedback_data]
        # In production, use more sophisticated pattern analysis
        return list(set(queries))[:5]  # Top 5 unique queries

    def _get_responses_needing_review(self) -> list[dict]:
        """Get responses that need expert review"""
        needing_review = []
        for response_id, response in self.responses.items():
            stats = self.get_feedback_stats(response_id)
            if stats.get("needs_review", False):
                needing_review.append(
                    {
                        "response_id": response_id,
                        "query": response["query"],
                        "stats": stats,
                    }
                )
        return needing_review

    def _generate_learning_recommendations(self) -> list[str]:
        """Generate recommendations for improving the system"""
        recommendations = []

        # Calculate stats directly to avoid recursion
        positive_count = len(
            [d for d in self.learning_data if d["type"] == "positive_feedback"]
        )
        negative_count = len(
            [d for d in self.learning_data if d["type"] == "negative_feedback"]
        )
        total_feedback = len(self.learning_data)
        approval_rate = positive_count / total_feedback if total_feedback > 0 else 0

        responses_needing_review = self._get_responses_needing_review()

        if approval_rate < 0.7:
            recommendations.append(
                "Consider improving response quality - approval rate is below 70%"
            )

        if len(responses_needing_review) > 0:
            recommendations.append(
                f"{len(responses_needing_review)} responses need expert review"
            )

        if negative_count > positive_count:
            recommendations.append(
                "More negative than positive feedback - investigate common issues"
            )

        return recommendations


# =============================================================================
# USAGE EXAMPLE
# =============================================================================


def demonstrate_thumbs_feedback():
    """
    Demonstrate the simple thumbs up/down feedback system.
    """
    # Initialize feedback system
    feedback = SimpleThumbsFeedback()

    # Create some sample responses
    response1_id = feedback.create_trackable_response(
        query="How do I install Python?",
        llm_response="To install Python, download it from python.org and run the installer.",
        sources=[
            {
                "id": "chunk_1",
                "content": "Python installation guide...",
                "source": "python.org",
            },
            {
                "id": "chunk_2",
                "content": "Download Python installer...",
                "source": "docs.python.org",
            },
        ],
        user_id="user123",
    )

    response2_id = feedback.create_trackable_response(
        query="What is machine learning?",
        llm_response="Machine learning is a subset of AI that uses algorithms to learn patterns.",
        sources=[
            {
                "id": "chunk_3",
                "content": "ML definition...",
                "source": "ml_textbook.pdf",
            },
        ],
        user_id="user456",
    )

    # Simulate user feedback
    print("=== Simulating User Feedback ===")

    # Users give thumbs up to first response
    feedback.thumbs_up(response1_id, "user123", "Very clear instructions!")
    feedback.thumbs_up(response1_id, "user789", "Worked perfectly")

    # Mixed feedback on second response
    feedback.thumbs_up(response2_id, "user456", "Good basic explanation")
    feedback.thumbs_down(response2_id, "user101", "Too basic, needs more detail")
    feedback.thumbs_down(response2_id, "user102", "Missing key concepts")

    # Check feedback stats
    print("\n=== Feedback Statistics ===")
    stats1 = feedback.get_feedback_stats(response1_id)
    print(
        f"Response 1 - Score: {stats1['score']}, Approval: {stats1['approval_rate']:.1%}"
    )

    stats2 = feedback.get_feedback_stats(response2_id)
    print(
        f"Response 2 - Score: {stats2['score']}, Approval: {stats2['approval_rate']:.1%}"
    )
    print(f"Needs review: {stats2['needs_review']}")

    # Get learning insights
    print("\n=== Learning Insights ===")
    insights = feedback.get_learning_insights()
    print(f"Total feedback: {insights['total_feedback']}")
    print(f"Overall approval rate: {insights['approval_rate']:.1%}")
    print(f"Responses needing review: {len(insights['responses_needing_review'])}")

    print("\nRecommendations:")
    for rec in insights["learning_recommendations"]:
        print(f"- {rec}")

    return feedback


if __name__ == "__main__":
    # Run demonstration
    feedback_system = demonstrate_thumbs_feedback()
    print("\nThumbς feedback system demonstration complete!")
