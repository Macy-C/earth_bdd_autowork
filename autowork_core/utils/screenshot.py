import os
import hashlib
import math
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger
from mss import mss

from config.paths import Paths
from autowork_core.utils.bus import timestamp, safe_name


class ScreenRecorder:
    """
    桌面录屏工具。

    说明：
    - 使用项目内置 ffmpeg 录制 mp4
    - OCR/PIC 和失败截图仍使用 mss，录屏不再参与 mss 抓屏竞争
    - 支持 start / stop
    - 适合 behave 的 before_scenario / after_scenario
    """

    def __init__(self, fps=5, monitor_index=1, buffer_seconds=8, segment_seconds=2, max_width=1280,
                 ffmpeg_path="", ffmpeg_preset="ultrafast", ffmpeg_crf=28, ffmpeg_draw_mouse=True):
        self.output_dir = Paths.RECORDINGS_DIR
        os.makedirs(self.output_dir, exist_ok=True)
        self.screenshot_dir = Paths.SCREENSHOTS_DIR
        os.makedirs(self.screenshot_dir, exist_ok=True)
        self.fps = max(1, int(float(fps or 1)))
        self.monitor_index = max(1, int(float(monitor_index or 1)))
        self.buffer_seconds = max(1.0, float(buffer_seconds or 1))
        self.segment_seconds = max(1.0, float(segment_seconds or 1))
        self.max_width = max(0, int(float(max_width or 0)))
        self.ffmpeg_path = str(ffmpeg_path or "").strip()
        self.ffmpeg_preset = str(ffmpeg_preset or "ultrafast").strip()
        self.ffmpeg_crf = int(float(ffmpeg_crf if ffmpeg_crf is not None else 28))
        self.ffmpeg_draw_mouse = bool(ffmpeg_draw_mouse)
        self._running = False
        self._process = None
        self._stderr_file = None
        self._stderr_path = None
        self._file_path = None
        self._segment_dir = None
        self._segment_pattern = None
        self._mode = "all"

    def save_screenshot(self,name="screenshot"):
        """
        保存 pywinauto 截图到本地。
        element: 可以是 context.window,也可以是某个控件对象
        """
        file_path = str(self._artifact_path(name, ".png", output_dir=self.screenshot_dir))
        with mss() as sct:
            sct.shot(output=file_path)
        return file_path

    def start(self, feature_name='feature', scenario_name="scenario", mode="all"):
        """
        开始录屏。
        """

        mode = self._normalize_mode(mode)
        if mode == "off":
            return None

        if self._running:
            return self._file_path if self._mode == "all" else None

        self._file_path = self._artifact_path(f"{feature_name}_{scenario_name}", ".mp4")
        self._mode = mode

        if not self._start_ffmpeg(feature_name, scenario_name):
            self._file_path = None
            return None

        return self._file_path if self._mode == "all" else None

    @staticmethod
    def _monitor_text(monitor):
        if not monitor:
            return "unknown"
        return (
            f"left={monitor.get('left')},top={monitor.get('top')},"
            f"width={monitor.get('width')},height={monitor.get('height')}"
        )

    def stop(self):
        """
        停止录屏，并返回视频路径。
        """

        if not self._running:
            return self._file_path if self._mode == "all" else None

        self._running = False

        process = self._process
        if process is not None and process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.write("q\n")
                    process.stdin.flush()
            except Exception:
                pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg 录屏停止超时，尝试终止进程")
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        elif process is not None:
            logger.warning(f"ffmpeg 录屏进程已提前退出 code={process.returncode}, log={self._stderr_path}")

        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except Exception:
                pass

        self._process = None
        self._close_stderr_file()

        return self._file_path if self._mode == "all" else None

    def save_buffered_video(self):
        """
        返回失败场景录屏路径。failed 模式会合并最近 BUFFER_SECONDS 秒分片。
        """

        if self._running:
            self.stop()

        if self._mode == "failed" and self._segment_dir:
            return self._merge_recent_segments()

        if not self._file_path or not self._file_path.exists():
            logger.warning(f"失败录屏文件不存在: {self._file_path}, ffmpeg_log={self._stderr_path}")
            return None
        if self._file_path.stat().st_size <= 0:
            logger.warning(f"失败录屏文件为空: {self._file_path}, ffmpeg_log={self._stderr_path}")
            return None
        return self._file_path

    def clear(self):
        self.delete()
        self._file_path = None

    def delete(self, retries=10, interval=0.2):
        """
        删除当前录屏文件。
        """

        if self._running:
            self.stop()

        if not self._file_path or not self._file_path.exists():
            self._delete_stderr_log()
            self._delete_segment_dir()
            return True

        deleted = False
        for attempt in range(1, retries + 1):
            try:
                self._file_path.unlink()
                deleted = True
                break
            except PermissionError as e:
                if attempt >= retries:
                    logger.warning(f"录屏文件仍被占用，删除失败: {self._file_path}, err={e}")
                    return False
                time.sleep(interval)

        self._delete_stderr_log()
        self._delete_segment_dir()
        return deleted

    @staticmethod
    def _normalize_mode(mode):
        mode = str(mode or "all").strip().lower()
        return mode if mode in ("off", "failed", "all") else "all"

    def _artifact_path(self, name, suffix, output_dir=None):
        output_dir = output_dir or self.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = suffix if str(suffix).startswith(".") else f".{suffix}"
        raw_name = str(name or "artifact")
        safe = safe_name(raw_name).strip(" .") or "artifact"
        digest = hashlib.sha1(safe.encode("utf-8", errors="ignore")).hexdigest()[:10]
        tail = f"_{timestamp()}_{digest}{suffix}"

        # Keep the full path under the legacy Windows MAX_PATH boundary with margin.
        max_path = 240 if os.name == "nt" else 4096
        max_component = 240 - len(tail)
        max_by_path = max_path - len(str(output_dir)) - 1 - len(tail)
        max_stem = max(16, min(160, max_component, max_by_path))
        if len(safe) > max_stem:
            safe = safe[:max_stem].rstrip(" ._-") or safe[:max_stem]

        return output_dir / f"{safe}{tail}"

    def _start_ffmpeg(self, feature_name, scenario_name):
        ffmpeg_path = self._resolve_ffmpeg_path()
        if ffmpeg_path is None:
            logger.warning(f"ffmpeg 不存在，录屏已跳过: {self._expected_ffmpeg_path()}")
            return False

        monitor = self._select_monitor()
        if self._mode == "failed":
            self._prepare_segment_dir(feature_name, scenario_name)
        cmd = self._ffmpeg_command(ffmpeg_path, monitor)
        self._stderr_path = self._artifact_path(f"ffmpeg_{feature_name}_{scenario_name}", ".log")
        self._stderr_file = open(self._stderr_path, "w", encoding="utf-8", errors="replace")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_file,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            logger.warning(f"ffmpeg 录屏启动失败: {e}, cmd={cmd}")
            self._close_stderr_file()
            return False

        time.sleep(0.2)
        if self._process.poll() is not None:
            logger.warning(
                f"ffmpeg 录屏启动后立即退出 code={self._process.returncode}, "
                f"log={self._stderr_path}, tail={self._stderr_tail()}"
            )
            self._process = None
            self._close_stderr_file()
            return False

        self._running = True
        logger.debug(
            f"ffmpeg 录屏已启动: mode={self._mode}, output={self._file_path}, "
            f"segments={self._segment_dir}, monitor={self._monitor_text(monitor)}"
        )
        return True

    def _expected_ffmpeg_path(self):
        return Path(self.ffmpeg_path) if self.ffmpeg_path else Paths.FFMPEG_EXE

    def _resolve_ffmpeg_path(self):
        path = self._expected_ffmpeg_path()
        if not path.is_absolute():
            path = Paths.BASE_DIR / path
        if not path.exists():
            return None
        if not self._looks_like_windows_exe(path):
            logger.warning(f"ffmpeg.exe 不是有效 Windows 可执行文件: {path}")
            return None
        return path

    @staticmethod
    def _looks_like_windows_exe(path):
        try:
            with open(path, "rb") as file:
                return file.read(2) == b"MZ"
        except Exception:
            return False

    def _select_monitor(self):
        with mss() as sct:
            if self.monitor_index < len(sct.monitors):
                return dict(sct.monitors[self.monitor_index])
            logger.warning(f"录屏 monitor_index={self.monitor_index} 不存在，已使用主屏幕")
            return dict(sct.monitors[1])

    def _ffmpeg_command(self, ffmpeg_path, monitor):
        cmd = [
            str(ffmpeg_path),
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "gdigrab",
            "-framerate",
            str(self.fps),
            "-draw_mouse",
            "1" if self.ffmpeg_draw_mouse else "0",
            "-rtbufsize",
            "100M",
            "-offset_x",
            str(int(monitor.get("left", 0))),
            "-offset_y",
            str(int(monitor.get("top", 0))),
            "-video_size",
            f"{int(monitor.get('width', 1))}x{int(monitor.get('height', 1))}",
            "-i",
            "desktop",
            "-an",
        ]
        if self.max_width:
            cmd.extend(["-vf", f"scale={self.max_width}:-2:force_original_aspect_ratio=decrease"])
        cmd.extend([
            "-c:v",
            "libx264",
            "-preset",
            self.ffmpeg_preset,
            "-crf",
            str(self.ffmpeg_crf),
            "-pix_fmt",
            "yuv420p",
        ])
        if self._mode == "failed":
            gop = max(1, int(round(self.fps * self.segment_seconds)))
            cmd.extend([
                "-g",
                str(gop),
                "-keyint_min",
                str(gop),
                "-sc_threshold",
                "0",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{self.segment_seconds:g})",
                "-f",
                "segment",
                "-segment_time",
                f"{self.segment_seconds:g}",
                "-segment_wrap",
                str(self._segment_wrap_count()),
                "-reset_timestamps",
                "1",
                "-segment_format",
                "mp4",
                str(self._segment_pattern),
            ])
        else:
            cmd.append(str(self._file_path))
        return cmd

    def _segment_wrap_count(self):
        return max(1, int(math.ceil(self.buffer_seconds / self.segment_seconds)) + 1)

    def _prepare_segment_dir(self, feature_name, scenario_name):
        root = self.output_dir / ".recording_segments"
        root.mkdir(parents=True, exist_ok=True)
        raw_name = f"{feature_name}_{scenario_name}"
        safe = safe_name(raw_name).strip(" .") or "recording"
        digest = hashlib.sha1(safe.encode("utf-8", errors="ignore")).hexdigest()[:10]
        name = f"{safe[:80].rstrip(' ._-')}_{timestamp()}_{digest}"
        self._segment_dir = root / name
        if self._segment_dir.exists():
            shutil.rmtree(self._segment_dir, ignore_errors=True)
        self._segment_dir.mkdir(parents=True, exist_ok=True)
        self._segment_pattern = self._segment_dir / "seg_%06d.mp4"

    def _segment_files(self):
        if not self._segment_dir or not self._segment_dir.exists():
            return []
        files = [path for path in self._segment_dir.glob("seg_*.mp4") if path.exists() and path.stat().st_size > 0]
        return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))

    def _recent_segment_files(self):
        files = self._segment_files()
        return files[-self._segment_wrap_count():]

    def _merge_recent_segments(self):
        segments = self._recent_segment_files()
        if not segments:
            logger.warning(f"没有可合并的录屏分片: {self._segment_dir}, ffmpeg_log={self._stderr_path}")
            return None

        list_path = self._segment_dir / "concat.txt"
        list_path.write_text("".join(f"file '{self._concat_path(segment)}'\n" for segment in segments), encoding="utf-8")
        if self._run_concat(list_path, copy_stream=True) or self._run_concat(list_path, copy_stream=False):
            self._delete_segment_dir()
            return self._valid_file_path()

        latest = segments[-1]
        try:
            shutil.copy2(latest, self._file_path)
            logger.warning(f"ffmpeg 分片合并失败，已保留最后一个分片: {latest} -> {self._file_path}")
            self._delete_segment_dir()
            return self._valid_file_path()
        except Exception as e:
            logger.warning(f"ffmpeg 分片合并失败，且最后分片复制失败: {e}, dir={self._segment_dir}")
            return None

    @staticmethod
    def _concat_path(path):
        return path.resolve().as_posix().replace("'", "'\\''")

    def _run_concat(self, list_path, copy_stream=True):
        ffmpeg_path = self._resolve_ffmpeg_path()
        if ffmpeg_path is None:
            return False
        cmd = [
            str(ffmpeg_path),
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
        ]
        if copy_stream:
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend([
                "-c:v",
                "libx264",
                "-preset",
                self.ffmpeg_preset,
                "-crf",
                str(self.ffmpeg_crf),
                "-pix_fmt",
                "yuv420p",
            ])
        cmd.extend(["-movflags", "+faststart", str(self._file_path)])
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and self._valid_file_path() is not None:
            return True
        logger.warning(
            f"ffmpeg 分片合并失败 copy_stream={copy_stream}, code={result.returncode}, "
            f"err={(result.stderr or '')[-1000:]}"
        )
        return False

    def _valid_file_path(self):
        if self._file_path and self._file_path.exists() and self._file_path.stat().st_size > 0:
            return self._file_path
        return None

    def _close_stderr_file(self):
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            finally:
                self._stderr_file = None

    def _stderr_tail(self, max_chars=1000):
        self._close_stderr_file()
        if not self._stderr_path or not self._stderr_path.exists():
            return ""
        try:
            return self._stderr_path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
        except Exception:
            return ""

    def _delete_stderr_log(self):
        self._close_stderr_file()
        if self._stderr_path and self._stderr_path.exists():
            try:
                self._stderr_path.unlink()
            except Exception:
                pass
        self._stderr_path = None

    def _delete_segment_dir(self):
        if self._segment_dir and self._segment_dir.exists():
            shutil.rmtree(self._segment_dir, ignore_errors=True)
        self._segment_dir = None
        self._segment_pattern = None

    @property
    def file_path(self):
        return self._file_path


