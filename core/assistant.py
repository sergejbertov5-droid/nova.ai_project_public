import logging
import random
from typing import Protocol

import voice.state as state
import voice.dialog_log as dialog_log

from core import conversation
from core.agent_runtime import AgentRuntime
from core.commands import CommandRouter


# Фразы-«продолжения» и их значения (зависит от контекста предыдущего ответа).
_CHAIN_CONTINUE = {
    "продолжи", "продолжай", "дальше", "ещё", "продолжить", "ещё подробнее",
}
_CHAIN_REPHRASE = {
    "попробуй иначе", "иначе", "по-другому", "переформулируй",
    "перефразируй", "сформулируй иначе", "по-иному",
}
_CHAIN_NEGATIVE = {
    "не то", "неверно", "неправильно", "не так", "не нравится", "нет неправильно",
}

# Короткие «индикаторы мышления» перед ответом AI (не длинная пауза-тишина).
_THINKING_FILLERS = [
    "Одну секунду, сэр.",
    "Секунду, сэр, уточняю.",
    "Принял, сэр. Произвожу вычисления.",
    "Уже думаю, сэр.",
    "Одну минуту, сэр.",
]


class Speaker(Protocol):

    def speak(
        self,
        text: str
    ) -> None:
        ...



    def stop(
        self
    ) -> None:
        ...



class Assistant:


    def __init__(
        self,
        speaker: Speaker
    ) -> None:

        self.speaker = speaker

        self.router = CommandRouter()

        self.runtime = AgentRuntime(
            self.router
        )

        # Контекст для команд-цепочек («продолжи», «попробуй иначе»...).
        self._last_user_text = ""
        self._last_response = ""

        # Филлер «Одну секунду» перед AI включается только в голосовом режиме.
        self.say_filler = False

        self._fillers = _THINKING_FILLERS



    def speak(
        self,
        text: str
    ) -> None:

        if not text:
            return

        text = str(text).strip()
        if not text:
            return

        # История диалога: NOVA AI говорит → пишем в ленту панели.
        try:
            dialog_log.append_turn("nova_ai", text)
        except Exception:
            logging.exception("Dialog log error (speak)")

        # Тихий режим: без озвучки, только визуально (история диалога).
        if state.is_silent():
            logging.info("SILENT: %s", text)
            print(f"[панель] NOVA AI: {text}")
            return

        try:
            self.speaker.speak(text)
        except Exception:
            logging.exception("Speaker error")



    def stop_speaking(
        self
    ):

        try:

            if hasattr(
                self.speaker,
                "stop"
            ):

                self.speaker.stop()


        except Exception:

            logging.exception(
                "Stop speech error"
            )



    def handle(
        self,
        text: str
    ) -> None:


        if not text:

            return

        text = str(text).strip()
        if not text:
            return

        logging.info(
            "User: %s",
            text
        )

        try:

            # История диалога: пишем реплику пользователя.
            try:
                dialog_log.append_turn("user", text)
            except Exception:
                logging.exception("Dialog log error (handle)")

            # Команды-цепочки: «продолжи», «попробуй иначе», «не то».
            #
            # Но только когда НИЧЕГО не ждёт подтверждения. Иначе фраза
            # «продолжи» ушла бы в AI и выполнила действие в обход
            # подтверждения, на которое сэр ещё не ответил: цепочка
            # обращается к router напрямую и не проходит через
            # AgentRuntime._handle_pending_confirmation. При ожидающем
            # подтверждении реплику обрабатывает runtime — он либо
            # исполнит действие, либо повторит вопрос.
            chain_response = None
            if not self.runtime.has_pending_confirmation():
                chain_response = self._handle_chain(text)
            if chain_response is not None:
                self.speak(chain_response)
                self._last_user_text = text
                self._last_response = chain_response
                self._remember_exchange(text, chain_response)
                return

            response = self.runtime.handle(text)

            logging.info(
                "Action completed"
            )

            self._last_user_text = text
            self._last_response = response

            self.speak(response)
            self._remember_exchange(text, response)


        except Exception:

            logging.exception(
                "Assistant error"
            )

            self.speak(
                "Сэр, произошла ошибка выполнения."
            )



    def filler(self) -> None:
        """Короткая фраза перед ответом AI (если режим это позволяет)."""
        if not self.say_filler:
            return
        try:
            self.speak(random.choice(self._fillers))
        except Exception:
            logging.exception("Filler error")

    @staticmethod
    def _remember_exchange(text: str, response: str) -> None:
        """Запомнить завершённый обмен в оперативной памяти беседы.

        Именно здесь, а не до вызова runtime: пока запрос выполняется,
        его собственной реплики в контексте быть не должно, иначе модель
        увидит вопрос дважды и начнёт опираться на саму себя.
        """
        try:
            conversation.record_exchange(text, response)
        except Exception:
            # Память беседы — вспомогательная. Потеря контекста не должна
            # превращаться в потерю ответа, который сэр уже получил.
            logging.exception("Не удалось сохранить обмен в память беседы")



    # ------------------------------------------------------------------
    # Команды-цепочки (контекст предыдущего ответа)
    # ------------------------------------------------------------------
    def _handle_chain(self, text: str):
        low = text.lower().strip()

        if low in _CHAIN_NEGATIVE:
            return (
                "Сэр, уточните, пожалуйста, что именно вы имели в виду "
                "иначе — и я сразу скорректирую ответ."
            )

        if low in _CHAIN_CONTINUE:
            if not self._last_response:
                return (
                    "Сэр, это была первая команда — пока продолжать нечего. "
                    "Задайте вопрос, и я отвечу."
                )
            return self._ask_ai_chain(
                "Продолжи и разверни свой предыдущий ответ: "
                f"«{self._last_response}»"
            )

        if low in _CHAIN_REPHRASE:
            if not self._last_response:
                return (
                    "Сэр, пока нечего переформулировать — задайте сначала вопрос."
                )
            return self._ask_ai_chain(
                "Переформулируй иначе свой предыдущий ответ: "
                f"«{self._last_response}»"
            )

        return None

    def _ask_ai_chain(self, prompt: str) -> str:
        try:
            if not self.router.brain.is_available():
                return (
                    "Сэр, сейчас я работаю в локальном режиме без онлайн-AI — "
                    "полноценно продолжить не смогу. Сформулируйте запрос "
                    "ещё раз по-другому, и я отвечу."
                )
            # Филлер произносится ради эффекта, возвращаемое значение не
            # используется: раньше результат присваивался в локальную
            # переменную и сразу терялся, что вводило в заблуждение
            # при чтении кода.
            self.filler()
            result = self.router.execute_ai(prompt)
            if not result or result == "Ошибка AI связи.":
                return (
                    "Сэр, AI сейчас недоступен — скажите, что именно "
                    "продолжить, и я отвечу по-новому."
                )
            return result
        except Exception:
            logging.exception("Chain AI error")
            return (
                "Сэр, не удалось продолжить через AI. Сформулируйте, "
                "пожалуйста, запрос по-другому."
            )