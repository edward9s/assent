"""本次沙箱測試用：避開 Python 3.13 在 Windows 套用的 0o700 特殊 ACL。"""
import os


_mkdir = os.mkdir


def _sandbox_mkdir(path, mode=0o777, *, dir_fd=None):
    if mode == 0o700:
        mode = 0o777
    if dir_fd is None:
        return _mkdir(path, mode)
    return _mkdir(path, mode, dir_fd=dir_fd)


os.mkdir = _sandbox_mkdir
