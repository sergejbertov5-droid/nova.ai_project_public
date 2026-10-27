"""
test_startup_shutdown.py — запуск и остановка NOVA AI.

Что здесь проверяется
--------------------
Каждая подсистема обязана вести себя так, чтобы отказ одной не ронял
остальные, а выход не оставлял занятых ресурсов:

* `MacOSTTS` — последний рубеж озвучки: работает без ключа и сети,
  останавливается по `stop()`, не падает, если `say` недоступен;
* `_probe_microphone` — не оставляет занятый CoreAudio-поток (второй
  поток на том же устройстве ломает микрофон на macOS);
* `setup_signal_handlers` — ловит SIGHUP, иначе закрытие терминала
  оставляет NOVA AI живым с занятым микрофоном;
* `cleanup_temp_files` — действительно убирает то, что NOVA AI насыпал
  вокруг себя, и не трогает то, что не его;
* `restore_original_proxy_env` — возвращает HTTP(S)_PROXY пользователю,
  иначе после выхода всё запущенное ходит через мёртвый туннель;
* `Lifecycle` — сбой подсистемы даёт деградацию, а не падение NOVA AI;
* `SingleInstanceGuard` — второй NOVA AI не делит микрофон.
"""
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import main
from core.lifecycle import STATUS_DEGRADED, STATUS_OK, Lifecycle
from voice.text_to_speech import MacOSTTS


class MacOSTSTest(unittest.TestCase):
    """Системный голос — единственный, который не может отказать по сети."""

    def setUp(self):
        self.state_patch = patch("voice.state.is_silent", return_value=False)
        self.start_patch = patch("voice.state.start_speaking")
        self.stop_patch = patch("voice.state.stop_speaking")
        self.state_patch.start()
        self.start_patch.start()
        self.stop_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.start_patch.stop)
        self.addCleanup(self.stop_patch.stop)

    def test_pick_voice_uses_first_available(self):
        # Голоса проверяются до старта, чтобы «Доброе утро» не ушло
        # в никуда с ошибкой «voice not found».
        with patch.object(MacOSTTS, "_voice_exists", side_effect=lambda n: n == "Yuri"):
            tts = MacOSTTS()
        self.assertEqual(tts.voice, "Yuri")

    def test_pick_voice_falls_back_to_system_when_none_found(self):
        # Ни одного голоса не нашли — не падаем: пустой голос означает
        # «say сам выберет», и это лучше, чем отказ озвучки на старте.
        with patch.object(MacOSTTS, "_voice_exists", return_value=False):
            tts = MacOSTTS()
        self.assertEqual(tts.voice, "")

    def test_voice_exists_is_false_when_say_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no say")):
            self.assertFalse(MacOSTTS._voice_exists("Milena"))

    def test_voice_exists_matches_whole_names_only(self):
        # Голоса в macOS перечисляются по стокам. Имя голоса ищется целиком:
        # подстрока «Alex» нашлась бы и в строке «Alexa», и мы бы выбрали
        # несуществующий голос вместо существующего.
        probe = subprocess.CompletedProcess(
            args=["say"], returncode=0,
            stdout=b"Alex       en_US    # Male\nMilena     ru_RU\n",
        )
        with patch("subprocess.run", return_value=probe):
            self.assertTrue(MacOSTTS._voice_exists("Alex"))
            self.assertFalse(MacOSTTS._voice_exists("Alexa"))
            self.assertFalse(MacOSTTS._voice_exists("Pavel"))

    def test_voice_exists_is_false_when_say_fails(self):
        probe = subprocess.CompletedProcess(args=["say"], returncode=1, stdout=b"")
        with patch("subprocess.run", return_value=probe):
            self.assertFalse(MacOSTTS._voice_exists("Milena"))

    def test_voice_exists_is_false_when_say_times_out(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("say", 5)):
            # Висящий `say` не должен висеть на старте NOVA AI.
            self.assertFalse(MacOSTTS._voice_exists("Milena"))

    def test_speak_launches_say_and_clears_speaking_flag(self):
        fake = MagicMock()
        fake.poll.return_value = None
        fake.communicate.return_value = (b"", b"")
        fake.returncode = 0
        with patch("subprocess.Popen", return_value=fake) as popen, \
             patch.object(MacOSTTS, "_pick_voice", return_value="Milena"):
            tts = MacOSTTS()
            tts.speak("Проверка")
        command = popen.call_args[0][0]
        self.assertEqual(command[0], "say")
        self.assertIn("Milena", command)
        # Строка текста передаётся после `--`, иначе фраза, начинающаяся
        # с дефиса, превращается в несуществующий ключ `say`.
        self.assertEqual(command[command.index("--") + 1], "Проверка")
        # Ждём завершения фонового потока проигрывания.
        for _ in range(100):
            if tts.current_process is None:
                break
            time.sleep(0.02)
        self.assertIsNone(tts.current_process)

    def test_silent_mode_prints_but_does_not_speak(self):
        with patch("voice.state.is_silent", return_value=True), \
             patch.object(MacOSTTS, "_pick_voice", return_value="Milena"), \
             patch("subprocess.Popen") as popen:
            MacOSTTS().speak("Тихий ответ")
        popen.assert_not_called()

    def test_missing_say_does_not_raise(self):
        # Нет `say` (сломанный PATH, не-macOS) — NOVA AI обязан продолжить
        # работу текстом, а не упасть вместе с озвучкой.
        with patch("subprocess.Popen", side_effect=OSError("no say")), \
             patch.object(MacOSTTS, "_pick_voice", return_value=""):
            MacOSTTS().speak("Текст")  # не должно бросить

    def test_stop_kills_process_and_waits_for_it(self):
        fake = MagicMock()
        fake.poll.return_value = None
        fake.wait.side_effect = [subprocess.TimeoutExpired("say", 2), None]
        # Проигрывание держим заблокированным, иначе фоновый поток успеет
        # сам снять процесс до того, как мы проверим stop().
        release = threading.Event()
        fake.communicate.side_effect = lambda *a, **k: release.wait(5) or (b"", b"")
        with patch("subprocess.Popen", return_value=fake), \
             patch.object(MacOSTTS, "_pick_voice", return_value="Milena"):
            tts = MacOSTTS()
            tts.speak("Долгая фраза")
            tts.stop()
        release.set()
        fake.terminate.assert_called()
        # Без ожидания следующая фраза наложилась бы на эту.
        self.assertTrue(fake.wait.called)
        self.assertIsNone(tts.current_process)

    def test_stop_is_safe_when_nothing_is_playing(self):
        MacOSTTS().stop()  # не должно бросить

    def test_bootstrap_tts_falls_back_to_system_voice(self):
        # Облачные голоса недоступны — последний рубеж обязан выжить.
        with patch.object(main, "FishTTS", side_effect=RuntimeError("402")), \
             patch.object(main, "ElevenLabsTTS", side_effect=RuntimeError("403")):
            tts = main._bootstrap_tts()
        self.assertIsInstance(tts, MacOSTTS)

    def test_bootstrap_tts_returns_none_when_nothing_works(self):
        with patch.object(main, "FishTTS", side_effect=RuntimeError("402")), \
             patch.object(main, "ElevenLabsTTS", side_effect=RuntimeError("403")), \
             patch("voice.text_to_speech.MacOSTTS", side_effect=OSError("no say")):
            self.assertIsNone(main._bootstrap_tts())


