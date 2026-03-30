"""
Test Suite for Thumbs Up/Down Feedback System

Tests the simple voting mechanism for RAG responses with learning capabilities.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from examples.thumbs_feedback_system import SimpleThumbsFeedback, ThumbsVote


class TestSimpleThumbsFeedback:
    """Test the simple thumbs up/down feedback system"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.feedback = SimpleThumbsFeedback()
        
        # Create test response
        self.response_id = self.feedback.create_trackable_response(
            query="How to install Python?",
            llm_response="Download from python.org and run the installer.",
            sources=[
                {'id': 'source1', 'content': 'Python installation guide', 'source': 'python.org'},
                {'id': 'source2', 'content': 'Download instructions', 'source': 'docs.python.org'}
            ],
            user_id="test_user"
        )
    
    def test_create_trackable_response(self):
        """Test creating a trackable response"""
        response_id = self.feedback.create_trackable_response(
            query="What is Django?",
            llm_response="Django is a Python web framework.",
            sources=[{'id': 'django_doc', 'content': 'Django overview'}],
            user_id="user123"
        )
        
        assert response_id is not None
        assert response_id in self.feedback.responses
        
        response = self.feedback.responses[response_id]
        assert response['query'] == "What is Django?"
        assert response['response'] == "Django is a Python web framework."
        assert len(response['sources']) == 1
        assert response['votes']['thumbs_up'] == 0
        assert response['votes']['thumbs_down'] == 0
    
    def test_thumbs_up_vote(self):
        """Test recording a thumbs up vote"""
        stats = self.feedback.thumbs_up(
            self.response_id, 
            "user123", 
            comment="Very helpful!"
        )
        
        assert stats['thumbs_up'] == 1
        assert stats['thumbs_down'] == 0
        assert stats['score'] == 1
        assert stats['approval_rate'] == 1.0
        assert not stats['needs_review']
        
        # Check comment was recorded
        response = self.feedback.responses[self.response_id]
        assert len(response['comments']) == 1
        assert response['comments'][0]['comment'] == "Very helpful!"
        assert response['comments'][0]['vote_type'] == 'thumbs_up'
    
    def test_thumbs_down_vote(self):
        """Test recording a thumbs down vote"""
        stats = self.feedback.thumbs_down(
            self.response_id,
            "user123",
            comment="Not helpful at all"
        )
        
        assert stats['thumbs_up'] == 0
        assert stats['thumbs_down'] == 1
        assert stats['score'] == -1
        assert stats['approval_rate'] == 0.0
        
        # Check comment was recorded
        response = self.feedback.responses[self.response_id]
        assert len(response['comments']) == 1
        assert response['comments'][0]['comment'] == "Not helpful at all"
        assert response['comments'][0]['vote_type'] == 'thumbs_down'
    
    def test_vote_update(self):
        """Test updating an existing vote"""
        # First vote thumbs up
        self.feedback.thumbs_up(self.response_id, "user123")
        stats = self.feedback.get_feedback_stats(self.response_id)
        assert stats['thumbs_up'] == 1
        assert stats['thumbs_down'] == 0
        
        # Change to thumbs down
        self.feedback.thumbs_down(self.response_id, "user123")
        stats = self.feedback.get_feedback_stats(self.response_id)
        assert stats['thumbs_up'] == 0
        assert stats['thumbs_down'] == 1
        
        # Change back to thumbs up
        self.feedback.thumbs_up(self.response_id, "user123")
        stats = self.feedback.get_feedback_stats(self.response_id)
        assert stats['thumbs_up'] == 1
        assert stats['thumbs_down'] == 0
    
    def test_multiple_users_voting(self):
        """Test multiple users voting on same response"""
        # Multiple thumbs up
        self.feedback.thumbs_up(self.response_id, "user1")
        self.feedback.thumbs_up(self.response_id, "user2")
        self.feedback.thumbs_up(self.response_id, "user3")
        
        # One thumbs down
        self.feedback.thumbs_down(self.response_id, "user4", "Needs more detail")
        
        stats = self.feedback.get_feedback_stats(self.response_id)
        assert stats['thumbs_up'] == 3
        assert stats['thumbs_down'] == 1
        assert stats['total_votes'] == 4
        assert stats['score'] == 2
        assert stats['approval_rate'] == 0.75
        assert not stats['needs_review']  # More ups than downs
    
    def test_needs_review_flag(self):
        """Test automatic flagging for review"""
        # Add multiple downvotes
        self.feedback.thumbs_down(self.response_id, "user1", "Wrong information")
        self.feedback.thumbs_down(self.response_id, "user2", "Confusing")
        self.feedback.thumbs_down(self.response_id, "user3", "Incomplete")
        
        # One upvote
        self.feedback.thumbs_up(self.response_id, "user4")
        
        stats = self.feedback.get_feedback_stats(self.response_id)
        assert stats['needs_review'] == True  # More downs than ups with >= 3 votes
        assert stats['approval_rate'] == 0.25
    
    def test_get_user_vote(self):
        """Test retrieving specific user's vote"""
        # No vote initially
        assert self.feedback.get_user_vote(self.response_id, "user123") is None
        
        # After thumbs up
        self.feedback.thumbs_up(self.response_id, "user123")
        assert self.feedback.get_user_vote(self.response_id, "user123") == True
        
        # After changing to thumbs down
        self.feedback.thumbs_down(self.response_id, "user123")
        assert self.feedback.get_user_vote(self.response_id, "user123") == False
    
    def test_invalid_response_id(self):
        """Test error handling for invalid response ID"""
        with pytest.raises(ValueError, match="Response invalid_id not found"):
            self.feedback.thumbs_up("invalid_id", "user123")
        
        with pytest.raises(ValueError, match="Response invalid_id not found"):
            self.feedback.thumbs_down("invalid_id", "user123")
    
    def test_learning_from_positive_feedback(self):
        """Test learning mechanisms from thumbs up votes"""
        with patch.object(self.feedback, '_boost_source_score') as mock_boost:
            with patch.object(self.feedback, '_record_successful_pattern') as mock_pattern:
                self.feedback.thumbs_up(self.response_id, "user123", "Great answer!")
                
                # Should boost all sources
                assert mock_boost.call_count == 2  # Two sources
                mock_boost.assert_any_call('source1', boost_factor=1.1)
                mock_boost.assert_any_call('source2', boost_factor=1.1)
                
                # Should record successful pattern
                mock_pattern.assert_called_once()
        
        # Check learning data was recorded
        assert len(self.feedback.learning_data) == 1
        learning_entry = self.feedback.learning_data[0]
        assert learning_entry['type'] == 'positive_feedback'
        assert learning_entry['comment'] == "Great answer!"
    
    def test_learning_from_negative_feedback(self):
        """Test learning mechanisms from thumbs down votes"""
        with patch.object(self.feedback, '_decrease_source_score') as mock_decrease:
            with patch.object(self.feedback, '_flag_problematic_pattern') as mock_flag:
                self.feedback.thumbs_down(self.response_id, "user123", "Wrong info")
                
                # Should decrease all sources
                assert mock_decrease.call_count == 2
                mock_decrease.assert_any_call('source1', decrease_factor=0.9)
                mock_decrease.assert_any_call('source2', decrease_factor=0.9)
                
                # Should flag problematic pattern
                mock_flag.assert_called_once()
        
        # Check learning data was recorded
        assert len(self.feedback.learning_data) == 1
        learning_entry = self.feedback.learning_data[0]
        assert learning_entry['type'] == 'negative_feedback'
        assert learning_entry['comment'] == "Wrong info"
    
    def test_auto_flag_for_review(self):
        """Test automatic flagging when heavily downvoted"""
        with patch.object(self.feedback, '_flag_for_review') as mock_flag:
            # Add enough downvotes to trigger review
            self.feedback.thumbs_down(self.response_id, "user1")
            mock_flag.assert_not_called()  # Not enough votes yet
            
            self.feedback.thumbs_down(self.response_id, "user2")
            self.feedback.thumbs_down(self.response_id, "user3")
            
            # Now should be flagged (3 downs, 0 ups)
            mock_flag.assert_called_with(self.response_id, reason="Heavy downvotes")
    
    def test_get_learning_insights(self):
        """Test learning insights generation"""
        # Add some feedback
        response2_id = self.feedback.create_trackable_response(
            query="What is AI?",
            llm_response="AI is artificial intelligence.",
            sources=[{'id': 'ai_source', 'content': 'AI definition'}]
        )
        
        # Mixed feedback
        self.feedback.thumbs_up(self.response_id, "user1", "Good")
        self.feedback.thumbs_up(response2_id, "user2", "Helpful")
        self.feedback.thumbs_down(self.response_id, "user3", "Bad")
        
        insights = self.feedback.get_learning_insights()
        
        assert insights['total_feedback'] == 3
        assert insights['positive_count'] == 2
        assert insights['negative_count'] == 1
        assert insights['approval_rate'] == 2/3
        
        assert 'common_positive_patterns' in insights
        assert 'common_negative_patterns' in insights
        assert 'learning_recommendations' in insights
    
    def test_learning_recommendations(self):
        """Test generation of learning recommendations"""
        # Create scenario with poor approval rate
        for i in range(5):
            response_id = self.feedback.create_trackable_response(
                query=f"Question {i}",
                llm_response=f"Answer {i}",
                sources=[]
            )
            self.feedback.thumbs_down(response_id, f"user{i}", "Poor answer")
        
        insights = self.feedback.get_learning_insights()
        recommendations = insights['learning_recommendations']
        
        # Should recommend improving quality due to low approval rate
        approval_rec = any('approval rate' in rec.lower() for rec in recommendations)
        assert approval_rec
        
        # Should note more negative than positive feedback
        negative_rec = any('negative than positive' in rec.lower() for rec in recommendations)
        assert negative_rec
    
    def test_responses_needing_review(self):
        """Test identification of responses needing review"""
        # Create response that needs review
        bad_response_id = self.feedback.create_trackable_response(
            query="Bad question",
            llm_response="Bad answer",
            sources=[]
        )
        
        # Add downvotes to trigger review need
        self.feedback.thumbs_down(bad_response_id, "user1")
        self.feedback.thumbs_down(bad_response_id, "user2")
        self.feedback.thumbs_down(bad_response_id, "user3")
        
        insights = self.feedback.get_learning_insights()
        needing_review = insights['responses_needing_review']
        
        assert len(needing_review) == 1
        assert needing_review[0]['response_id'] == bad_response_id
        assert needing_review[0]['query'] == "Bad question"
        assert needing_review[0]['stats']['needs_review'] == True


