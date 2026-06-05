"""
AegisQuant Structured Logger (Production)
-----------------------------------------
- Uses CONFIG for level and file path.
- No sensitive data in formatters.
- Single logger instance, no duplicate handlers.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

# Import CONFIG after it is fully loaded (avoid circular import: load Logger after Config)
def _get_config() -> Dict[str, Any]:
    try:
        from AegisQuantConfig import CONFIG
        return CONFIG
    except Exception:
        return {}

def setup_logger(
    name: str = "AegisQuant",
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
) -> logging.Logger:
    config = _get_config()
    level = (log_level or config.get("LOGGING", {}).get("LEVEL") or "INFO").upper()
    file_path = log_file or config.get("LOGGING", {}).get("FILE_PATH") or "aegis_quant.log"

    numeric_level = getattr(logging, level, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    logger = logging.getLogger(name)
    logger.setLevel(numeric_level)

    if logger.hasHandlers():
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    class Utf8StreamHandler(logging.StreamHandler):
        """StreamHandler that safely writes UTF-8 log messages to stdout.

        Root cause of the 11-minute event-loop freeze (Windows + subprocess PIPE):
        The original implementation called stream.buffer.write() followed by
        stream.buffer.flush().  On a subprocess PIPE on Windows the BufferedWriter
        flush eventually calls WriteFile() on the OS pipe handle.  When multiple
        threads log rapidly (event-loop thread + Telegram _run_worker thread), the
        4 096-byte Windows anonymous-pipe buffer fills up.  WriteFile() then BLOCKS
        the calling thread until the watchdog's capture thread drains the pipe —
        but that capture thread may itself be waiting on a logging lock held by the
        blocked thread, creating a multi-minute deadlock.

        Fix:
        • Always write via stream.write() (the TextIOWrapper), never via
          stream.buffer directly.  TextIOWrapper with PYTHONUNBUFFERED=1 /
          write_through=True forwards to the OS without an extra buffering layer.
        • Never call flush() explicitly.  With PYTHONUNBUFFERED=1 data reaches
          the OS pipe buffer immediately via write_through; an extra flush() just
          risks calling FlushFileBuffers on the pipe handle which can block.
        • Replace non-ASCII characters that the console encoding cannot handle
          rather than letting UnicodeEncodeError propagate.
        """

        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = self.format(record) + self.terminator
                stream = self.stream
                # Replace unencodable characters instead of raising errors.
                # Do NOT call stream.buffer.write() — that path can block on a
                # Windows subprocess PIPE when the OS pipe buffer fills up.
                try:
                    stream.write(msg)
                except UnicodeEncodeError:
                    # Last-resort fallback: encode→decode with replacement so
                    # the message still reaches the log even if some glyphs are lost.
                    stream.write(msg.encode(stream.encoding or "utf-8", errors="replace")
                                    .decode(stream.encoding or "utf-8", errors="replace"))
                # Do NOT call stream.flush() here — with PYTHONUNBUFFERED=1 data
                # is already written through to the OS; an explicit flush on a pipe
                # handle can invoke FlushFileBuffers() which blocks until the reader
                # has consumed all buffered data.
            except Exception:
                self.handleError(record)

    console = Utf8StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_dir = os.path.dirname(file_path)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


AG_LOGGER = setup_logger()