class ProbeMicrophoneTest(unittest.TestCase):
    """Проверка микрофона не должна оставлять устройство занятым."""

    def test_closes_recognizer_on_success(self):
        rec = MagicMock()
        with patch("voice.speech_to_text.SpeechRecognitionSTT", return_value=rec):
            with patch("voice.speech_to_text.active_mic_level", return_value=(None, None)):
                ok, _msg = main._probe_microphone()
        self.assertTrue(ok)
        # Главное: поток закрыт, иначе голосовой цикл откроет второй
        # на том же устройстве и macOS вернёт «cannot do in current context».
        rec.close.assert_called_once()

    def test_reports_failure_without_raising(self):
        with patch("voice.speech_to_text.SpeechRecognitionSTT", side_effect=RuntimeError("нет доступа")):
            with patch("voice.speech_to_text.active_mic_level", return_value=(None, None)):
                ok, msg = main._probe_microphone()
        self.assertFalse(ok)
        self.assertIn("нет доступа", msg)

    def test_does_not_open_second_stream_when_mic_already_active(self):
        rec = MagicMock()
        with patch("voice.speech_to_text.SpeechRecognitionSTT", return_value=rec) as factory:
            with patch("voice.speech_to_text.active_mic_level", return_value=(420.0, 3)):
                ok, msg = main._probe_microphone()
        self.assertTrue(ok)
        # Поток уже открыт голосовым циклом — второй не открываем.
        factory.assert_not_called()
        self.assertIn("3", msg)

    def test_failure_degrades_subsystem_instead_of_raising(self):
        # Проверка — диагностика. Её сбой обязан быть виден как деградация,
        # а не ронять запуск NOVA AI.
        lifecycle = Lifecycle()
        with patch("voice.speech_to_text.SpeechRecognitionSTT", side_effect=RuntimeError("busy")):
            with patch("voice.speech_to_text.active_mic_level", return_value=(None, None)):
                with patch.object(main, "LIFECYCLE", lifecycle):
                    ok, _ = main._probe_microphone()
        self.assertFalse(ok)
        self.assertEqual([d["name"] for d in lifecycle.degraded()], ["Микрофон"])