class TestThumbsVoteDataClass:
    """Test the ThumbsVote dataclass"""
    
    def test_thumbs_vote_creation(self):
        """Test creating a ThumbsVote object"""
        timestamp = datetime.now()
        vote = ThumbsVote(
            user_id="user123",
            response_id="response456",
            vote=True,
            timestamp=timestamp,
            comment="Great response!"
        )
        
        assert vote.user_id == "user123"
        assert vote.response_id == "response456"
        assert vote.vote == True
        assert vote.timestamp == timestamp
        assert vote.comment == "Great response!"
    
    def test_thumbs_vote_without_comment(self):
        """Test ThumbsVote without optional comment"""
        vote = ThumbsVote(
            user_id="user123",
            response_id="response456",
            vote=False,
            timestamp=datetime.now()
        )
        
        assert vote.comment is None
        assert vote.vote == False


class TestFeedbackIntegration:
    """Integration tests for feedback system"""
    
    def test_full_feedback_cycle(self):
        """Test complete feedback cycle from creation to insights"""
        feedback = SimpleThumbsFeedback()
        
        # Create multiple responses
        responses = []
        for i in range(3):
            response_id = feedback.create_trackable_response(
                query=f"Question {i}",
                llm_response=f"Answer {i}",
                sources=[
                    {'id': f'source_{i}_1', 'content': f'Content {i} part 1'},
                    {'id': f'source_{i}_2', 'content': f'Content {i} part 2'},
                ]
            )
            responses.append(response_id)
        
        # Simulate realistic user feedback
        # Response 0: Very positive
        for j in range(5):
            feedback.thumbs_up(responses[0], f"user_{j}", "Excellent!")
        
        # Response 1: Mixed
        for j in range(2):
            feedback.thumbs_up(responses[1], f"user_{j}")
        for j in range(3):
            feedback.thumbs_down(responses[1], f"user_{j+2}", "Could be better")
        
        # Response 2: Very negative
        for j in range(4):
            feedback.thumbs_down(responses[2], f"user_{j}", "Terrible answer")
        
        # Analyze results
        insights = feedback.get_learning_insights()
        
        assert insights['total_feedback'] == 14  # 5 + 5 + 4 votes
        assert insights['positive_count'] == 7   # 5 + 2 positive
        assert insights['negative_count'] == 7   # 3 + 4 negative
        
        # Check responses needing review
        needing_review = insights['responses_needing_review']
        assert len(needing_review) >= 1  # At least response 2 should need review
        
        # Verify individual response stats
        stats0 = feedback.get_feedback_stats(responses[0])
        assert stats0['approval_rate'] == 1.0
        assert not stats0['needs_review']
        
        stats2 = feedback.get_feedback_stats(responses[2])
        assert stats2['approval_rate'] == 0.0
        assert stats2['needs_review']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])