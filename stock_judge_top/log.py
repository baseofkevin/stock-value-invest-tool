"""执行日志状态。

调用方通过 configure() 设置 QUIET / LOG_STREAM；
其他模块只导入 log()，不要 from judge.log import QUIET。
"""
import sys

LOG_STREAM = sys.stdout
QUIET = False


def configure(quiet=None, stream=None):
    global QUIET, LOG_STREAM
    if quiet is not None:
        QUIET = quiet
    if stream is not None:
        LOG_STREAM = stream


def log(msg):
    if not QUIET:
        print(msg, file=LOG_STREAM, flush=True)