class SignalHandlersTest(unittest.TestCase):
    """Закрытие терминала (SIGHUP) обязано останавливать NOVA AI."""

    def _registered(self, target):
        original = signal.getsignal(target)
        self.addCleanup(signal.signal, target, original)

    def test_sighup_is_handled(self):
        self._registered(signal.SIGHUP)
        main.setup_signal_handlers()
        handler = signal.getsignal(signal.SIGHUP)
        self.assertTrue(callable(handler))
        # Обработчик поднимает SystemExit, а не логирует: логирование
        # внутри обработчика сигнала вешает процесс на блокировке лога.
        with self.assertRaises(SystemExit):
            handler(signal.SIGHUP, None)

    def test_sigterm_and_sigint_are_handled(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig):
                self._registered(sig)
                main.setup_signal_handlers()
                with self.assertRaises(SystemExit):
                    signal.getsignal(sig)(sig, None)


class StartupOrderTest(unittest.TestCase):
    """Порядок регистрации обработчиков в теле main().

    Найдено на реальном запуске: SIGTERM в окне старта убивал NOVA AI
    мгновенно, без единой строки в логе и без раскрутки подсистем.
    Причина — обработчики ставились уже после VPN, health-проверок и
    подбора голоса, а это окно занимает ~13 секунд. Именно в нём сэр
    обычно жмёт Ctrl+C, не дождавшись приветствия.
    """

    def _main_source(self):
        with open(main.__file__, encoding="utf-8") as handle:
            return handle.read()

    def test_signal_handlers_registered_before_resources(self):
        source = self._main_source()
        body = source[source.index("\ndef main("):source.index("\ndef _run_health_checks_safely")]
        handler_at = body.find("setup_signal_handlers()")
        self.assertNotEqual(handler_at, -1, "обработчики сигналов не регистрируются")
        for resource in (
            "ensure_connectivity(",
            "_run_health_checks_safely()",
            "_bootstrap_tts()",
            "init_task_monitor(",
            "assistant.speak(startup)",
        ):
            at = body.find(resource)
            with self.subTest(step=resource):
                self.assertNotEqual(at, -1, f"шаг {resource} не найден в main()")
                self.assertLess(
                    handler_at, at,
                    f"{resource} выполняется ДО регистрации обработчиков сигналов: "
                    "SIGTERM в этом окне убивает процесс без раскрутки",
                )

    def test_shutdown_is_registered_as_atexit(self):
        # SystemExit из обработчика сигнала раскручивает finally в main(),
        # но выход может произойти и мимо него (os._exit, необработанное
        # исключение в потоке) — тогда спасёт только atexit.
        source = self._main_source()
        self.assertIn("atexit.register(_shutdown_everything)", source)


class ShutdownLoggingTest(unittest.TestCase):
    """Остановок в журнале должно быть ровно столько же, сколько выходов.

    Найдено на реальном запуске: `=== NOVA AI STOPPED ===` печаталась дважды
    (из `finally` в main() и из atexit), и по журналу было невозможно понять,
    какой из двух остановов настоящий.
    """

    def test_double_call_logs_stopped_once(self):
        lifecycle = Lifecycle()
        lifecycle.register("Напоминания", stop=lambda: None)
        with patch.object(main, "LIFECYCLE", lifecycle), \
             patch.object(main, "_shutdown_done", False), \
             patch("main.state.stop_speaking"), \
             self.assertLogs(level="INFO") as captured:
            main._shutdown_everything()
            main._shutdown_everything()  # atexit после finally
        stopped = [r for r in captured.records if "=== NOVA AI STOPPED ===" in r.message]
        self.assertEqual(len(stopped), 1, f"остановка отмечена {len(stopped)} раз")

    def test_stops_are_still_attempted_only_once(self):
        calls = []
        lifecycle = Lifecycle()
        lifecycle.register("озвучка", stop=lambda: calls.append(1))
        with patch.object(main, "LIFECYCLE", lifecycle), \
             patch.object(main, "_shutdown_done", False), \
             patch("main.state.stop_speaking"):
            main._shutdown_everything()
            main._shutdown_everything()
        self.assertEqual(calls, [1])


