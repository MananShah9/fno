import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from telethon import TelegramClient
from telethon.sessions import StringSession, SQLiteSession
from telethon.crypto import AuthKey
import telegram_client
import config


class TestTelegramSession(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.test_api_id = "123456"
        self.test_api_hash = "abcdef0123456789abcdef0123456789"
        self.test_phone = "+919876543210"
        # Generate a valid sample StringSession
        mem_session = StringSession()
        mem_session.set_dc(2, "149.154.167.50", 443)
        mem_session.auth_key = AuthKey(bytes(range(256)))
        self.valid_session_str = StringSession.save(mem_session)

    def test_get_session_string_from_env_string_session(self):
        """Test get_session_string reads TELEGRAM_STRING_SESSION from environment."""
        with patch.dict(os.environ, {"TELEGRAM_STRING_SESSION": self.valid_session_str}):
            sess_str = telegram_client.get_session_string()
            self.assertEqual(sess_str, self.valid_session_str)

    def test_get_session_string_from_env_session_string_fallback(self):
        """Test get_session_string reads TELEGRAM_SESSION_STRING from environment as fallback."""
        with patch.dict(os.environ, {"TELEGRAM_STRING_SESSION": "", "TELEGRAM_SESSION_STRING": self.valid_session_str}):
            sess_str = telegram_client.get_session_string()
            self.assertEqual(sess_str, self.valid_session_str)

    def test_get_session_string_empty_when_no_env_and_no_legacy(self):
        """Test get_session_string returns empty string when neither env var nor legacy file exists."""
        with patch.dict(os.environ, {"TELEGRAM_STRING_SESSION": "", "TELEGRAM_SESSION_STRING": ""}):
            with patch("os.path.exists", return_value=False):
                sess_str = telegram_client.get_session_string()
                self.assertEqual(sess_str, "")

    def test_get_session_string_migrates_legacy_sqlite_session(self):
        """Test get_session_string automatically migrates legacy SQLite session file to StringSession."""
        legacy_dir = "sessions"
        legacy_path = os.path.join(legacy_dir, "telegram_user")
        legacy_file = legacy_path + ".session"

        os.makedirs(legacy_dir, exist_ok=True)
        # Create a mock SQLite session file
        sql_session = SQLiteSession(legacy_path)
        sql_session.set_dc(2, "149.154.167.50", 443)
        sql_session.auth_key = AuthKey(bytes(range(256)))
        expected_str = StringSession.save(sql_session)
        sql_session.close()

        try:
            with patch.dict(os.environ, {"TELEGRAM_STRING_SESSION": "", "TELEGRAM_SESSION_STRING": ""}):
                with patch("config.update_env_variable") as mock_update_env:
                    migrated = telegram_client.get_session_string()
                    self.assertEqual(migrated, expected_str)
                    mock_update_env.assert_called_once_with("TELEGRAM_STRING_SESSION", expected_str)
        finally:
            if os.path.exists(legacy_file):
                os.remove(legacy_file)

    def test_create_telegram_client_uses_string_session(self):
        """Test create_telegram_client creates client with StringSession without creating SQLite file."""
        with patch.dict(os.environ, {
            "TELEGRAM_API_ID": self.test_api_id,
            "TELEGRAM_API_HASH": self.test_api_hash,
            "TELEGRAM_STRING_SESSION": self.valid_session_str
        }):
            client = telegram_client.create_telegram_client()
            self.assertIsNotNone(client)
            self.assertIsInstance(client.session, StringSession)
            self.assertEqual(client.api_id, int(self.test_api_id))
            self.assertEqual(client.api_hash, self.test_api_hash)
            # Ensure no SQLite file was created in sessions/
            self.assertFalse(os.path.exists("sessions/telegram_user.session"))

    def test_create_telegram_client_missing_credentials(self):
        """Test create_telegram_client returns None if credentials missing or invalid."""
        with patch.dict(os.environ, {"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": ""}):
            client = telegram_client.create_telegram_client()
            self.assertIsNone(client)

        with patch.dict(os.environ, {"TELEGRAM_API_ID": "not_an_int", "TELEGRAM_API_HASH": "abc"}):
            client = telegram_client.create_telegram_client()
            self.assertIsNone(client)

    def test_concurrent_string_sessions_do_not_lock(self):
        """Test multiple StringSession TelegramClient instances can coexist without SQLite locking errors."""
        with patch.dict(os.environ, {
            "TELEGRAM_API_ID": self.test_api_id,
            "TELEGRAM_API_HASH": self.test_api_hash,
            "TELEGRAM_STRING_SESSION": self.valid_session_str
        }):
            # Simulate worker daemon client
            worker_client = telegram_client.create_telegram_client()
            # Simulate CLI dashboard client
            cli_client = telegram_client.create_telegram_client()
            # Simulate simulator client
            sim_client = telegram_client.create_telegram_client()

            self.assertIsInstance(worker_client.session, StringSession)
            self.assertIsInstance(cli_client.session, StringSession)
            self.assertIsInstance(sim_client.session, StringSession)

            # Check that sessions are distinct in-memory objects
            self.assertIsNot(worker_client.session, cli_client.session)
            self.assertIsNot(cli_client.session, sim_client.session)

            # No SQLite session lock files created
            self.assertFalse(os.path.exists("sessions/telegram_user.session"))

    async def test_check_login_authorized(self):
        """Test check_login returns True when authorized."""
        mock_cli = MagicMock()
        mock_cli.is_connected.return_value = True
        mock_cli.is_user_authorized = AsyncMock(return_value=True)

        result = await telegram_client.check_login(target_client=mock_cli)
        self.assertTrue(result)
        mock_cli.is_user_authorized.assert_awaited_once()

    async def test_check_login_unauthorized(self):
        """Test check_login returns False when unauthorized."""
        mock_cli = MagicMock()
        mock_cli.is_connected.return_value = False
        mock_cli.connect = AsyncMock()
        mock_cli.is_user_authorized = AsyncMock(return_value=False)

        result = await telegram_client.check_login(target_client=mock_cli)
        self.assertFalse(result)
        mock_cli.connect.assert_awaited_once()
        mock_cli.is_user_authorized.assert_awaited_once()

    async def test_interactive_login_persists_session_string(self):
        """Test interactive_login persists StringSession to .env upon successful login."""
        mock_cli = MagicMock()
        mock_cli.is_connected.return_value = False
        mock_cli.connect = AsyncMock()
        mock_cli.is_user_authorized = AsyncMock(return_value=False)
        mock_cli.start = AsyncMock()
        
        mock_me = MagicMock()
        mock_me.first_name = "Trader"
        mock_me.username = "trader_user"
        mock_cli.get_me = AsyncMock(return_value=mock_me)

        mock_cli.session = MagicMock()
        mock_cli.session.save.return_value = self.valid_session_str

        with patch.dict(os.environ, {"TELEGRAM_PHONE": self.test_phone}):
            with patch("config.update_env_variable") as mock_update_env:
                success = await telegram_client.interactive_login(target_client=mock_cli)
                self.assertTrue(success)
                mock_cli.start.assert_awaited_once_with(phone=self.test_phone)
                mock_update_env.assert_called_once_with("TELEGRAM_STRING_SESSION", self.valid_session_str)

    async def test_get_channel_entity_resolves_id_and_username(self):
        """Test get_channel_entity resolves both integer channel IDs and string usernames."""
        mock_cli = MagicMock()
        mock_cli.is_connected.return_value = True
        mock_entity = MagicMock()
        mock_cli.get_entity = AsyncMock(return_value=mock_entity)

        # Test integer ID
        res = await telegram_client.get_channel_entity("-100123456789", target_client=mock_cli)
        self.assertEqual(res, mock_entity)
        mock_cli.get_entity.assert_awaited_with(-100123456789)

        # Test username
        res2 = await telegram_client.get_channel_entity("@my_trading_channel", target_client=mock_cli)
        self.assertEqual(res2, mock_entity)
        mock_cli.get_entity.assert_awaited_with("@my_trading_channel")


if __name__ == "__main__":
    unittest.main()
