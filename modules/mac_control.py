import subprocess


class MacController:
    def open_app(self, app_name: str) -> None:
        subprocess.run(["open", "-a", app_name], check=True)