import unittest
from datetime import UTC, datetime

from movie_bot.models import MovieOrder
from movie_bot.transcripts import (
    TranscriptAttachment,
    TranscriptMessage,
    render_transcript_html,
)


class TranscriptTests(unittest.TestCase):
    def test_escapes_content_and_includes_movie_totals(self) -> None:
        order = MovieOrder(
            id=1,
            guild_id=2,
            customer_id=3,
            channel_id=4,
            movie_showtime="Movie <Night> — 8:30 PM",
            zip_code="89109",
            seats=2,
            snacks="None",
            submitted_total_cents=8642,
            customer_price_cents=4321,
            discount_basis_points=5000,
            assigned_staff_id=5,
            payment_method="Cash App",
            status="tickets_sent",
            created_at="now",
            updated_at="now",
        )
        message = TranscriptMessage(
            author_name="User <One>",
            author_id=3,
            avatar_url=None,
            created_at=datetime.now(UTC),
            content="<script>alert('x')</script>",
            attachments=[
                TranscriptAttachment(filename="ticket.png", url="https://example.com/t.png")
            ],
        )
        result = render_transcript_html(
            guild_name="Oxy Movies",
            channel_name="movie-0001-user",
            order=order,
            messages=[message],
        )
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)
        self.assertIn("$86.42", result)
        self.assertIn("$43.21", result)
        self.assertIn("Movie &lt;Night&gt;", result)


if __name__ == "__main__":
    unittest.main()
