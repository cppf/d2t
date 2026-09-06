"""
Small self-contained logging/progress module for drive_to_telegram.py.

No external dependencies (works even if rich/tqdm are not installed in
the image). Falls back to plain text automatically when stdout is not a
real terminal (e.g. piped into a file) or when NO_COLOR is set.
"""

import sys
import time
import shutil


def format_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def format_eta(elapsed, done, total):
    if done <= 0 or total <= 0:
        return "estimating..."
    avg = elapsed / done
    remaining = avg * (total - done)
    return f"~{format_duration(remaining)} remaining"


class Timer:
    def __init__(self):
        self._start = time.monotonic()

    def elapsed(self):
        return time.monotonic() - self._start


class _Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"


class _NoColors:
    def __getattr__(self, name):
        return ""


class ProgressBar:
    """A single live-updating terminal line, e.g. for download/upload %."""

    WIDTH = 28

    def __init__(self, label, log):
        self.label = label
        self.log = log
        self._last_percent = -1
        self._start = time.monotonic()
        self._active = self.log.is_tty

    def update(self, current, total):
        if not total:
            return
        percent = int(current * 100 / total)
        if percent == self._last_percent:
            return
        self._last_percent = percent

        filled = int(self.WIDTH * percent / 100)
        bar = "#" * filled + "-" * (self.WIDTH - filled)
        rate = current / max(time.monotonic() - self._start, 0.001)
        line = (
            f"    {self.log.c.CYAN}{self.label:<10}{self.log.c.RESET} "
            f"[{bar}] {percent:3d}%  "
            f"{format_bytes(current)}/{format_bytes(total)}  "
            f"{format_bytes(rate)}/s"
        )

        if self._active:
            sys.stdout.write("\r" + line.ljust(shutil.get_terminal_size((100, 20)).columns - 1))
            sys.stdout.flush()
        else:
            # Non-tty: only print occasionally so log files stay readable.
            if percent % 20 == 0:
                print(line)

    def finish(self, failed=False):
        if self._active:
            sys.stdout.write("\r" + " " * (shutil.get_terminal_size((100, 20)).columns - 1) + "\r")
            sys.stdout.flush()


class Log:
    def __init__(self, no_color=False):
        self.is_tty = sys.stdout.isatty()
        self.c = _NoColors() if (no_color or not self.is_tty) else _Colors()

    # -- low level -----------------------------------------------------

    def line(self, text=""):
        print(text)

    def section(self, title):
        width = min(shutil.get_terminal_size((100, 20)).columns, 78)
        print()
        print(f"{self.c.BOLD}{self.c.BLUE}{'=' * width}{self.c.RESET}")
        print(f"{self.c.BOLD}{self.c.BLUE}{title}{self.c.RESET}")
        print(f"{self.c.BOLD}{self.c.BLUE}{'=' * width}{self.c.RESET}")

    def file_header(self, index, total, filename, eta_text=""):
        width = min(shutil.get_terminal_size((100, 20)).columns, 78)
        print()
        print(f"{self.c.MAGENTA}{'-' * width}{self.c.RESET}")
        tag = f"[{index}/{total}]"
        suffix = f"  {self.c.DIM}{eta_text}{self.c.RESET}" if eta_text else ""
        print(f"{self.c.BOLD}{tag} {filename}{self.c.RESET}{suffix}")

    def step(self, stage, message, ok=None):
        if ok is True:
            mark = f"{self.c.GREEN}OK{self.c.RESET}"
        elif ok is False:
            mark = f"{self.c.RED}!!{self.c.RESET}"
        else:
            mark = f"{self.c.DIM}..{self.c.RESET}"
        print(f"  {mark}  {self.c.BOLD}{stage:<10}{self.c.RESET} {message}")

    def success(self, message):
        print(f"  {self.c.GREEN}{self.c.BOLD}\u2713{self.c.RESET} {message}")

    def warn(self, message):
        print(f"{self.c.YELLOW}{self.c.BOLD}Warning:{self.c.RESET} {self.c.YELLOW}{message}{self.c.RESET}")

    def error(self, message):
        print(f"{self.c.RED}{self.c.BOLD}Error:{self.c.RESET} {self.c.RED}{message}{self.c.RESET}")

    def progress_bar(self, label):
        return ProgressBar(label, self)

    # -- summary ---------------------------------------------------------

    def summary(self, total, sent, duplicates, skipped, failed, elapsed):
        width = min(shutil.get_terminal_size((100, 20)).columns, 78)
        print()
        print(f"{self.c.BOLD}{'=' * width}{self.c.RESET}")
        print(f"{self.c.BOLD}RUN COMPLETE{self.c.RESET}  ({format_duration(elapsed)} total)")
        print(f"{self.c.BOLD}{'=' * width}{self.c.RESET}")

        rows = [
            ("Sent", len(sent), self.c.GREEN),
            ("Already in destination", len(duplicates), self.c.CYAN),
            ("Skipped (too large)", len(skipped), self.c.YELLOW),
            ("Failed", len(failed), self.c.RED),
        ]
        label_width = max(len(r[0]) for r in rows)
        for label, count, color in rows:
            print(f"  {color}{label:<{label_width}}{self.c.RESET}  :  {count}")

        print(f"  {'Total files':<{label_width}}  :  {total}")

        if failed:
            print()
            print(f"{self.c.RED}{self.c.BOLD}Failed files:{self.c.RESET}")
            for name, reason in failed:
                print(f"  - {name}: {reason}")
            print(f"{self.c.DIM}  Local copies of failed files were kept so they can be retried "
                  f"without re-downloading from Drive.{self.c.RESET}")

        print(f"{self.c.BOLD}{'=' * width}{self.c.RESET}")
