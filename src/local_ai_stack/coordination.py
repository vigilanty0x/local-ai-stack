"""Coordination interprocessus de l'unique Ollama CPU.

Deux modes sont volontairement distincts :

* le mode prive (tests/postes locaux) cree un verrou 0600 sous le data root ;
* le mode partage (VPS) n'a jamais le droit de creer ou de reparer le verrou.
  Il exige une ressource systeme pre-creee root:<groupe> 0660 dans un
  repertoire root:<groupe> 0750. Les services ne peuvent ainsi ni remplacer
  le fichier ni inventer silencieusement deux verrous differents.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import threading
import time
from typing import Iterator
def data_root() -> Path:
    return Path(os.environ.get("LOCAL_AI_STATE_ROOT", ".local-ai"))

try:  # Windows doit rester importable en mode prive/tests.
    import grp as _grp
except ImportError:  # pragma: no cover - plateforme non POSIX
    _grp = None


class OllamaLockError(RuntimeError):
    """Le verrou n'est pas accessible ou son identite n'est pas sure."""


class OllamaBusy(OllamaLockError):
    """Une autre inference detient deja le verrou."""


_PROCESS_LOCK = threading.Lock()
_FALSE = {"", "0", "false", "no", "off"}


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() not in _FALSE


def configured_lock_path() -> Path:
    configured = str(os.environ.get("OLLAMA_INFERENCE_LOCK") or "").strip()
    if configured:
        return Path(configured)
    return data_root() / "ollama-inference.lock"


def _expected_group_id(name: str | None = None) -> int:
    numeric = str(os.environ.get("OLLAMA_INFERENCE_LOCK_GID") or "").strip()
    if numeric:
        if not numeric.isdigit():
            raise OllamaLockError("GID du verrou Ollama invalide")
        return int(numeric)
    group_name = str(
        name or os.environ.get("OLLAMA_INFERENCE_LOCK_GROUP")
        or "ollama"
    ).strip()
    if not group_name or any(character in group_name for character in "/\0\n"):
        raise OllamaLockError("groupe du verrou Ollama invalide")
    if _grp is None:
        raise OllamaLockError("groupe POSIX indisponible en mode partage")
    try:
        return _grp.getgrnam(group_name).gr_gid
    except KeyError as exc:
        raise OllamaLockError(
            "groupe du verrou Ollama absent: %s" % group_name
        ) from exc


def _open_shared(
    target: Path,
    expected_group: str | None,
    *,
    bind_alias: bool = False,
) -> int:
    if not target.is_absolute():
        raise OllamaLockError("verrou Ollama partage non absolu")
    expected_gid = _expected_group_id(expected_group)
    raw_parent = target.parent
    if bind_alias:
        # Le conteneur CC voit le meme inode via un bind mount de fichier
        # sous son volume historique. Le parent du point de montage appartient
        # a l'application; seul un vrai mountpoint peut donc lever ce controle.
        if not os.path.ismount(target):
            raise OllamaLockError("alias du verrou Ollama non monte")
    else:
        try:
            resolved_parent = raw_parent.resolve(strict=True)
        except OSError as exc:
            raise OllamaLockError("repertoire du verrou Ollama absent") from exc
        if resolved_parent != raw_parent:
            raise OllamaLockError("repertoire du verrou Ollama symbolique")
        directory = os.lstat(raw_parent)
        if (not stat.S_ISDIR(directory.st_mode)
                or stat.S_ISLNK(directory.st_mode)
                or directory.st_uid != 0 or directory.st_gid != expected_gid
                or stat.S_IMODE(directory.st_mode) != 0o750):
            raise OllamaLockError(
                "repertoire du verrou Ollama: root:groupe 0750 requis"
            )
    try:
        before = os.lstat(target)
    except OSError as exc:
        raise OllamaLockError(
            "verrou Ollama partage absent (creation par le service refusee)"
        ) from exc
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != 0 or before.st_gid != expected_gid
            or stat.S_IMODE(before.st_mode) != 0o660):
        raise OllamaLockError(
            "verrou Ollama partage: root:groupe 0660 et lien unique requis"
        )
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise OllamaLockError("ouverture du verrou Ollama partage refusee") from exc
    opened = os.fstat(descriptor)
    if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or opened.st_uid != 0 or opened.st_gid != expected_gid
            or stat.S_IMODE(opened.st_mode) != 0o660):
        os.close(descriptor)
        raise OllamaLockError("verrou Ollama partage remplace pendant l'ouverture")
    return descriptor


def _open_private(target: Path) -> int:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise OllamaLockError("repertoire du verrou Ollama inaccessible") from exc
    target = parent / target.name
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise OllamaLockError("ouverture du verrou Ollama prive refusee") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise OllamaLockError("verrou Ollama prive non regulier ou lie")
    expected_uid = os.geteuid() if hasattr(os, "geteuid") else metadata.st_uid
    expected_gid = os.getegid() if hasattr(os, "getegid") else metadata.st_gid
    # Un verrou existant est validé, jamais réparé par une application.
    # Les ACL Windows restent sous le contrôle de l'opérateur ; les bits
    # POSIX 0600 ne décrivent pas leur autorisation effective.
    if (os.name != "nt" and (
            metadata.st_uid != expected_uid or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600)):
        os.close(descriptor)
        raise OllamaLockError("identite du verrou Ollama prive invalide")
    return descriptor


def _verify_path(descriptor: int, target: Path) -> None:
    opened = os.fstat(descriptor)
    try:
        observed = os.lstat(target)
    except OSError as exc:
        raise OllamaLockError("verrou Ollama disparu pendant flock") from exc
    if ((opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1):
        raise OllamaLockError("verrou Ollama remplacé pendant flock")


@contextmanager
def ollama_inference_lock(
    *,
    blocking: bool = True,
    timeout_s: float = 10.0,
    lock_path: Path | str | None = None,
    shared: bool | None = None,
    expected_group: str | None = None,
) -> Iterator[None]:
    """Reserve Ollama avec un flock borne et une identite de fichier stricte."""
    timeout_s = max(0.0, min(30.0, float(timeout_s)))
    deadline = time.monotonic() + timeout_s
    if blocking:
        acquired = _PROCESS_LOCK.acquire(timeout=timeout_s)
    else:
        acquired = _PROCESS_LOCK.acquire(blocking=False)
    if not acquired:
        raise OllamaBusy("Ollama occupe dans ce processus")

    descriptor = None
    locked = False
    fcntl = None
    msvcrt = None
    try:
        target = Path(lock_path) if lock_path is not None else configured_lock_path()
        shared_mode = (
            _truthy(os.environ.get("OLLAMA_INFERENCE_LOCK_SHARED"))
            if shared is None else bool(shared)
        )
        bind_alias = _truthy(
            os.environ.get("OLLAMA_INFERENCE_LOCK_BIND_ALIAS")
        )
        descriptor = (
            _open_shared(target, expected_group, bind_alias=bind_alias)
            if shared_mode else _open_private(target)
        )
        if os.name == "nt":
            import msvcrt as msvcrt_module
            msvcrt = msvcrt_module
        else:
            import fcntl as fcntl_module
            fcntl = fcntl_module
        while True:
            try:
                if msvcrt is not None:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if not blocking or time.monotonic() >= deadline:
                    raise OllamaBusy("Ollama occupe par un autre processus") from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        _verify_path(descriptor, target)
        os.ftruncate(descriptor, 0)
        os.write(
            descriptor,
            ("pid=%d acquired=%.6f\n" % (os.getpid(), time.time())).encode("ascii"),
        )
        os.fsync(descriptor)
        yield
    finally:
        if descriptor is not None:
            if locked:
                try:
                    if msvcrt is not None:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)
        _PROCESS_LOCK.release()


def ollama_inference_busy(*, lock_path: Path | str | None = None) -> bool:
    """Sonde non bloquante, fermee si le verrou est invalide/inaccessible."""
    try:
        with ollama_inference_lock(
            blocking=False, timeout_s=0.0, lock_path=lock_path
        ):
            return False
    except (OSError, OllamaLockError):
        return True
