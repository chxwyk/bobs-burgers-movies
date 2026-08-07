import tempfile
import unittest
from pathlib import Path

from movie_bot.database import ActiveOrderExistsError, Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "movie.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_settings_store_status_and_payments(self) -> None:
        self.assertIsNone(await self.db.get_application_state("brand_avatar_sha256"))
        await self.db.set_application_state("brand_avatar_sha256", "avatar-v1")
        self.assertEqual(
            await self.db.get_application_state("brand_avatar_sha256"), "avatar-v1"
        )
        await self.db.set_application_state("brand_avatar_sha256", "avatar-v2")
        self.assertEqual(
            await self.db.get_application_state("brand_avatar_sha256"), "avatar-v2"
        )

        await self.db.upsert_guild_settings(
            guild_id=1,
            brand_name="Oxy Movies",
            ticket_category_id=2,
            staff_role_id=3,
            notification_role_id=4,
            log_channel_id=5,
            banner_url=None,
        )
        settings = await self.db.get_guild_settings(1)
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings.staff_role_id, 3)
        self.assertEqual(settings.notification_role_id, 4)

        self.assertTrue(await self.db.get_store_open(1))
        await self.db.set_store_open(1, False)
        self.assertFalse(await self.db.get_store_open(1))

        await self.db.upsert_payment_method(
            guild_id=1,
            staff_user_id=6,
            name="Cash App",
            instructions="Send to $movie-test",
        )
        methods = await self.db.list_payment_methods(1, 6)
        self.assertEqual([item.name for item in methods], ["Cash App"])
        self.assertTrue(await self.db.remove_payment_method(1, 6, "cash app"))

    async def test_movie_order_lifecycle_and_duplicate_protection(self) -> None:
        order = await self.db.create_order(
            guild_id=1,
            customer_id=10,
            movie_showtime="Superman — 8:30 PM",
            zip_code="89109",
            seats=3,
            snacks="Large popcorn and two drinks",
            submitted_total_cents=8642,
        )
        self.assertEqual(order.customer_price_cents, 4321)
        self.assertEqual(order.status, "creating")

        with self.assertRaises(ActiveOrderExistsError):
            await self.db.create_order(
                guild_id=1,
                customer_id=10,
                movie_showtime="Another Movie — 9:00 PM",
                zip_code="89109",
                seats=2,
                snacks="None",
                submitted_total_cents=5000,
            )

        order = await self.db.attach_channel(order.id, 99)
        order = await self.db.assign_order(order.id, 20)
        order = await self.db.update_order_total(order.id, 10001, actor_id=20)
        self.assertEqual(order.customer_price_cents, 5001)
        order = await self.db.set_order_payment_method(order.id, "Cash App", actor_id=10)
        self.assertEqual(order.payment_method, "Cash App")
        order = await self.db.set_order_status(order.id, "closed", actor_id=20)
        self.assertEqual(order.status, "closed")

        next_order = await self.db.create_order(
            guild_id=1,
            customer_id=10,
            movie_showtime="Another Movie — 9:00 PM",
            zip_code="89109",
            seats=2,
            snacks="None",
            submitted_total_cents=5000,
        )
        self.assertNotEqual(order.id, next_order.id)


if __name__ == "__main__":
    unittest.main()
