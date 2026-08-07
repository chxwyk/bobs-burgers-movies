import os
import unittest
from unittest.mock import patch

from movie_bot.config import ConfigError, Settings


class ConfigTests(unittest.TestCase):
    def test_reads_required_and_optional_settings(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "DEV_GUILD_ID": "123456789012345678",
            "DATABASE_PATH": "/tmp/movie.db",
            "TRANSCRIPT_DIR": "/tmp/movie-transcripts",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.token, "test-token")
        self.assertEqual(settings.dev_guild_id, 123456789012345678)
        self.assertEqual(settings.movie_staff_role_id, 1535138119664402443)
        self.assertEqual(settings.customer_notification_role_id, 1515866262306033737)
        self.assertEqual(str(settings.database_path), "/tmp/movie.db")

    def test_role_ids_can_be_overridden(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "MOVIE_STAFF_ROLE_ID": "111",
            "CUSTOMER_NOTIFICATION_ROLE_ID": "222",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.movie_staff_role_id, 111)
        self.assertEqual(settings.customer_notification_role_id, 222)

    def test_requires_token(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(ConfigError),
        ):
            Settings.from_env()

    def test_guild_id_must_be_numeric(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"DISCORD_TOKEN": "test-token", "DEV_GUILD_ID": "not-an-id"},
                clear=True,
            ),
            self.assertRaises(ConfigError),
        ):
            Settings.from_env()


if __name__ == "__main__":
    unittest.main()
