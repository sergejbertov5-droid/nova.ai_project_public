-- NOVA AI Control — запускает панель управления (Python/tkinter):
-- статус, включить/выключить NOVA AI, «Настройки» (ключи, разрешения),
-- открытие логов. Весь интерфейс — в control_gui.py + control_settings.py.
-- Запуск синхронный: do shell script убивает фоновые процессы (&),
-- поэтому панель живёт, пока приложение открыто.
-- Папка проекта берётся из настроек пользователя, а не из пути,
-- прописанного вручную: прежний вариант указывал на машину автора,
-- и у покупателя панель не открывалась.
--
-- Чтобы задать свою папку, выполните один раз:
--     defaults write com.nova_ai.control projectPath "/путь/к/NOVA AI"
-- Если значение не задано, берётся папка, содержащая этот скрипт
-- (это работает, когда .applescript лежит рядом с main.py).

on projectPath()
	try
		set p to (do shell script "defaults read com.nova_ai.control projectPath")
	on error
		set p to ""
	end try
	if p is "" then
		try
			set p to (do shell script "dirname " & quoted form of POSIX path of (path to me))
		on error
			set p to (POSIX path of (path to desktop folder) & "NOVA AI")
		end try
	end if
	return p
end projectPath

on run
	try
		do shell script "cd " & quoted form of (my projectPath()) & " && ./.venv/bin/python control_gui.py"
	on error errorMessage
		display dialog "Не удалось открыть панель NOVA AI:" & return & return & errorMessage buttons {"OK"} default button "OK" with title "NOVA AI Control" with icon caution
	end try
end run