class CleanupTempFilesTest(unittest.TestCase):
    """Уборка при старте: только своё, но зато своё убирается."""

    def _workdir(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(directory)
        for sub in ("logs", os.path.join("logs", "tasks"),
                    os.path.join("data", "screenshots")):
            os.makedirs(sub, exist_ok=True)
        return directory

    def _write(self, path, size=8, age_days=0):
        full = os.path.join(os.getcwd(), path)
        with open(full, "wb") as handle:
            handle.write(b"x" * size)
        if age_days:
            stamp = time.time() - age_days * 86400
            os.utime(full, (stamp, stamp))
        return full

    def test_removes_rotated_but_keeps_current_log(self):
        self._workdir()
        rotated = self._write("logs/nova_ai.jsonl.1", size=64, age_days=90)
        current = self._write("logs/nova_ai.jsonl", size=64)
        main.cleanup_temp_files()
        self.assertFalse(os.path.exists(rotated))
        # Текущий лог пишется прямо сейчас — удалять его нельзя.
        self.assertTrue(os.path.exists(current))

    def test_removes_oversized_log(self):
        self._workdir()
        # Проверяем правило напрямую: растягивать файл на 16 МБ ради
        # одного теста незачем, а проверять надо обе границы сразу.
        big = self._write("logs/nova_ai.jsonl.2", size=4096)
        removed = main._rotate_logs_by_age(
            "logs", "nova_ai.jsonl.2", max_megabytes=0.001, max_age_days=365,
        )
        self.assertEqual([os.path.basename(p) for p in removed], ["nova_ai.jsonl.2"])
        self.assertFalse(os.path.exists(big))

    def test_keeps_rotated_log_within_limits(self):
        self._workdir()
        keep = self._write("logs/nova_ai.jsonl.2", size=64)
        main._rotate_logs_by_age(
            "logs", "nova_ai.jsonl.2", max_megabytes=1, max_age_days=365,
        )
        self.assertTrue(os.path.exists(keep))

    def test_keeps_fresh_small_rotated_log(self):
        self._workdir()
        keep = self._write("logs/dialog.jsonl.1", size=128, age_days=1)
        main.cleanup_temp_files()
        self.assertTrue(os.path.exists(keep))

    def test_removes_old_task_and_screenshot_files(self):
        self._workdir()
        task = self._write(os.path.join("logs", "tasks", "tasks-2020.jsonl.1"), age_days=400)
        shot = self._write(os.path.join("data", "screenshots", "старый.png"), age_days=400)
        smoke = self._write(os.path.join("data", "_smoke_test.json"), age_days=400)
        main.cleanup_temp_files()
        self.assertFalse(os.path.exists(task))
        self.assertFalse(os.path.exists(shot))
        self.assertFalse(os.path.exists(smoke))

    def test_does_not_touch_foreign_files(self):
        self._workdir()
        foreign = self._write("logs/чужой-файл.jsonl", age_days=400)
        other = self._write(os.path.join("logs", "tasks", "мои-задачи.jsonl"), age_days=400)
        main.cleanup_temp_files()
        self.assertTrue(os.path.exists(foreign))
        self.assertTrue(os.path.exists(other))

    def test_missing_files_are_not_an_error(self):
        self._workdir()
        main.cleanup_temp_files()  # ничего нет — не должно бросить


class RestoreProxyEnvTest(unittest.TestCase):
    """Остановка обязана вернуть пользователю его собственные настройки."""

    def test_restores_original_proxy_env(self):
        from core import vpn_service

        names = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]
        saved = {n: os.environ.get(n) for n in names}

        def restore_env():
            for name in names:
                if saved.get(name) is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = saved[name]

        self.addCleanup(restore_env)

        # NOVA AI подменил прокси на время работы.
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9/"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9/"

        vpn_service.restore_original_proxy_env()

        for name in names:
            self.assertEqual(os.environ.get(name), saved.get(name),
                             f"{name} не вернулся в исходное состояние")

    def test_restore_never_raises(self):
        from core import vpn_service

        with patch("core.vpn_service._clear_proxy", side_effect=OSError("busy")):
            vpn_service.restore_original_proxy_env()  # не должно бросить


