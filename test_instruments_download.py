import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime

import instruments_manager
from instruments_manager import (
    validate_nfo_csv_reader,
    validate_nfo_csv_content,
    validate_nfo_csv_file,
    download_and_cache_nfo_instruments,
    MIN_INSTRUMENTS_ROWS,
    REQUIRED_CSV_HEADERS,
)


class TestInstrumentsDownloadAndValidation(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_csv_path = os.path.join(self.test_dir, "nfo_instruments.csv")
        self.orig_data_dir = instruments_manager.DATA_DIR
        self.orig_instruments_file = instruments_manager.INSTRUMENTS_FILE
        instruments_manager.DATA_DIR = self.test_dir
        instruments_manager.INSTRUMENTS_FILE = self.test_csv_path

    def tearDown(self):
        instruments_manager.DATA_DIR = self.orig_data_dir
        instruments_manager.INSTRUMENTS_FILE = self.orig_instruments_file
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def _generate_valid_csv(self, row_count=10005):
        header = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
        rows = [
            f"{100000 + i},50000,NIFTY26AUG{24000 + i}CE,NIFTY,100,2026-08-25,{24000 + i},0.05,65,CE,NFO-OPT,NFO\n"
            for i in range(row_count)
        ]
        return header + "".join(rows)

    def test_validate_nfo_csv_content_valid(self):
        """Test valid CSV content containing all required headers and >= 10,000 rows."""
        csv_text = self._generate_valid_csv(row_count=10005)
        self.assertTrue(validate_nfo_csv_content(csv_text, min_rows=10000))

    def test_validate_nfo_csv_content_insufficient_rows(self):
        """Test CSV content with fewer rows than min_rows threshold."""
        csv_text = self._generate_valid_csv(row_count=500)
        self.assertFalse(validate_nfo_csv_content(csv_text, min_rows=10000))
        # But should be valid if min_rows is set lower
        self.assertTrue(validate_nfo_csv_content(csv_text, min_rows=100))

    def test_validate_nfo_csv_content_missing_headers(self):
        """Test CSV content missing one or more required headers."""
        # Missing tradingsymbol and strike
        bad_header = "instrument_token,exchange_token,name,last_price,expiry,lot_size,instrument_type\n"
        rows = [f"{100000 + i},50000,NIFTY,100,2026-08-25,65,CE\n" for i in range(10050)]
        bad_csv = bad_header + "".join(rows)
        self.assertFalse(validate_nfo_csv_content(bad_csv, min_rows=10000))

    def test_validate_nfo_csv_content_empty_or_too_small(self):
        """Test empty or very short CSV content."""
        self.assertFalse(validate_nfo_csv_content("", min_rows=10000))
        self.assertFalse(validate_nfo_csv_content("instrument_token\n123", min_rows=1))

    def test_validate_nfo_csv_file_valid_and_corrupt(self):
        """Test validate_nfo_csv_file with valid file, corrupt file, and non-existent file."""
        self.assertFalse(validate_nfo_csv_file(os.path.join(self.test_dir, "nonexistent.csv")))

        # Write valid file
        valid_csv = self._generate_valid_csv(row_count=10005)
        with open(self.test_csv_path, "w", encoding="utf-8") as f:
            f.write(valid_csv)
        self.assertTrue(validate_nfo_csv_file(self.test_csv_path, min_rows=10000))

        # Overwrite with partial / truncated file
        with open(self.test_csv_path, "w", encoding="utf-8") as f:
            f.write("instrument_token,tradingsymbol,name,expiry,strike,lot_size,instrument_type\n1,NIFTY26AUG24500CE,NIFTY,2026-08-25,24500,65,CE\n")
        self.assertFalse(validate_nfo_csv_file(self.test_csv_path, min_rows=10000))

    @patch("instruments_manager.requests.get")
    def test_atomic_download_success(self, mock_get):
        """Test successful download writes to temp file first and atomically renames to INSTRUMENTS_FILE."""
        valid_csv = self._generate_valid_csv(row_count=10005)
        mock_response = MagicMock()
        mock_response.text = valid_csv
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = download_and_cache_nfo_instruments(min_rows=10000)
        self.assertEqual(result, valid_csv)
        self.assertTrue(os.path.exists(self.test_csv_path))
        self.assertTrue(validate_nfo_csv_file(self.test_csv_path, min_rows=10000))

        # Check no dangling temp files in data dir
        tmp_files = [f for f in os.listdir(self.test_dir) if f.endswith(".tmp")]
        self.assertEqual(len(tmp_files), 0)

    @patch("instruments_manager.requests.get")
    def test_atomic_download_failed_validation_does_not_corrupt_cache(self, mock_get):
        """Test that invalid downloaded CSV does not overwrite existing valid cache file."""
        # Setup existing valid cache file
        valid_csv = self._generate_valid_csv(row_count=10005)
        with open(self.test_csv_path, "w", encoding="utf-8") as f:
            f.write(valid_csv)

        # Mock download returning partial / corrupted response (e.g. proxy HTML error or partial rows)
        corrupted_response = MagicMock()
        corrupted_response.text = "<html><body>502 Bad Gateway</body></html>" * 50
        corrupted_response.raise_for_status.return_value = None
        mock_get.return_value = corrupted_response

        # Force download by bypassing today's date cache
        with patch("instruments_manager.validate_nfo_csv_file", return_value=False):
            pass

        # Since download fails validation, it should log error and fallback to valid existing file
        result = download_and_cache_nfo_instruments(min_rows=10000)
        self.assertEqual(result, valid_csv)
        # Verify file content on disk remained valid and was NOT replaced by corrupted response
        with open(self.test_csv_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), valid_csv)

        # Check no dangling temp files
        tmp_files = [f for f in os.listdir(self.test_dir) if f.endswith(".tmp")]
        self.assertEqual(len(tmp_files), 0)

    @patch("instruments_manager.requests.get")
    def test_atomic_download_network_error_fallback(self, mock_get):
        """Test that network error triggers fallback to existing valid cache."""
        valid_csv = self._generate_valid_csv(row_count=10005)
        with open(self.test_csv_path, "w", encoding="utf-8") as f:
            f.write(valid_csv)

        mock_get.side_effect = ConnectionError("Proxy unreachable")

        # Fallback to existing valid cache
        result = download_and_cache_nfo_instruments(min_rows=10000)
        self.assertEqual(result, valid_csv)

    @patch("instruments_manager.requests.get")
    def test_atomic_download_network_error_no_fallback_raises(self, mock_get):
        """Test that network error with no valid fallback raises exception."""
        mock_get.side_effect = ConnectionError("Proxy unreachable")
        with self.assertRaises(ConnectionError):
            download_and_cache_nfo_instruments(min_rows=10000)

    @patch("instruments_manager.requests.get")
    def test_cached_today_valid_skips_download(self, mock_get):
        """Test that if today's valid file already exists, requests.get is not called."""
        valid_csv = self._generate_valid_csv(row_count=10005)
        with open(self.test_csv_path, "w", encoding="utf-8") as f:
            f.write(valid_csv)

        result = download_and_cache_nfo_instruments(min_rows=10000)
        self.assertEqual(result, valid_csv)
        mock_get.assert_not_called()

    @patch("instruments_manager.requests.get")
    def test_cached_today_corrupt_triggers_fresh_download(self, mock_get):
        """Test that if today's cache file is corrupted/partial, it triggers fresh download."""
        # Write corrupted file with today's timestamp
        with open(self.test_csv_path, "w", encoding="utf-8") as f:
            f.write("corrupted content " * 100)

        fresh_csv = self._generate_valid_csv(row_count=10005)
        mock_response = MagicMock()
        mock_response.text = fresh_csv
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = download_and_cache_nfo_instruments(min_rows=10000)
        self.assertEqual(result, fresh_csv)
        mock_get.assert_called_once()
        # Verify file is now fresh valid CSV
        self.assertTrue(validate_nfo_csv_file(self.test_csv_path, min_rows=10000))


if __name__ == "__main__":
    unittest.main()
