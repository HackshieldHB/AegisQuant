import os
import sys
import json
import time
import atexit
import psutil
from typing import Optional, Dict, Any
from Core.Logger import AG_LOGGER

class SingletonException(Exception):
    """Raised when another instance of the application is already running."""
    pass

class SingletonLock:
    """
    Institution-Grade Multi-Layer Singleton Lock System.
    Provides Defense-in-Depth mutual exclusion across the OS.
    
    Layers:
    A) OS-level exclusive file lock (msvcrt on Windows, fcntl on Unix). Released by OS on crash.
    B) PID & Timestamp recording for stale lock recovery.
    C) Process Signature matching via psutil to guarantee PID belongs to AegisQuant.
    """
    
    def __init__(self, lock_name: str = "aegisquant.lock"):
        self.logger = AG_LOGGER
        self.lock_name = lock_name
        self.lock_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), lock_name)
        self.lock_file = None
        
        # Determine platform locking mechanism
        if sys.platform == 'win32':
            try:
                import msvcrt
                self._platform_lock = self._win_lock
                self._platform_unlock = self._win_unlock
                self.msvcrt = msvcrt
            except ImportError:
                self.logger.warning("msvcrt missing on Windows; falling back to basic PID verification.")
                self._platform_lock = self._dummy_lock
                self._platform_unlock = self._dummy_unlock
        else:
            try:
                import fcntl
                self._platform_lock = self._unix_lock
                self._platform_unlock = self._unix_unlock
                self.fcntl = fcntl
            except ImportError:
                self.logger.warning("fcntl missing on Unix; falling back to basic PID verification.")
                self._platform_lock = self._dummy_lock
                self._platform_unlock = self._dummy_unlock
                
        # Register atexit handler for clean destruction
        atexit.register(self.release)

    def _win_lock(self, file_obj) -> bool:
        try:
            # LK_NBLCK: Non-blocking exclusive lock
            self.msvcrt.locking(file_obj.fileno(), self.msvcrt.LK_NBLCK, 1)
            return True
        except (IOError, OSError):
            return False

    def _win_unlock(self, file_obj) -> None:
        try:
            file_obj.seek(0)
            self.msvcrt.locking(file_obj.fileno(), self.msvcrt.LK_UNLCK, 1)
        except (IOError, OSError):
            pass

    def _unix_lock(self, file_obj) -> bool:
        try:
            # LOCK_EX | LOCK_NB: Exclusive, non-blocking lock
            self.fcntl.flock(file_obj.fileno(), self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            return False

    def _unix_unlock(self, file_obj) -> None:
        try:
            self.fcntl.flock(file_obj.fileno(), self.fcntl.LOCK_UN)
        except (IOError, OSError):
            pass

    def _dummy_lock(self, file_obj) -> bool:
        return True
        
    def _dummy_unlock(self, file_obj) -> None:
        pass

    def _read_metadata(self) -> Optional[Dict[str, Any]]:
        """Read PID and signature metadata from an existing lock file."""
        if not os.path.exists(self.lock_path):
            return None
        try:
            with open(self.lock_path, 'r') as f:
                content = f.read().strip()
                if not content:
                    return None
                return json.loads(content)
        except Exception:
            return None

    def _write_metadata(self) -> None:
        """Write current process identity to the lock file."""
        if not self.lock_file:
            return
        
        try:
            p = psutil.Process(os.getpid())
            metadata = {
                "pid": p.pid,
                "name": p.name(),
                "cmdline": p.cmdline(),
                "create_time": p.create_time(),
                "lock_time": time.time(),
                "cwd": p.cwd()
            }
            
            self.lock_file.seek(0)
            self.lock_file.truncate()
            self.lock_file.write(json.dumps(metadata, indent=4))
            self.lock_file.flush()
        except Exception as e:
            self.logger.warning("Failed to write lock metadata: %s", e)

    def _verify_process_alive(self, metadata: Dict[str, Any]) -> bool:
        """Verify if the PID in metadata is actually running OUR application."""
        pid = metadata.get("pid")
        if not pid:
            return False
            
        try:
            # Check if PID exists
            if not psutil.pid_exists(pid):
                return False
                
            p = psutil.Process(pid)
            
            # Layer C: Process Signature Matching
            # Validate creation time matches to prevent PID-reuse collisions
            create_time = metadata.get("create_time")
            if create_time and abs(p.create_time() - create_time) > 1.0:
                self.logger.info("PID %s exists but create_time mismatched. Stale lock.", pid)
                return False
                
            # Validate command line contains our execution signature
            cmdline = p.cmdline()
            old_cmd = metadata.get("cmdline", [])
            
            # Look for python and our script name signatures
            if not cmdline or 'python' not in str(cmdline).lower():
                return False
                
            return True
            
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def _scan_for_duplicates(self) -> None:
        """
        Layer D: Brute-force process scan for duplicate engine instances.
        Does NOT rely on lock files — inspects all running processes directly.
        
        Excludes entire process tree (self + parent + children) to prevent
        false positives when launched as a subprocess by the Watchdog.
        """
        current_pid = os.getpid()
        
        # Build exclusion set: self + parent + all children
        exclude_pids = {current_pid}
        try:
            me = psutil.Process(current_pid)
            parent_pid = me.ppid()
            if parent_pid:
                exclude_pids.add(parent_pid)
            for child in me.children(recursive=True):
                exclude_pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        
        self.logger.debug("Duplicate scan excluding PIDs (self/parent/children): %s", exclude_pids)
        
        duplicates = []

        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                if proc.info['pid'] in exclude_pids:
                    continue
                cmd = " ".join(proc.info.get('cmdline') or [])
                if ("Main.py" in cmd or "Main_Production" in cmd) and "python" in cmd.lower():
                    duplicates.append((proc.info['pid'], cmd))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if duplicates:
            for dpid, dcmd in duplicates:
                self.logger.critical(
                    "DUPLICATE PROCESS FOUND: PID=%s CMD=%s", dpid, dcmd[:120]
                )
            raise SingletonException(
                f"Duplicate engine instance(s) detected via process scan: "
                f"{[d[0] for d in duplicates]}"
            )

        self.logger.info(
            "Process scan clean: no duplicate engine instances found (scanned %d processes)",
            len(list(psutil.process_iter()))
        )

    def acquire(self) -> None:
        """
        Attempt to acquire the singleton lock. 
        Fail CLOSED immediately if another engine is holding it.
        """
        # Layer D: Process-level scan FIRST (does not rely on lock files)
        self._scan_for_duplicates()

        # Step 1: Check existing metadata to identify stale locks vs active locks
        metadata = self._read_metadata()
        if metadata:
            existing_pid = metadata.get("pid")
            if existing_pid and existing_pid != os.getpid():
                if self._verify_process_alive(metadata):
                    self.logger.critical("🛑 CRITICAL: Another AegisQuant instance is currently running (PID: %s).", existing_pid)
                    self.logger.critical("DUPLICATE ENGINE INSTANCE DETECTED — STARTUP BLOCKED")
                    self.logger.critical("Execution HALTED to prevent duplicate trades and capital wipeout. Failing CLOSED.")
                    raise SingletonException(f"Duplicate instance detected (PID: {existing_pid}).")
                else:
                    self.logger.warning("STALE LOCK DETECTED — RECOVERING (PID %s died). Reclaiming lock.", existing_pid)
                    try:
                        if os.path.exists(self.lock_path):
                            os.remove(self.lock_path)
                    except Exception as e:
                        self.logger.debug("Failed to remove stale lock file: %s", e)
            else:
                # Same PID (already held, shouldn't normally happen but just in case)
                pass
                
        # Step 2: Attempt OS-level atomic file lock
        try:
            # Open in append/read mode without truncating yet to preserve OS locks
            self.lock_file = open(self.lock_path, 'a+')
            
            if not self._platform_lock(self.lock_file):
                self.logger.critical("🛑 CRITICAL: OS-Level lock acquisition failed. Another process holds the mutex.")
                self.logger.critical("Execution HALTED to prevent race conditions. Failing CLOSED.")
                self.lock_file.close()
                self.lock_file = None
                raise SingletonException("OS mutex lock acquisition failed. Duplicate instance running.")
                
            # Lock acquired! Step 3: Write metadata
            self._write_metadata()
            self.logger.info("🔒 Singleton OS Lock acquired successfully (%s).", self.lock_name)
            
        except SingletonException:
            raise
        except Exception as e:
            self.logger.critical("Failed to instantiate singleton lock: %s", e)
            if self.lock_file:
                self.lock_file.close()
                self.lock_file = None
            raise SingletonException(f"Lock creation wrapper failed: {e}")

    def release(self) -> None:
        """Release the OS lock and clean up the file."""
        if self.lock_file:
            try:
                self.logger.info("🔓 Releasing Singleton Lock (%s).", self.lock_name)
                self._platform_unlock(self.lock_file)
                self.lock_file.close()
                
                # Best-effort deletion
                if os.path.exists(self.lock_path):
                    os.remove(self.lock_path)
            except Exception as e:
                self.logger.debug("Failed clean lock release: %s", e)
            finally:
                self.lock_file = None
