"""Lazy native type initialization preserves one consistent family across threads."""
from pathlib import Path
import importlib.util
import threading

def test_fresh_resource_import_has_no_ffi_and_concurrent_explicit_load_has_one_type_family():
    path=Path(__file__).resolve().parents[1]/'src/local_ai_stack/resources.py'
    spec=importlib.util.spec_from_file_location('local_ai_stack._fresh_resources_for_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert 'ctypes' not in module.__dict__ and module._WINDOWS_TYPES_LOADED is False
    barrier=threading.Barrier(8);families=[];errors=[]
    def load():
        try:
            barrier.wait(timeout=5);module._load_windows_types()
            families.append((id(module.FileTime),id(module.MemoryStatus),id(module._Guid)))
        except BaseException as exc:errors.append(type(exc).__name__)
    threads=[threading.Thread(target=load) for _ in range(8)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=8)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors and len(families)==8 and len(set(families))==1
    assert module._WINDOWS_TYPES_LOADED is True
