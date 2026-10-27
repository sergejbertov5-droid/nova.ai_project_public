"""
test_vpn_service.py — собственный VPN-сервис NOVA AI (безопасный сетевой слой).
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests

from core import vpn_service
from core.vpn_service import (
    VPNMonitor,
    ensure_connectivity,
    load_servers,
    network_status_text,
    _apply_proxy,
    _clear_proxy,
    _proxy_for,
    _system_proxy,
    _verify_geo_channel,
    discover_ai_channel,
    servers_text,
    set_geo_auto,
)


class FakeRunResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class LoadServersTest(unittest.TestCase):
    def test_missing_file_returns_defaults(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: os.path.exists(directory) and os.rmdir(directory))
        target = os.path.join(directory, "vpn_servers.json")  # не существует
        servers = load_servers(path=target)
        kinds = {s["kind"] for s in servers}
        self.assertIn("direct", kinds)
        self.assertIn("system", kinds)

    def test_reads_custom_file(self):
        handle = tempfile.NamedTemporaryFile(
            suffix=".json", mode="w+", encoding="utf-8", delete=False
        )
        handle.close()
        json.dump({"servers": [
            {"id": "direct", "name": "Прямое", "kind": "direct", "enabled": True},
            {"id": "p1", "name": "Прокси 1", "kind": "proxy", "url": "http://127.0.0.1:9999", "enabled": True},
            {"id": "p2", "name": "Выкл", "kind": "proxy", "url": "http://x", "enabled": False},
        ]}, open(handle.name, "w", encoding="utf-8"))
        self.addCleanup(os.unlink, handle.name)
        servers = load_servers(path=handle.name)
        names = [s["id"] for s in servers]
        self.assertEqual(names, ["direct", "p1"])  # выключенные пропускаются


class ProxyHelpersTest(unittest.TestCase):
    def test_proxy_for_direct_is_none(self):
        self.assertIsNone(_proxy_for({"kind": "direct"}))

    def test_proxy_for_system_uses_scutil(self):
        with patch.object(
            vpn_service,
            "_system_proxy",
            return_value="http://127.0.0.1:10809",
        ):
            self.assertEqual(
                _proxy_for({"kind": "system"}),
                {"http": "http://127.0.0.1:10809", "https": "http://127.0.0.1:10809"},
            )

    def test_proxy_for_system_without_proxy_is_none(self):
        with patch.object(vpn_service, "_system_proxy", return_value=None):
            self.assertIsNone(_proxy_for({"kind": "system"}))

    def test_proxy_for_custom(self):
        self.assertEqual(
            _proxy_for({"kind": "proxy", "url": "http://p:3128"}),
            {"http": "http://p:3128", "https": "http://p:3128"},
        )

    def test_apply_and_clear_environment(self):
        _clear_proxy()
        _apply_proxy({"http": "http://p:1", "https": "http://p:1"})
        self.assertEqual(os.environ.get("HTTPS_PROXY"), "http://p:1")
        self.assertEqual(os.environ.get("all_proxy"), "http://p:1")
        _clear_proxy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            self.assertNotIn(name, os.environ)

    def test_clear_restores_user_original_values(self):
        with patch.dict(vpn_service._original_proxy_env,
                        {"HTTPS_PROXY": "http://user-proxy:1"}):
            _apply_proxy({"http": "http://tunnel:2", "https": "http://tunnel:2"})
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://tunnel:2")
            _clear_proxy()
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://user-proxy:1")

    def test_apply_touches_only_proxy_env_names(self):
        os.environ["JARVIS_ISOLATION_SENTINEL"] = "keep-me"
        try:
            _apply_proxy({"http": "http://t:9", "https": "http://t:9"})
            self.assertEqual(os.environ["JARVIS_ISOLATION_SENTINEL"], "keep-me")
            _clear_proxy()
            self.assertEqual(os.environ["JARVIS_ISOLATION_SENTINEL"], "keep-me")
        finally:
            os.environ.pop("JARVIS_ISOLATION_SENTINEL", None)

    def test_tts_hosts_bypass_proxy(self):
        """Озвучка идёт напрямую: стриминг аудио через прокси рвётся."""
        _clear_proxy()
        try:
            _apply_proxy({"http": "http://p:1", "https": "http://p:1"})
            no_proxy = os.environ.get("NO_PROXY", "")
            for host in vpn_service._DIRECT_HOSTS:
                self.assertIn(host, no_proxy)
            self.assertEqual(os.environ.get("no_proxy"), no_proxy)
        finally:
            _clear_proxy()

    def test_user_no_proxy_is_preserved(self):
        _clear_proxy()
        try:
            os.environ["NO_PROXY"] = "my-internal-host"
            _apply_proxy({"http": "http://p:1", "https": "http://p:1"})
            no_proxy = os.environ["NO_PROXY"]
            self.assertIn("my-internal-host", no_proxy)
            self.assertIn("api.fish.audio", no_proxy)
        finally:
            _clear_proxy()
            os.environ.pop("NO_PROXY", None)


class SystemProxyTest(unittest.TestCase):
    def test_parses_scutil_proxy(self):
        stdout = (
            "CFNetworkCopySystemProxySettings returned a dictionary\n"
            "HTTPEnable : 1\n"
            "HTTPProxy : 127.0.0.1\n"
            "HTTPPort : 10809\n"
            "HTTPSEnable : 1\n"
            "HTTPSProxy : 127.0.0.1\n"
            "HTTPSPort : 0\n"
        )
        with patch.object(
            subprocess,
            "run",
            return_value=FakeRunResult(stdout=stdout),
        ):
            self.assertEqual(_system_proxy(), "http://127.0.0.1:10809")

    def test_no_proxy_when_scutil_empty(self):
        with patch.object(
            subprocess,
            "run",
            return_value=FakeRunResult(stdout="HTTPEnable : 0\n"),
        ):
            self.assertIsNone(_system_proxy())

    def test_no_proxy_on_error(self):
        with patch.object(
            subprocess,
            "run",
            side_effect=OSError("no"),
        ):
            self.assertIsNone(_system_proxy())


class ProbeTest(unittest.TestCase):
    def test_any_endpoint_ok_means_connected(self):
        def fake_get(url, **kwargs):
            if "generativelanguage" in url:
                raise requests.RequestException("blocked")
            return object()

        with patch("core.vpn_service.requests.get", side_effect=fake_get):
            ok, latency = vpn_service._probe(None)
        self.assertTrue(ok)
        self.assertGreater(latency, 0.0)

    def test_all_endpoints_down_means_offline(self):
        def fake_get(url, **kwargs):
            raise requests.RequestException("всё упало")

        with patch("core.vpn_service.requests.get", side_effect=fake_get):
            ok, latency = vpn_service._probe(None)
        self.assertFalse(ok)
        self.assertEqual(latency, 0.0)

    def test_probe_uses_proxy_dict(self):
        captured = {}

        def fake_get(url, **kwargs):
            captured["proxies"] = kwargs.get("proxies")
            return object()

        with patch("core.vpn_service.requests.get", side_effect=fake_get):
            vpn_service._probe({"http": "http://t:1", "https": "http://t:1"})
        self.assertIsNotNone(captured.get("proxies"))


class EnsureConnectivityTest(unittest.TestCase):
    def setUp(self):
        vpn_service._state.update({
            "connected": True,
            "server_id": "direct",
            "server_name": "Прямое подключение",
            "latency_ms": 0.0,
        })
        _clear_proxy()
        self.servers = [
            {"id": "direct", "name": "Прямое подключение", "kind": "direct", "enabled": True},
            {"id": "sys", "name": "Системный прокси", "kind": "system", "enabled": True},
            {"id": "p1", "name": "Мой прокси", "kind": "proxy",
             "url": "http://127.0.0.1:10809", "enabled": True},
        ]

    def test_direct_wins(self):
        with (
            patch.object(vpn_service, "load_servers", return_value=self.servers),
            patch.object(vpn_service, "_probe", return_value=(True, 12.0)),
        ):
            result = ensure_connectivity()
        self.assertTrue(result["connected"])
        self.assertEqual(result["server_id"], "direct")
        # прямое подключение → прокси из env убраны
        self.assertNotIn("HTTPS_PROXY", os.environ)

    def test_proxy_fallback_when_direct_down(self):
        def probe(proxy):
            if proxy is not None:
                return True, 33.0
            return False, 0.0

        with (
            patch.object(vpn_service, "load_servers", return_value=self.servers),
            patch.object(vpn_service, "_probe", side_effect=probe),
            # системного прокси нет — канал system пропускается без пробы
            patch.object(vpn_service, "_system_proxy", return_value=None),
        ):
            result = ensure_connectivity()
        self.assertTrue(result["connected"])
        self.assertIn("прокси", result["server_name"].casefold())
        self.assertEqual(os.environ.get("HTTPS_PROXY"), "http://127.0.0.1:10809")

    def test_all_down_goes_offline(self):
        with (
            patch.object(vpn_service, "load_servers", return_value=self.servers),
            patch.object(vpn_service, "_probe", return_value=(False, 0.0)),
        ):
            result = ensure_connectivity()
        self.assertFalse(result["connected"])
        self.assertEqual(result["server_id"], "offline")
        self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertIn("локаль", result["message"].casefold())

    def test_disabled_skips_probing(self):
        with (
            patch.object(vpn_service, "JARVIS_VPN_ENABLED", False),
            patch.object(vpn_service, "_probe", side_effect=AssertionError("не должно пробовать")),
        ):
            result = ensure_connectivity()
        self.assertTrue(result["connected"])
        self.assertEqual(result["server_id"], "disabled")


class GeoAutoTest(unittest.TestCase):
    def setUp(self):
        vpn_service.set_geo_auto(False)
        vpn_service._state.update({
            "connected": True,
            "server_id": "direct",
            "server_name": "Прямое подключение",
            "latency_ms": 0.0,
        })
        vpn_service._last_geo_scan = 0.0
        cache = vpn_service._GEO_CACHE_FILE
        if cache.exists():
            cache.unlink()
        _clear_proxy()

    def tearDown(self):
        vpn_service.set_geo_auto(False)
        cache = vpn_service._GEO_CACHE_FILE
        if cache.exists():
            cache.unlink()
        _clear_proxy()

    @patch("core.vpn_service._online_ai_provider")
    @patch("core.vpn_service._scan_geo_channels")
    @patch("core.vpn_service._geo_reachable")
    def test_direct_blocked_finds_geo_channel(self, geo_reachable, scan, provider):
        vpn_service.set_geo_auto(True)
        provider.return_value = {"id": "gemini", "url": "https://x", "model": "m", "key": "k"}
        geo_reachable.return_value = (False, 0.0)  # прямой канал гео-заблокирован
        scan.return_value = {
            "id": "geo-DE",
            "name": "Собственный VPN · Германия (авто)",
            "kind": "proxy",
            "url": "http://1.2.3.4:8080",
            "country": "DE",
            "latency_ms": 55.0,
        }
        result = discover_ai_channel(timeout=5.0, force=True)
        self.assertIsNotNone(result)
        self.assertEqual(result["server_id"], "geo-DE")
        self.assertEqual(os.environ.get("HTTPS_PROXY"), "http://1.2.3.4:8080")
        # канал сохранился в кэш для быстрого старта
        self.assertTrue(vpn_service._GEO_CACHE_FILE.exists())
        scan.assert_called_once()

    @patch("core.vpn_service._online_ai_provider")
    @patch("core.vpn_service._scan_geo_channels")
    def test_direct_works_keeps_it(self, scan, provider):
        vpn_service.set_geo_auto(True)
        provider.return_value = {"id": "gemini", "url": "https://x", "model": "m", "key": "k"}

        def fake_reachable(proxy, timeout=None):
            # прямой канал (proxy=None) не гео-блокирован — ничего не ищем
            return (proxy is None, 12.0)

        with patch("core.vpn_service._geo_reachable", side_effect=fake_reachable):
            result = discover_ai_channel(timeout=5.0, force=True)
        self.assertIsNone(result)
        scan.assert_not_called()
        self.assertNotIn("HTTPS_PROXY", os.environ)

    @patch("core.vpn_service._online_ai_provider")
    def test_disabled_returns_none(self, provider):
        vpn_service.set_geo_auto(False)
        provider.assert_not_called()
        self.assertIsNone(discover_ai_channel(timeout=2.0))

    @patch("core.vpn_service._online_ai_provider")
    @patch("core.vpn_service._fetch_proxy_candidates")
    @patch("core.vpn_service._proxy_country")
    @patch("core.vpn_service._geo_reachable")
    @patch("core.vpn_service._ai_probe")
    def test_scan_excludes_cis_and_picks_best(self, probe, geo_reachable, country, fetch, provider):
        fetch.return_value = [
            "1.1.1.1:8080",   # СНГ — дешёвая гео-проба не проходит
            "2.2.2.2:8080",   # Германия, медленно
            "3.3.3.3:8080",   # Нидерланды, быстро — лучший
        ]
        geo_table = {
            "http://1.1.1.1:8080": (False, 500.0),
            "http://2.2.2.2:8080": (True, 100.0),
            "http://3.3.3.3:8080": (True, 90.0),
        }
        probe_table = {
            "http://2.2.2.2:8080": (True, 300.0),
            "http://3.3.3.3:8080": (True, 40.0),
        }
        country_table = {
            "http://2.2.2.2:8080": "DE",
            "http://3.3.3.3:8080": "NL",
        }

        def fake_geo(proxy, timeout=None):
            return geo_table.get(proxy["https"], (False, 0.0))

        def fake_probe(proxy, timeout=None):
            return probe_table.get(proxy["https"], (False, 0.0))

        def fake_country(proxy):
            return country_table.get(proxy["https"])

        geo_reachable.side_effect = fake_geo
        probe.side_effect = fake_probe
        country.side_effect = fake_country
        channel = vpn_service._scan_geo_channels(budget=10.0)
        self.assertIsNotNone(channel)
        self.assertEqual(channel["url"], "http://3.3.3.3:8080")
        self.assertEqual(channel["id"], "geo-NL")

    @patch("core.vpn_service._ai_probe")
    def test_verify_geo_channel(self, probe):
        probe.return_value = (True, 22.0)
        channel = {"url": "http://9.9.9.9:3128", "name": "geo"}
        self.assertTrue(_verify_geo_channel(channel))
        self.assertEqual(channel["latency_ms"], 22.0)
        probe.return_value = (False, 0.0)
        self.assertFalse(_verify_geo_channel(channel))

    @patch("core.vpn_service._online_ai_provider")
    def test_geo_autoselect_cache_reuse(self, provider):
        # Сначала находим канал и кэшируем его.
        vpn_service.set_geo_auto(True)
        provider.return_value = {"id": "gemini", "url": "https://x", "model": "m", "key": "k"}

        def probe_first(proxy, timeout=None):
            # живые каналы дают рабочий AI (для verify-пробы).
            return (proxy is not None, 60.0)

        with (
            patch("core.vpn_service._scan_geo_channels") as scan,
            patch("core.vpn_service._ai_probe", side_effect=probe_first),
            patch("core.vpn_service._geo_reachable",
                  return_value=(False, 0.0)),  # direct гео-заблокирован
        ):
            scan.return_value = {
                "id": "geo-FR",
                "name": "Собственный VPN · Франция (авто)",
                "kind": "proxy",
                "url": "http://8.8.8.8:8888",
                "country": "FR",
                "latency_ms": 60.0,
            }
            first = discover_ai_channel(timeout=5.0, force=True)
        self.assertEqual(first["server_id"], "geo-FR")

        # Второй запуск: канал из кэша, без повторного сканирования.
        cache = vpn_service._load_geo_cache()
        self.assertIsNotNone(cache)
        with (
            patch("core.vpn_service._scan_geo_channels") as scan2,
            patch("core.vpn_service._ai_probe", return_value=(True, 61.0)),
        ):
            second = discover_ai_channel(timeout=5.0, force=True)
        self.assertEqual(second["server_id"], "geo-FR")
        scan2.assert_not_called()


class GeoDiagnosticsTest(unittest.TestCase):
    """Пустой результат geo-поиска обязан объяснять причину в логе.

    Вчера «geo-поиск — нет кандидатов в списках» неотличало «источники
    не отдались» (сеть упала) от «списки пусты» — диагностика заняла час.
    """

    @patch("core.vpn_service.JARVIS_VPN_GEO_SOURCES", [])
    def test_empty_source_list_is_reported(self):
        with self.assertLogs(level="WARNING") as caught:
            found = vpn_service._fetch_proxy_candidates()
        self.assertEqual(found, [])
        self.assertIn("не заданы", "\n".join(caught.output))

    @patch("core.vpn_service.JARVIS_VPN_GEO_SOURCES", ["https://dead.example/list.txt"])
    def test_unreachable_source_is_named_in_log(self):
        with patch("core.vpn_service.requests.get", side_effect=requests.Timeout):
            with self.assertLogs(level="WARNING") as caught:
                found = vpn_service._fetch_proxy_candidates()
        self.assertEqual(found, [])
        message = "\n".join(caught.output)
        self.assertIn("dead.example", message)
        self.assertIn("таймаут", message)

    @patch("core.vpn_service.JARVIS_VPN_GEO_SOURCES", ["https://lists.example/proxies.txt"])
    def test_empty_body_is_reported_as_zero_pairs(self):
        with patch("core.vpn_service.requests.get",
                   return_value=Mock(status_code=200, text="адресов нет")):
            with self.assertLogs(level="WARNING") as caught:
                found = vpn_service._fetch_proxy_candidates()
        self.assertEqual(found, [])
        message = "\n".join(caught.output)
        self.assertIn("lists.example", message)
        self.assertIn("пар=0", message)

    @patch("core.vpn_service.JARVIS_VPN_GEO_SOURCES", ["https://lists.example/p.txt"])
    def test_healthy_source_summary_is_logged(self):
        with patch("core.vpn_service.requests.get",
                   return_value=Mock(status_code=200, text="1.2.3.4:8080")):
            with self.assertLogs(level="INFO") as caught:
                found = vpn_service._fetch_proxy_candidates()
        self.assertEqual(found, ["1.2.3.4:8080"])
        self.assertIn("пар=1", "\n".join(caught.output))

    @patch("core.vpn_service._geo_reachable", return_value=(False, 500.0))
    @patch("core.vpn_service._fetch_proxy_candidates", return_value=["5.5.5.5:8080"])
    def test_scan_counts_cheap_geo_rejections(self, fetch, geo):
        with self.assertLogs(level="WARNING") as caught:
            channel = vpn_service._scan_geo_channels(budget=5.0)
        self.assertIsNone(channel)
        message = "\n".join(caught.output)
        self.assertIn("кандидатов: 1", message)
        self.assertIn("не дождались: 0", message)
        self.assertIn("гео-проба: 1", message)

    @patch("core.vpn_service._ai_probe", return_value=(False, 0.0))
    @patch("core.vpn_service._geo_reachable", return_value=(True, 10.0))
    @patch("core.vpn_service._fetch_proxy_candidates", return_value=["6.6.6.6:8080"])
    def test_scan_counts_ai_probe_rejections(self, fetch, geo, probe):
        with self.assertLogs(level="WARNING") as caught:
            channel = vpn_service._scan_geo_channels(budget=5.0)
        self.assertIsNone(channel)
        message = "\n".join(caught.output)
        self.assertIn("AI-проба: 1", message)
        self.assertIn("не дождались: 0", message)


class StatusAndMonitorTest(unittest.TestCase):
    def test_status_text_connected(self):
        vpn_service._state.update({
            "connected": True, "server_name": "Прямое подключение", "latency_ms": 5.0,
        })
        text = network_status_text()
        self.assertIn("интернет в порядке", text)
        self.assertIn("Прямое подключение", text)

    def test_status_text_offline(self):
        vpn_service._state.update({
            "connected": False, "server_name": "Сеть недоступна", "latency_ms": 0.0,
        })
        text = network_status_text()
        self.assertIn("недоступен", text)
        self.assertIn("локаль", text.casefold())

    def test_monitor_stops_when_disabled(self):
        monitor = VPNMonitor(tick=1)
        with patch.object(vpn_service, "JARVIS_VPN_ENABLED", False):
            monitor.run()  # должен выйти сразу, без сети

    def test_monitor_loops_and_stops_cleanly(self):
        monitor = VPNMonitor(tick=0.05)
        with (
            patch.object(vpn_service, "JARVIS_VPN_ENABLED", True),
            patch.object(vpn_service, "ensure_connectivity") as mocked,
        ):
            monitor.start()
            time.sleep(0.3)
            monitor.stop()
            monitor.join(timeout=2)
        self.assertFalse(monitor.is_alive())
        self.assertGreaterEqual(mocked.call_count, 1)  # сеть перепроверялась


if __name__ == "__main__":
    unittest.main()