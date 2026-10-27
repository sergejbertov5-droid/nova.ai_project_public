"""GUI: раздел «AI / API KEYS» панели настроек.

Проверяется без сети и без записи в настоящий .env:
read_env подменён фикстурой, write_env/delete_keys записывают вызовы.
Test Connection заменяет AIBrain фейковым runtime (регистрирует вызов),
который отвечает без обращения к провайдерам.
"""
import os
import time
import tkinter as tk
import unittest
from unittest import mock

import control_settings as cs
import ui_kit as ui
from core.ai_providers import PROVIDERS, PROVIDER_ORDER

# GEMINI_API_KEY нет намеренно — проверяем состояние «Not configured».
FIXTURE = {
    "AI_PROVIDER": "auto",
    "GROQ_API_KEY": "gsk_ui_fixture_do_not_use",
}

KEYED_PIDS = [pid for pid in PROVIDER_ORDER if PROVIDERS[pid].get("key_env")]


class _Resp:
    def __init__(self, ok=True, status_code=200, text="{}"):
        self.ok = ok
        self.status_code = status_code
        self.text = text


class _FakeRuntime:
    """Замена AIBrain: фиксирует вызов _post_provider, сеть не трогает."""
    instances = []

    def __init__(self):
        self.posted = []
        type(self).instances.append(self)

    def _post_provider(self, provider, messages, max_tokens=None,
                       temperature=None, json_mode=None):
        self.posted.append({
            "provider": provider,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "json_mode": json_mode,
        })
        return _Resp(ok=True, status_code=200, text='{"choices":[]}')


class SettingsAIKeysTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.write_calls = []
        cls.delete_calls = []
        cls._patchers = [
            mock.patch.object(cs, "read_env", return_value=dict(FIXTURE)),
            mock.patch.object(cs, "has_password", return_value=False),
            mock.patch.object(cs, "has_password_broken", return_value=False),
            mock.patch.object(
                cs, "write_env",
                side_effect=lambda updates, path=None:
                cls.write_calls.append(dict(updates))),
            mock.patch.object(
                cs, "delete_keys",
                side_effect=lambda keys, **kw:
                cls.delete_calls.append(list(keys)) or True),
        ]
        for patcher in cls._patchers:
            patcher.start()
        cls.win = cs.SettingsWindow(cls.root)
        cls.win.win.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.win.win.destroy()
        except Exception:
            pass
        try:
            cls.root.destroy()
        except Exception:
            pass
        for patcher in cls._patchers:
            patcher.stop()

    # --- helpers ------------------------------------------------
    def setUp(self):
        """Каждый тест начинает с «свежего» состояния карточек: порядок
        тестов не влияет на результат."""
        self.win.env = dict(FIXTURE)
        for row in self.win.ai_provider_rows.values():
            row["connected"] = False
            row["error"] = False
            row["test_result"] = None
            row["test_running"] = False
            row["shown"] = False
            row["var"].set(FIXTURE.get(row["key"], ""))
            row["entry"].configure(show="\u2022")
            row["test_btn"].configure(state="normal")
            row["show_btn"].configure(text="Показать")
            row["test_lbl"].configure(text="", fg=ui.DIM)
            self.win._refresh_provider_card(row)
        self.write_calls.clear()
        self.delete_calls.clear()

    def _drive(self, row, needle, timeout=6.0):
        """Ждёт результат фонового теста и прогоняет его через _poll_test —
        эту же функцию вызывает таймер в реальном UI.

        ВАЖНО: нельзя качать окно через win.update() — ScrollArea крутит
        свой after(33)-тик, и Tk.update() на macOS не возвращается.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if row.get("test_result") is not None:
                break
            time.sleep(0.03)
        if row.get("test_result") is None:
            self.fail("Фоновый тест не вернул результат за {:.0f}с".format(timeout))
        self.win._poll_test(row)
        text = row["test_lbl"].cget("text")
        self.assertIn(needle, text, f"не дождались {needle!r}: {text!r}")

    # --- раздел существует --------------------------------------
    def test_section_cards_for_all_keyed_registry_providers(self):
        for pid in KEYED_PIDS:
            self.assertIn(pid, self.win.ai_provider_rows,
                          f"нет карточки провайдера {pid}")
        self.assertNotIn("ollama", self.win.ai_provider_rows)
        self.assertTrue(hasattr(self.win, "ollama_status"),
                        "карточка Ollama должна остаться")

    def test_ai_tab_renamed_to_api_keys(self):
        # Под-вкладка «Доступы и данные» содержит «AI / API KEYS».
        names = self._sub_tab_labels()
        self.assertTrue(any("AI / API KEYS" in n for n in names))

    def _sub_tab_labels(self):
        sub = self.win._access_sub
        out = []
        for child in sub.tabs():
            label = sub.tab(child, "text") or ""
            out.append(label)
        return out

    def test_selector_offers_auto_registry_and_custom(self):
        values = list(self.win._provider_combo.cget("values"))
        self.assertEqual(values, ["auto", *list(PROVIDER_ORDER), "custom"])
        self.assertEqual(self.win.provider_var.get(), "auto")

    # --- маскирование -------------------------------------------
    def test_key_entry_masked_by_default(self):
        for pid in KEYED_PIDS:
            entry = self.win.ai_provider_rows[pid]["entry"]
            self.assertEqual(entry.cget("show"), "\u2022",
                             f"{pid}: ключ не в маске")

    def test_mask_toggle_shows_then_hides(self):
        row = self.win.ai_provider_rows["groq"]
        self.assertEqual(row["entry"].cget("show"), "\u2022")
        row["show_btn"].invoke()
        self.assertEqual(row["entry"].cget("show"), "")
        row["show_btn"].invoke()
        self.assertEqual(row["entry"].cget("show"), "\u2022")

    def test_existing_secret_not_in_a_plain_label(self):
        # Значение есть в .env, но ни один Label его не показывает.
        for pid in KEYED_PIDS:
            row = self.win.ai_provider_rows[pid]
            secret = self.win.env.get(row["key"], "")
            if not secret:
                continue  # ключа нет — нечего проверять
            for lbl in (row["status_lbl"], row["model_lbl"],
                        row["test_lbl"], self.win.status):
                self.assertNotIn(secret, lbl.cget("text"))

    # --- статусы -------------------------------------------------
    def test_groq_configured_gemini_not_configured(self):
        self.assertIn("Configured",
                      self.win.ai_provider_rows["groq"]["status_lbl"].cget("text"))
        self.assertIn("Not configured",
                      self.win.ai_provider_rows["gemini"]["status_lbl"].cget("text"))

    def test_model_from_registry(self):
        lbl = self.win.ai_provider_rows["gemini"]["model_lbl"].cget("text")
        self.assertIn(PROVIDERS["gemini"]["model"], lbl)

    # --- сохранение ---------------------------------------------
    def test_save_calls_config_writer_and_status_has_no_secret(self):
        row = self.win.ai_provider_rows["groq"]
        new_key = "gsk_ui_save_test_123"
        row["var"].set(new_key)
        row["save_btn"].invoke()
        self.assertTrue(any(
            u.get("GROQ_API_KEY") == new_key for u in self.write_calls),
            "write_env не получил новый Groq-ключ")
        self.assertNotIn(new_key, self.win.status.cget("text"))
        # После сохранения карточка осталась в актуальном состоянии.
        self.assertIn("Configured",
                      row["status_lbl"].cget("text"))

    def test_delete_calls_delete_keys_and_resets_status(self):
        row = self.win.ai_provider_rows["groq"]
        with mock.patch.object(cs, "messagebox", wraps=cs.messagebox) as mb:
            mb.askyesno.return_value = True
            row["del_btn"].invoke()
        self.assertIn(["GROQ_API_KEY"], self.delete_calls)
        self.assertEqual(row["var"].get(), "")
        self.assertIn("Not configured",
                      row["status_lbl"].cget("text"))

    # --- Test Connection -----------------------------------------
    def test_test_connection_uses_runtime_and_success_status(self):
        row = self.win.ai_provider_rows["groq"]
        fake = _FakeRuntime()
        with mock.patch.object(cs, "AIBrain", return_value=fake):
            self.win._test_connection("groq", row)
            self._drive(row, "Connection successful")
        self.assertTrue(row["test_lbl"].cget("text").startswith("\u2713"))
        self.assertIn("Connected", row["status_lbl"].cget("text"))
        self.assertTrue(self.win.win.winfo_exists())
        # Был вызван именно существующий runtime c конфигом из registry.
        self.assertEqual(len(fake.posted), 1)
        call = fake.posted[0]
        self.assertEqual(call["provider"]["model"], PROVIDERS["groq"]["model"])
        self.assertEqual(call["messages"], [{"role": "user", "content": "ping"}])
        self.assertFalse(call["json_mode"])

    def test_connection_failure_keeps_app_alive(self):
        class _FailBrain:
            def _post_provider(self, *args, **kwargs):
                return _Resp(
                    ok=False, status_code=401,
                    text='{"error":{"message":"Invalid API key: bad"}}')

        row = self.win.ai_provider_rows["groq"]
        with mock.patch.object(cs, "AIBrain", return_value=_FailBrain()):
            self.win._test_connection("groq", row)
            self._drive(row, "Invalid API key")
        text = row["test_lbl"].cget("text")
        self.assertTrue(text.startswith("\u2715"))
        self.assertIn("Invalid API key (HTTP 401)", text)
        self.assertFalse("gsk_ui_fixture_do_not_use" in text)
        self.assertTrue(self.win.win.winfo_exists(), "окно закрылось из-за ошибки")

    def test_connection_rate_limit_classified(self):
        class _RateBrain:
            def _post_provider(self, *args, **kwargs):
                raise cs.RetryableHTTPError(
                    "rate limited",
                    body='{"error":{"message":"Rate limit reached"}}',
                    status=429)

        row = self.win.ai_provider_rows["groq"]
        with mock.patch.object(cs, "AIBrain", return_value=_RateBrain()):
            self.win._test_connection("groq", row)
            self._drive(row, "Rate limit")
        self.assertIn("Rate limit (HTTP 429)",
                      row["test_lbl"].cget("text"))

    def test_connection_network_error(self):
        import requests

        class _NetBrain:
            def _post_provider(self, *args, **kwargs):
                raise requests.exceptions.ConnectionError("refused")

        row = self.win.ai_provider_rows["groq"]
        with mock.patch.object(cs, "AIBrain", return_value=_NetBrain()):
            self.win._test_connection("groq", row)
            self._drive(row, "Network error")
        self.assertIn("no connection", row["test_lbl"].cget("text"))

    def test_no_key_provider_makes_no_network_and_no_crash(self):
        seen = []

        class _NeverInstantiated:
            def __init__(self):
                seen.append(1)

        row = self.win.ai_provider_rows["gemini"]  # ключа нет в фикстуре
        with mock.patch.object(cs, "AIBrain", _NeverInstantiated):
            self.win._test_connection("gemini", row)
        self.assertEqual(seen, [], "runtime не должен создаваться без ключа")
        self.assertIn("Not configured", row["test_lbl"].cget("text"))
        self.assertTrue(self.win.win.winfo_exists())

    def test_unsaved_key_blocks_test(self):
        row = self.win.ai_provider_rows["groq"]
        row["var"].set("gsk_not_saved_yet")
        try:
            self.win._test_connection("groq", row)
        finally:
            row["var"].set(FIXTURE["GROQ_API_KEY"])
        self.assertIn("сохраните", row["test_lbl"].cget("text"))

    # --- deep-link ------------------------------------------------
    def test_ai_deeplink_selects_ai_tab(self):
        with mock.patch.dict(os.environ, {"JARVIS_SETTINGS_TAB": "ai"}):
            w = cs.SettingsWindow(self.root)
        try:
            selected = self.root.tk.call(w.tabs._w, "tab", "current", "-text")
            self.assertIn("Доступы и данные", selected)
            sub_index = w._access_sub.index(w._access_sub.select())
            self.assertEqual(sub_index, 2, "должна открыться AI / API KEYS")
        finally:
            w.win.destroy()


if __name__ == "__main__":
    unittest.main()