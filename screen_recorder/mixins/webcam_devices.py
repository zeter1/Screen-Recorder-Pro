from ..shared import *


class WebcamDevicesMixin:
    def get_dshow_video_devices(self):
        if os.name != "nt":
            return []
        try:
            result = self.run_managed_process(
                [self.ffmpeg_path, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
                creationflags=self.creation_flags(),
                expected_returncodes=(0, 1),
            )
            text = (result.stderr or "") + "\n" + (result.stdout or "")
        except Exception:
            return []

        devices = []
        in_video_section = False
        for line in text.splitlines():
            lower = line.lower()
            if "directshow video devices" in lower:
                in_video_section = True
                continue
            if "directshow audio devices" in lower:
                in_video_section = False
            match = re.search(r'"(.+?)"\s*\(video\)', line)
            if not match and in_video_section and "alternative name" not in lower:
                match = re.search(r'"([^"]+)"', line)
            if match:
                name = match.group(1).strip()
                if name and name not in devices:
                    devices.append(name)
        return devices

    def refresh_webcam_devices(self, silent=False):
        devices = self.get_dshow_video_devices()
        old = str(self.webcam_device_var.get() or WEBCAM_AUTO).strip() or WEBCAM_AUTO
        values = [WEBCAM_AUTO]
        for name in devices:
            if name not in values:
                values.append(name)
        if old != WEBCAM_AUTO and old not in values:
            values.append(old)
        self.webcam_devices = values

        try:
            if self.webcam_combo is not None and self.webcam_combo.winfo_exists():
                self.webcam_combo.configure(values=self.webcam_devices)
        except Exception:
            pass

        if old not in self.webcam_devices:
            self.webcam_device_var.set(WEBCAM_AUTO)

        if not silent:
            if devices:
                self.status_var.set(f"Вебкамеры обновлены: {len(devices)}.")
            else:
                self.status_var.set("Вебкамеры не найдены. Проверь подключение камеры и доступность FFmpeg.")

    def get_cached_webcam_devices(self):
        devices = [name for name in list(getattr(self, "webcam_devices", []) or []) if name and name != WEBCAM_AUTO]
        if not devices:
            devices = self.get_dshow_video_devices()
            self.webcam_devices = [WEBCAM_AUTO] + [name for name in devices if name != WEBCAM_AUTO]
        return list(devices)

    def get_selected_webcam_device_name(self):
        selected = str(self.webcam_device_var.get() or "").strip()
        if not selected or selected == WEBCAM_AUTO:
            return None
        return selected

    def get_selected_webcam_index(self):
        selected = self.get_selected_webcam_device_name()
        if not selected:
            return 0
        devices = self.get_cached_webcam_devices()
        try:
            return max(0, devices.index(selected))
        except Exception:
            return 0