class LifecycleTest(unittest.TestCase):
    """Реестр подсистем: отказ одной не должен ронять остальные."""

    def test_failed_startup_becomes_degraded_not_exception(self):
        lifecycle = Lifecycle()
        # Ключевое свойство startup_step: исключение НАРУЖУ не выходит.
        # Отказ подсистемы обязан продолжать старт, а не ронять NOVA AI.
        with lifecycle.startup_step("Telegram-бот") as step:
            raise ZeroDivisionError("сеть недоступна")
        self.assertEqual(step.status, STATUS_DEGRADED)
        self.assertIn("сеть недоступна", step.error)
        self.assertEqual([d["name"] for d in lifecycle.degraded()], ["Telegram-бот"])

    def test_healthy_startup_reports_ok(self):
        lifecycle = Lifecycle()
        with lifecycle.startup_step("Напоминания") as step:
            pass
        self.assertEqual(step.status, STATUS_OK)
        self.assertEqual(lifecycle.degraded(), [])

    def test_partial_start_is_rolled_back(self):
        # Подсистема успела занять ресурс и упала — её нужно остановить,
        # иначе останется фоновый поток в никуда.
        lifecycle = Lifecycle()
        stops = []
        with lifecycle.startup_step("Самообучение", stop=lambda: stops.append("stopped")):
            raise RuntimeError("упал на середине")
        self.assertEqual(stops, ["stopped"])

    def test_systemexit_is_not_swallowed(self):
        # SystemExit — это команда «остановись», её нельзя превращать
        # в деградацию: процесс обязан завершиться.
        lifecycle = Lifecycle()
        with self.assertRaises(SystemExit):
            with lifecycle.startup_step("Микрофон"):
                raise SystemExit(0)

    def test_shutdown_runs_stops_in_reverse_order(self):
        order = []
        lifecycle = Lifecycle()
        lifecycle.register("первая", stop=lambda: order.append(1))
        lifecycle.register("вторая", stop=lambda: order.append(2))
        lifecycle.register("третья", stop=lambda: order.append(3))
        lifecycle.shutdown_all()
        # Озвучка регистрируется первой, чтобы останавливаться последней:
        # иначе помощники говорили бы в закрытый канал.
        self.assertEqual(order, [3, 2, 1])

    def test_shutdown_is_idempotent(self):
        calls = []
        lifecycle = Lifecycle()
        lifecycle.register("озвучка", stop=lambda: calls.append(1))
        lifecycle.shutdown_all()
        lifecycle.shutdown_all()
        self.assertEqual(calls, [1])

    def test_failing_stop_does_not_block_the_rest(self):
        stops = []
        lifecycle = Lifecycle()
        lifecycle.register("первая", stop=lambda: stops.append(1))
        lifecycle.register("сломаная",
                           stop=lambda: (_ for _ in ()).throw(RuntimeError("бузится")))
        lifecycle.register("третья", stop=lambda: stops.append(3))
        lifecycle.shutdown_all()
        # Зависшая подсистема не имеет права остановить остановку остальных.
        self.assertEqual(stops, [3, 1])
        self.assertIn("сломаная", lifecycle.stop_failures)


class SingleInstanceTest(unittest.TestCase):
    """Второй NOVA AI не должен делить микрофон и файлы состояния."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.lock_file = os.path.join(self.directory, "nova_ai.lock")

    def test_second_guard_is_refused_while_first_holds_lock(self):
        from core.single_instance import SingleInstanceGuard

        first = SingleInstanceGuard(lock_file=self.lock_file)
        self.addCleanup(first.release)
        if not first.acquire():
            self.skipTest("файловая система не поддерживает flock")
        second = SingleInstanceGuard(lock_file=self.lock_file)
        self.assertFalse(second.acquire())
        self.assertTrue(first.owner_summary())

    def test_lock_is_reusable_after_release(self):
        from core.single_instance import SingleInstanceGuard

        first = SingleInstanceGuard(lock_file=self.lock_file)
        if not first.acquire():
            self.skipTest("файловая система не поддерживат flock")
        first.release()
        second = SingleInstanceGuard(lock_file=self.lock_file)
        self.addCleanup(second.release)
        # После остановки первого NOVA AI второй имеет право занять место.
        self.assertTrue(second.acquire())

    def test_owner_summary_names_the_running_pid(self):
        from core.single_instance import SingleInstanceGuard

        owner = SingleInstanceGuard(lock_file=self.lock_file)
        if not owner.acquire():
            self.skipTest("файловая система не поддерживает flock")
        self.addCleanup(owner.release)

        # Сводку читает тот, кто НЕ смог занять блокировку: она обязана
        # назвать чужой PID, иначе сэр видит «занято» и не понимает, кого
        # останавливать.
        second = SingleInstanceGuard(lock_file=self.lock_file)
        self.assertFalse(second.acquire())
        summary = second.owner_summary()
        self.assertIn(str(os.getpid()), summary)


if __name__ == "__main__":
    unittest.main()