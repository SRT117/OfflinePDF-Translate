import unittest
from unittest.mock import MagicMock, patch

from core import i18n


class TestI18nSystemDetection(unittest.TestCase):
    def test_detect_system_language_maps_supported_locales(self):
        cases = [
            (["zh-CN"], "zh_CN", "zh"),
            (["zh-Hant-TW"], "zh_TW", "zh"),
            (["ja-JP"], "ja_JP", "ja"),
            (["ko-KR"], "ko_KR", "ko"),
            (["ru-RU"], "ru_RU", "ru"),
            (["en-US"], "en_US", "en"),
            (["fr-FR"], "fr_FR", "en"),
            ([], "de_DE", "en"),
        ]
        for ui_langs, locale_name, expected in cases:
            with self.subTest(ui_langs=ui_langs, locale_name=locale_name):
                mock_locale = MagicMock()
                mock_locale.uiLanguages.return_value = ui_langs
                mock_locale.name.return_value = locale_name
                with patch.object(i18n.QLocale, "system", return_value=mock_locale):
                    self.assertEqual(i18n.detect_system_language(), expected)

    def test_saved_language_prefers_user_setting_else_system(self):
        mock_settings = MagicMock()
        mock_settings.value.return_value = "ja"
        with patch.object(i18n, "QSettings", return_value=mock_settings):
            self.assertEqual(i18n.saved_language(), "ja")

        mock_settings.value.return_value = None
        mock_locale = MagicMock()
        mock_locale.uiLanguages.return_value = ["en-GB"]
        mock_locale.name.return_value = "en_GB"
        with patch.object(i18n, "QSettings", return_value=mock_settings), patch.object(
            i18n.QLocale, "system", return_value=mock_locale
        ):
            self.assertEqual(i18n.saved_language(), "en")

    def test_saved_language_migrates_legacy_qsettings_before_system_fallback(self):
        stores = {
            ("OfflinePDF", "Translate"): {},
            ("PDF2zh", "Local"): {"interface_language": "ja"},
            ("LitMTrans", "Local"): {"interface_language": "ko"},
        }

        class FakeSettings:
            def __init__(self, org, app):
                self._data = stores[(org, app)]

            def value(self, key, default=None):
                return self._data.get(key, default)

            def setValue(self, key, val):
                self._data[key] = val

        mock_locale = MagicMock()
        mock_locale.uiLanguages.return_value = ["en-US"]
        mock_locale.name.return_value = "en_US"
        with patch.object(i18n, "QSettings", side_effect=FakeSettings), patch.object(
            i18n.QLocale, "system", return_value=mock_locale
        ):
            # 1. Reads legacy PDF2zh/Local instead of overwriting with system language
            self.assertEqual(i18n.saved_language(), "ja")
            # 2. set_language promotes choice into new OfflinePDF/Translate settings
            i18n.set_language(i18n.saved_language())
            self.assertEqual(stores[("OfflinePDF", "Translate")]["interface_language"], "ja")
            # 3. New setting takes precedence over legacy once present
            stores[("OfflinePDF", "Translate")]["interface_language"] = "ru"
            self.assertEqual(i18n.saved_language(), "ru")
            # 4. Falls back to LitMTrans/Local if only that legacy setting exists
            stores[("OfflinePDF", "Translate")].clear()
            stores[("PDF2zh", "Local")].clear()
            self.assertEqual(i18n.saved_language(), "ko")
            # 5. Falls back to system language when all settings are empty
            stores[("LitMTrans", "Local")].clear()
            self.assertEqual(i18n.saved_language(), "en")


if __name__ == "__main__":
    unittest.main()
